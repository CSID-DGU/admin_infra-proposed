#!/usr/bin/env bash
# 실험 스택(ailab-baseline / ailab-noprobe / ailab-full)을 한 번에 띄운다. 여러 번 실행해도 결과가 같다.
# 세 스택은 같은 코드이고 config-server의 실행 방식(RUN_MODE) 하나만 다르다.
#
#   stack-up.sh <baseline|noprobe|full> <config-server 이미지(저장소:태그)> <프론트엔드 이미지> <admin_be 이미지>
#
# admin_infra의 "Deploy Proposed Stack" 워크플로가 배포 서버에서 실행한다. 공개 레포의 Actions 로그에
# 그대로 남으므로 비밀번호, 운영 설정값, 실사용자 계정 이름은 절대 출력하지 않는다(값은 파이프로만 넘김).
set -euo pipefail

STACK=${1:?"스택 이름(baseline|noprobe|full)"}
IMAGE=${2:?"config-server 이미지(저장소:태그)"}
FE_IMAGE=${3:-}   # 비우면 프론트엔드를 올리지 않는다
# admin_be 브랜치에서 빌드한 스택 전용 이미지. 운영 admin_be 이미지는 작업 등록 인터페이스
# (/operations/*)를 모르므로 쓰지 않는다 — 비우면 틀린 조건으로 뜨지 않도록 중단한다.
BE_IMAGE=${4:?"admin_be 이미지(저장소:태그)"}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
if [ -r /etc/kubernetes/ci-deployer.conf ]; then
  export KUBECONFIG=/etc/kubernetes/ci-deployer.conf
fi

PROD_NS=ailab-infra
PROD_RELEASE=containerssh-config-server
PROD_BE_NS=default

# 할당 값은 uid-ranges.yaml 한 곳에 선언돼 있다(admin_infra-proposed#118). case문에 손으로
# 적지 않는 이유와 실제 겪은 충돌 사고들은 그 파일 머리말 주석 참고.
RANGES_FILE="$HERE/uid-ranges.yaml"
[ -f "$RANGES_FILE" ] || { echo "대역 선언 파일이 없음: $RANGES_FILE"; exit 1; }
yaml_field() {  # $1: "- {stack: ..., ...}" 한 줄, $2: 필드명 → 값
  echo "$1" | sed -n "s/.*[{ ]$2: \"\{0,1\}\([^,}\"]*\)\"\{0,1\}.*/\1/p"
}
STACK_LINE=$(grep -E "^- \{stack: $STACK, " "$RANGES_FILE") || true
[ -n "$STACK_LINE" ] || { echo "알 수 없는 스택: $STACK ($RANGES_FILE에 선언 없음)"; exit 2; }
UID_MIN=$(yaml_field "$STACK_LINE" uid_min)
UID_MAX=$(yaml_field "$STACK_LINE" uid_max)
SHARED_GID_MIN=$(yaml_field "$STACK_LINE" shared_gid_min)
SHARED_GID_MAX=$(yaml_field "$STACK_LINE" shared_gid_max)
NP_MIN=$(yaml_field "$STACK_LINE" np_min)
NP_MAX=$(yaml_field "$STACK_LINE" np_max)
CONFIG_NODEPORT=$(yaml_field "$STACK_LINE" config_nodeport)
PREFIX=$(yaml_field "$STACK_LINE" prefix)
# NodePort는 이 클러스터 쿠버네티스 API 서버의 기본 허용 범위(30000~32767) 밖이면 서비스 생성이
# 422로 거부된다(2026-09-18 e2e 점검 중 실측으로 발견) — 배포가 한참 진행된 뒤에야 드러나므로
# 여기서 미리 막는다.
{ [ "$NP_MIN" -ge 30000 ] && [ "$NP_MAX" -le 32767 ]; } || { echo "NodePort 대역 $NP_MIN~$NP_MAX 이 쿠버네티스 허용 범위(30000~32767) 밖임"; exit 1; }
# 개인 그룹은 gid=uid라 uid 대역이 곧 개인 그룹 gid 대역이다 — 자기 스택 안에서 둘이 겹치면
# 공용 그룹이 다음 uid 번호를 선점할 수 있다(#148).
{ [ "$SHARED_GID_MIN" -gt "$UID_MAX" ] || [ "$SHARED_GID_MAX" -lt "$UID_MIN" ]; } || { echo "공용 GID 대역 $SHARED_GID_MIN~$SHARED_GID_MAX 이 자기 UID 대역 $UID_MIN~$UID_MAX 과 겹침"; exit 1; }
# 다른 스택과 대역이 겹치는지 선언 파일 안에서만 비교한다(형제 스택이 지금 떠 있는지와 무관 —
# 배포 상태에 의존하는 실시간 조회는 신뢰할 수 없어 기각했다, admin_infra-proposed#118).
while IFS= read -r OTHER_LINE; do
  case "$OTHER_LINE" in "- {stack:"*) ;; *) continue ;; esac
  OTHER_STACK=$(yaml_field "$OTHER_LINE" stack)
  [ "$OTHER_STACK" = "$STACK" ] && continue
  O_UID_MIN=$(yaml_field "$OTHER_LINE" uid_min); O_UID_MAX=$(yaml_field "$OTHER_LINE" uid_max)
  O_SG_MIN=$(yaml_field "$OTHER_LINE" shared_gid_min); O_SG_MAX=$(yaml_field "$OTHER_LINE" shared_gid_max)
  O_NP_MIN=$(yaml_field "$OTHER_LINE" np_min); O_NP_MAX=$(yaml_field "$OTHER_LINE" np_max)
  if [ "$UID_MIN" -le "$O_UID_MAX" ] && [ "$UID_MAX" -ge "$O_UID_MIN" ]; then
    echo "UID 대역이 $OTHER_STACK 과 겹침: $UID_MIN~$UID_MAX vs $O_UID_MIN~$O_UID_MAX"; exit 1
  fi
  if [ "$SHARED_GID_MIN" -le "$O_SG_MAX" ] && [ "$SHARED_GID_MAX" -ge "$O_SG_MIN" ]; then
    echo "공용 GID 대역이 $OTHER_STACK 과 겹침: $SHARED_GID_MIN~$SHARED_GID_MAX vs $O_SG_MIN~$O_SG_MAX"; exit 1
  fi
  if [ "$SHARED_GID_MIN" -le "$O_UID_MAX" ] && [ "$SHARED_GID_MAX" -ge "$O_UID_MIN" ]; then
    echo "공용 GID 대역이 $OTHER_STACK 의 UID 대역과 겹침: $SHARED_GID_MIN~$SHARED_GID_MAX vs $O_UID_MIN~$O_UID_MAX"; exit 1
  fi
  if [ "$NP_MIN" -le "$O_NP_MAX" ] && [ "$NP_MAX" -ge "$O_NP_MIN" ]; then
    echo "NodePort 대역이 $OTHER_STACK 과 겹침: $NP_MIN~$NP_MAX vs $O_NP_MIN~$O_NP_MAX"; exit 1
  fi
done < "$RANGES_FILE"
# config-server의 RUN_MODE(구 이름 VERIFY_MODE)는 baseline/noprobe/full 셋만 허용한다 —
# 재시도·복구·실접근 검증 여부를 가르는 동작 방식 값이지 네임스페이스 구분자가 아니다.
# 실운영(operation)은 네임스페이스·UID대역·접두어로 이미 다른 스택과 구분되므로, 동작
# 방식은 재시도·결과 확인 없이 뒷정리 후 종료하는 baseline(=기존 운영과 같은 동작)으로 맞춘다.
VERIFY_MODE="$STACK"
[ "$STACK" = "operation" ] && VERIFY_MODE=baseline
NS=ailab-$STACK
RELEASE=config-server-$STACK
# 스택 화면은 운영 프론트엔드가 쓰는 ingress 컨트롤러(nodePort 30081, 방화벽 허용)에 호스트 규칙을 더해 연다.
# nip.io는 이름에 적힌 IP로 해석해 주는 공개 DNS라 DNS 설정 없이 스택마다 다른 호스트를 쓸 수 있다.
PUBLIC_IP=210.94.179.18
FE_HOST=$STACK.$PUBLIC_IP.nip.io

step() { echo; echo "=== $*"; }
render() { sed -e "s|__NS__|$NS|g" "$@"; }
rnd() { head -c "${1:-16}" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
# 크론잡 Pod도 같은 app 라벨을 쓰므로 job-name 라벨이 붙은 Pod는 뺀다.
# 배포 직후 이전 Pod는 진행 중인 요청을 마칠 때까지(최대 10분) 종료 중이면서도 Running으로 남는다.
# 그런 Pod는 새 요청에 응답하지 않으므로 고르지 않는다.
running_pod() {
  kubectl -n "$1" get pod -l "$2,!job-name" --field-selector=status.phase=Running \
    -o go-template='{{range .items}}{{if not .metadata.deletionTimestamp}}pod/{{.metadata.name}}{{"\n"}}{{end}}{{end}}' | head -1
}
getpw() { kubectl -n "$NS" get secret stack-db -o jsonpath="{.data.$1}" | base64 -d; }

# 운영 계정 대장(NAS의 kubeSharePath)은 예전엔 실행 중인 운영 config-server Pod 안에서 읽었다. 운영을 내려도
# 배포가 돌도록, 스택 네임스페이스에 그 경로만 붙인 임시 도우미 Pod를 띄워 읽고 끝나면 지운다.
LEDGER_POD=ledger-helper
ledger_cleanup() { kubectl -n "$NS" delete pod "$LEDGER_POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
start_ledger_helper() {  # $1: NFS 서버, $2: 계정 대장 경로
  kubectl -n "$NS" delete pod "$LEDGER_POD" --ignore-not-found --wait=true --timeout=60s >/dev/null 2>&1 || true
  # kube_share NFS 마운트를 NAS가 허용하는 노드는 csid-dgu-desktop 하나뿐이다 — config-server
  # 자신도 항상 거기 고정 배포된다(Chart/values.yaml 참고). farm 노드는 이 경로 마운트 권한이
  # 없어(mount.nfs: Operation not permitted) 스케줄러가 임의로 farm 노드를 고르면 실패한다.
  # nodeName을 직접 주면 스케줄러의 테인트 검사를 건너뛰므로 control-plane toleration 없이도 뜬다.
  local pin="  nodeName: csid-dgu-desktop"
  cat <<YAML | kubectl -n "$NS" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: $LEDGER_POD
  labels: {app: ledger-helper}
spec:
$pin
  restartPolicy: Never
  containers:
    - name: helper
      image: $IMAGE
      imagePullPolicy: IfNotPresent
      command: ["sleep", "1800"]
      volumeMounts: [{name: kube-share, mountPath: /kube_share}]
  volumes:
    - name: kube-share
      nfs: {server: "$1", path: "$2"}
YAML
  kubectl -n "$NS" wait --for=condition=Ready pod/"$LEDGER_POD" --timeout=5m >/dev/null
}
ledger() { kubectl -n "$NS" exec "$LEDGER_POD" -- "$@"; }
BASE=$(mktemp); trap 'rm -f "$BASE"; ledger_cleanup' EXIT

step "사전 확인"
# NFS·NAS·Kerberos·farm 노드 설정은 운영 릴리스 값을 그대로 쓴다. 값은 릴리스에 남아 있어 운영 Pod가 없어도 읽힌다.
helm -n "$PROD_NS" get values "$PROD_RELEASE" -o yaml > "$BASE"
KUBE_SHARE=$(sed -n 's/^[[:space:]]*kubeSharePath:[[:space:]]*//p' "$BASE" | head -1 | tr -d "\"'")
NFS_SERVER=$(awk '/^nfs:/{f=1; next} f && /^[^[:space:]]/{f=0} f && /^[[:space:]]+server:/{sub(/^[[:space:]]+server:[[:space:]]*/, ""); gsub(/["\047]/, ""); print; exit}' "$BASE")
[ -n "$KUBE_SHARE" ] || { echo "운영 kubeSharePath를 찾지 못함"; exit 1; }
[ -n "$NFS_SERVER" ] || { echo "운영 nfs.server를 찾지 못함"; exit 1; }
for PORT in "$CONFIG_NODEPORT"; do
  TAKEN=$(kubectl get svc -A -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{range .spec.ports[*]}{.nodePort}{" "}{end}{"\n"}{end}' \
    | awk -v ns="$NS" -v p="$PORT" '$1 != ns { for (i = 2; i <= NF; i++) if ($i == p) c++ } END { print c+0 }')
  [ "$TAKEN" = 0 ] || { echo "nodePort $PORT를 다른 네임스페이스가 쓰고 있음"; exit 1; }
done
HOST_TAKEN=$(kubectl get ing -A -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{range .spec.rules[*]}{.host}{" "}{end}{"\n"}{end}' \
  | awk -v ns="$NS" -v h="$FE_HOST" '$1 != ns { for (i = 2; i <= NF; i++) if ($i == h) c++ } END { print c+0 }')
[ "$HOST_TAKEN" = 0 ] || { echo "ingress 호스트 $FE_HOST를 다른 네임스페이스가 쓰고 있음"; exit 1; }
echo "config-server nodePort $CONFIG_NODEPORT 사용 가능, 화면 호스트 $FE_HOST 사용 가능"

step "네임스페이스 $NS"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$NS" ailab.dgu/proposed-stack="$STACK" --overwrite >/dev/null

step "사용자 컨테이너 우선순위 등급"
# 노드 디스크가 쪼들리면 kubelet이 Pod를 쫓아낸다. 그 순서는 자원 요청 초과 여부와 우선순위로
# 갈리는데, 우선순위를 비워 두면(0) 사용자 컨테이너가 이름 없는 다른 Pod들과 같은 취급을 받아
# 먼저 밀려난다. 실제로 마이그레이션 중이던 사용자 컨테이너가 이렇게 축출돼 작업이 실패했다.
# 시스템 등급(20억)보다 한참 낮게 두어 클러스터 운영에는 영향을 주지 않고, 선점은 끈다 —
# 이 등급은 밀려나지 않기 위한 것이지 남을 밀어내기 위한 것이 아니다.
# 클러스터 전역 자원이라 세 스택이 각각 적용해도 같은 내용이라 문제없다.
cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: ailab-user-workload
value: 100000
globalDefault: false
preemptionPolicy: Never
description: "사용자 컨테이너 — 자원 압박 시 시스템 구성요소보다 늦게 축출된다"
YAML
echo "ailab-user-workload 적용"

step "스택 구성요소 우선순위 등급"
# 사용자 컨테이너만 지켜서는 부족하다. 백엔드·프론트엔드·config-server·DB·Redis가 밀려나면
# 사용자 컨테이너가 살아 있어도 신청·조회·작업이 전부 멈춘다. 실제로 백엔드가 한 노드에서
# 19번 연속 축출됐다. 그래서 구성요소는 사용자 컨테이너보다 한 단계 위에 둔다 —
# 둘 다 밀려날 상황이면 서비스를 굴리는 쪽을 남기는 편이 복구가 빠르다.
# 선점은 여기서도 끈다. 자리를 빼앗지 않고 축출 순서만 뒤로 미룬다.
cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: ailab-stack-component
value: 200000
globalDefault: false
preemptionPolicy: Never
description: "스택 구성요소 — 사용자 컨테이너보다도 늦게 축출된다"
YAML
echo "ailab-stack-component 적용"

step "계정 대장 확인 (임시 도우미 Pod)"
start_ledger_helper "$NFS_SERVER" "$KUBE_SHARE"
USED=$(ledger awk -F: -v lo="$UID_MIN" -v hi="$UID_MAX" '$3>=lo && $3<=hi {n++} END {print n+0}' /kube_share/passwd)
[ "$USED" = 0 ] || { echo "운영 계정 대장에 UID $UID_MIN~$UID_MAX 계정이 ${USED}개 있음. 대역을 옮겨야 함"; exit 1; }
# config-server는 nfs.kubeSharePath를 /kube_share로 마운트해 passwd/group/shadow를 둔다. 운영 경로의
# 하위 디렉터리를 스택 전용으로 쓰므로 테스트 계정이 운영 대장에 섞이지 않는다. 비어 있으면
# config-server가 처음 계정을 만들 때 기본 파일로 채운다.
# (실운영도 같은 방식이다 — 원본 경로는 NAS가 마운트를 거부해 격리 방식으로 되돌렸다. 위 UID_MAX가
# 이제 겹치지 않으므로 세 스택과 다를 게 없다.)
STACK_KUBE_SHARE="$KUBE_SHARE/exp-$STACK"
ledger mkdir -p "/kube_share/exp-$STACK"
echo "UID $UID_MIN~$UID_MAX 비어 있음, 스택 계정 대장 /kube_share/exp-$STACK"

step "SSH 키 복사 ($PROD_NS → $NS)"
for s in nas-ssh-key farm-ssh-key farm-ad-ssh-key; do
  TYPE=$(kubectl -n "$PROD_NS" get secret "$s" -o jsonpath='{.type}')
  {
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\ntype: %s\ndata:\n' "$s" "$TYPE"
    kubectl -n "$PROD_NS" get secret "$s" -o go-template='{{range $k, $v := .data}}  {{$k}}: {{$v}}{{"\n"}}{{end}}'
  } | kubectl -n "$NS" apply -f - >/dev/null
  echo "$s"
done

step "스택 DB 비밀번호"
if kubectl -n "$NS" get secret stack-db >/dev/null 2>&1; then
  echo "기존 값 유지"
else
  kubectl -n "$NS" create secret generic stack-db \
    --from-literal=root="$(rnd)" --from-literal=pod_port_user="$(rnd)" --from-literal=log_db_user="$(rnd)" \
    --from-literal=admin_user="$(rnd)" --from-literal=admin_redis="$(rnd)" --from-literal=jwt_secret="$(rnd 32)" >/dev/null
  echo "새로 생성"
fi
kubectl -n "$NS" create secret generic config-server-db-secret --from-literal=password="$(getpw pod_port_user)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" create secret generic log-mysql-secret \
  --from-literal=MYSQL_PASSWORD="$(getpw log_db_user)" --from-literal=MYSQL_ROOT_PASSWORD="$(getpw root)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

step "SSH 호스트 키"
# config-server가 farm·AD·NAS에 접속할 때 상대 서버를 확인하도록, 배포 서버에서 호스트 키를 모아 Secret으로 넣는다.
# 주소가 공개 로그에 남지 않게 개수만 출력한다. 하나라도 받지 못하면 넣지 않는다(확인을 켜면 그 호스트 접속이 막힌다).
KH_CHANGED=0
# 배포 서버에 PyYAML이 없어도 되도록 helm 값을 JSON으로 받아 표준 라이브러리로만 읽는다.
# 목록을 못 만들면 배포를 멈추지 않고 이번에는 호스트 키 확인을 바꾸지 않는다.
KH_FILE=$(mktemp); KH_OK=0; KH_ALL=0
if ! TARGETS=$(helm -n "$PROD_NS" get values "$PROD_RELEASE" -o json | python3 "$HERE/ssh_targets.py" 2>/dev/null); then
  echo "접속 대상 목록을 만들지 못함"; TARGETS=""
fi
while read -r host port; do
  [ -n "$host" ] || continue
  KH_ALL=$((KH_ALL + 1))
  if { ssh-keyscan -T 5 -p "$port" "$host" 2>/dev/null || true; } | { grep -v '^#' || true; } > "$KH_FILE.part" && [ -s "$KH_FILE.part" ]; then
    cat "$KH_FILE.part" >> "$KH_FILE"; KH_OK=$((KH_OK + 1))
  fi
  rm -f "$KH_FILE.part"
done <<< "$TARGETS"
if [ "$KH_ALL" -gt 0 ] && [ "$KH_OK" = "$KH_ALL" ]; then
  # 첫 배포엔 Secret이 없어 kubectl이 실패한다(pipefail로 스크립트가 멈추지 않게 빈 값으로 둔다).
  OLD=$({ kubectl -n "$NS" get secret config-server-ssh-known-hosts -o jsonpath='{.data.known_hosts}' 2>/dev/null || true; } | { base64 -d 2>/dev/null || true; } | sort | sha256sum)
  NEW=$(sort "$KH_FILE" | sha256sum)
  kubectl -n "$NS" create secret generic config-server-ssh-known-hosts --from-file=known_hosts="$KH_FILE" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  [ "$OLD" = "$NEW" ] || KH_CHANGED=1
  echo "호스트 ${KH_ALL}개 키 수집, 호스트 키 확인 켬$([ "$KH_CHANGED" = 1 ] && echo " (변경됨)")"
else
  echo "호스트 키를 ${KH_OK}/${KH_ALL}개만 받음 — 이번 배포에서는 호스트 키 확인을 바꾸지 않음"
fi
rm -f "$KH_FILE"

step "config-server 내부 API 토큰"
TOKEN_NEW=0
if [ -z "$(kubectl -n "$NS" get secret stack-db -o jsonpath='{.data.config_api_token}')" ]; then
  kubectl -n "$NS" patch secret stack-db -p "{\"data\":{\"config_api_token\":\"$(rnd 32 | base64 -w0)\"}}" >/dev/null
  TOKEN_NEW=1
fi
kubectl -n "$NS" create secret generic config-server-api-token --from-literal=token="$(getpw config_api_token)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
echo "$([ "$TOKEN_NEW" = 1 ] && echo 새로 생성 || echo 기존 값 유지)"

step "MySQL"
render "$HERE/mysql.yaml" | kubectl apply -f -
kubectl -n "$NS" rollout status statefulset/mysql --timeout=10m
{
  echo "CREATE DATABASE IF NOT EXISTS pod_port_db; CREATE DATABASE IF NOT EXISTS operation_state_db; CREATE DATABASE IF NOT EXISTS web_admin;"
  for pair in "pod_port_user:pod_port_db" "log_db_user:operation_state_db" "admin_user:web_admin"; do
    u=${pair%%:*}; db=${pair##*:}; pw=$(getpw "$u")
    echo "CREATE USER IF NOT EXISTS '$u'@'%' IDENTIFIED BY '$pw'; ALTER USER '$u'@'%' IDENTIFIED BY '$pw'; GRANT ALL ON $db.* TO '$u'@'%';"
  done
  echo "FLUSH PRIVILEGES;"
} | kubectl -n "$NS" exec -i mysql-0 -- sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" 2>/dev/null'
kubectl -n "$NS" exec -i mysql-0 -- sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" pod_port_db 2>/dev/null' < "$ROOT/infra-sql/pod_port_db.sql"
kubectl -n "$NS" exec -i mysql-0 -- sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" operation_state_db 2>/dev/null' < "$ROOT/infra-sql/operation_log.sql"
kubectl -n "$NS" exec -i mysql-0 -- sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" operation_state_db 2>/dev/null' < "$ROOT/infra-sql/trial_manifest.sql"
echo "DB 3개(pod_port_db, operation_state_db, web_admin)와 테이블 준비 완료"

step "Redis"
render "$HERE/redis.yaml" | kubectl apply -f -

step "이미지 저장소"
# 사용자 이미지 커밋·재시작용 NAS 볼륨은 논문 실험에 필요 없고, 첫 설치에서 이 노드의 NFS 마운트가
# 시간 초과로 막혔다. 실험 스택은 임시 디스크를 쓴다(imageStore.claimName 비움). 예전 실행에서 만든
# PVC 릴리스가 있으면 정리한다.
if helm -n "$NS" status pvc-image-store >/dev/null 2>&1; then
  helm -n "$NS" uninstall pvc-image-store --wait >/dev/null && echo "이전 PVC 릴리스 정리 (NAS의 PV는 Retain이라 남음)"
else
  echo "임시 디스크 사용"
fi

step "config-server ($RELEASE, $IMAGE)"
# 운영 릴리스 값($BASE, 사전 확인에서 읽음)에 스택마다 달라야 하는 값만 덮어쓴다.
# 첫 설치가 실패한 릴리스는 upgrade가 받지 않을 수 있으므로 지우고 다시 설치한다.
if helm -n "$NS" status "$RELEASE" -o json 2>/dev/null | grep -q '"status":"failed"'; then
  helm -n "$NS" uninstall "$RELEASE" --wait >/dev/null && echo "실패한 이전 설치 정리"
fi
helm upgrade --install "$RELEASE" "$ROOT/config-server/Chart" -n "$NS" -f "$BASE" \
  --set namespace="$NS" --set config.namespace="$NS" \
  --set image.repository="${IMAGE%:*}" --set image.tag="${IMAGE##*:}" --set image.pullPolicy=IfNotPresent \
  --set service.nodePort="$CONFIG_NODEPORT" \
  --set nfs.kubeSharePath="$STACK_KUBE_SHARE" \
  --set infra.adminBeInternalUrl="http://admin-prod.$NS" \
  --set accounts.uidMin="$UID_MIN" --set accounts.uidMax="$UID_MAX" --set accounts.prefix="$PREFIX" \
  --set accounts.sharedGidMin="$SHARED_GID_MIN" --set accounts.sharedGidMax="$SHARED_GID_MAX" \
  --set nodeport.min="$NP_MIN" --set nodeport.max="$NP_MAX" \
  --set verifyMode="$VERIFY_MODE" \
  --set redis.host="redis-bg-master.$NS.svc.cluster.local" \
  --set db.host=infra-mysql --set logDb.host=log-mysql \
  --set imageStore.claimName= \
  --set controller.enabled=true \
  --set priorityClassName=ailab-stack-component \
  --wait --timeout 10m
if [ "$TOKEN_NEW" = 1 ] || [ "$KH_CHANGED" = 1 ]; then
  # 토큰·호스트 키 Secret은 Pod 템플릿에 드러나지 않아 helm 업그레이드만으로는 새 값을 읽지 않는다.
  kubectl -n "$NS" rollout restart deployment/"$RELEASE" deployment/"$RELEASE-controller" >/dev/null
  kubectl -n "$NS" rollout status deployment/"$RELEASE" --timeout=10m
  kubectl -n "$NS" rollout status deployment/"$RELEASE-controller" --timeout=10m
fi

step "admin_be"
ADMIN_IMAGE=$BE_IMAGE
# 운영 admin_be는 설정 파일을 이미지가 아니라 admin-prod-config Secret으로 받는다. 같은 파일을 복사해
# 같은 위치에 넣고, 운영 자원을 가리키는 값만 아래 SPRING_APPLICATION_JSON으로 덮어쓴다.
kubectl -n "$PROD_BE_NS" get secret admin-prod-config >/dev/null 2>&1 || { echo "운영 admin-prod-config Secret이 없음"; exit 1; }
{
  printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: admin-prod-config\ntype: Opaque\ndata:\n'
  kubectl -n "$PROD_BE_NS" get secret admin-prod-config -o go-template='{{range $k, $v := .data}}  {{$k}}: {{$v}}{{"\n"}}{{end}}'
} | kubectl -n "$NS" apply -f - >/dev/null
PROD_CFG_HASH=$(kubectl -n "$PROD_BE_NS" get secret admin-prod-config -o jsonpath='{.data}' | sha256sum | cut -c1-16)
# 운영 설정 중 운영 자원을 가리키는 것은 모두 여기서 덮어쓴다. 실험 스택 셋은 Slack을 닿지 않는
# 주소로 돌려 실수로 진짜 채널에 보내지 못하게 막는다. 실운영(operation)은 그 자체가 진짜라 이
# 안전장치를 끄고 admin-prod-config Secret(위에서 그대로 복사한 실제 파일 설정)의 진짜 webhook·
# 봇 토큰이 그대로 쓰이게 둔다 — SPRING_APPLICATION_JSON(env)이 파일 설정보다 우선순위가 높으므로
# 여기서 키 자체를 안 넣어야 파일 값이 이긴다(e2e 점검 중 이 안전장치가 실운영도 막고 있던 걸 발견).
# 메일(가입 인증 코드, 만료 안내)은 운영 설정 그대로 보낸다. 서명키는 새로 줘서 운영에서 발급한 토큰이
# 여기서 통하지 않게 한다.
SINK=http://127.0.0.1:9/
SLACK_OVERRIDE=",\"slack-webhook-url\":{\"error-log\":\"$SINK\",\"noti\":\"$SINK\",\"farm-admin\":\"$SINK\",\"lab-admin\":\"$SINK\"},\"slack\":{\"bot-token\":\"disabled\"}"
[ "$STACK" = "operation" ] && SLACK_OVERRIDE=""

CONFIG_JSON=$(cat <<EOF
{"spring":{"datasource":{"url":"jdbc:mysql://admin-mysql.$NS.svc.cluster.local:3306/web_admin?serverTimezone=Asia/Seoul&useSSL=false&allowPublicKeyRetrieval=true","username":"admin_user","password":"$(getpw admin_user)"},"data":{"redis":{"host":"admin-redis.$NS.svc.cluster.local","port":6379,"password":"$(getpw admin_redis)"}},"jpa":{"hibernate":{"ddl-auto":"update"}}},"config":{"base-url":"http://containerssh-config-service.$NS.svc.cluster.local","api-token":"$(getpw config_api_token)"}$SLACK_OVERRIDE,"prometheus":{"base-url":"http://127.0.0.1:9"},"kubernetes":{"pod-namespace":"$NS"},"jwt":{"secret":"$(getpw jwt_secret)"}}
EOF
)
kubectl -n "$NS" create secret generic admin-be-config --from-literal=SPRING_APPLICATION_JSON="$CONFIG_JSON" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
CONFIG_HASH=$(printf '%s%s' "$CONFIG_JSON" "$PROD_CFG_HASH" | sha256sum | cut -c1-16)
# 쿠버네티스 API 주소(서비스 주소 + 실제 API 서버 주소). 내부 주소라 공개 로그에 출력하지 않는다.
API_SVC_IP=$(kubectl -n default get svc kubernetes -o jsonpath='{.spec.clusterIP}')
API_EP=$(kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes \
  -o jsonpath='{range .items[*]}{range .endpoints[*]}{.addresses[0]}{" "}{end}{end}')
API_EP_PORT=$(kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes \
  -o jsonpath='{.items[0].ports[0].port}')
if [ -z "$API_SVC_IP" ] || [ -z "$API_EP" ] || [ -z "$API_EP_PORT" ]; then
  echo "쿠버네티스 API 주소를 읽지 못함"; exit 1
fi
API_EGRESS="    - to:
        - ipBlock:
            cidr: $API_SVC_IP/32
      ports:
        - protocol: TCP
          port: 443"
for ip in $API_EP; do
  API_EGRESS="$API_EGRESS
    - to:
        - ipBlock:
            cidr: $ip/32
      ports:
        - protocol: TCP
          port: $API_EP_PORT"
done
# 실험 스택 셋은 실수로 진짜 Slack 채널에 알림을 보내지 않도록 443(HTTPS)을 막아 두는 게 맞지만,
# 실운영(operation)은 그 자체가 진짜라 여기서 Slack 알림이 실제로 나가야 한다(e2e 점검 중 전부
# ResourceAccessException으로 막혀 있던 걸 발견, 2026-09-18). Slack IP는 고정돼 있지 않아
# ipBlock으로 좁힐 수 없으므로 443만 전체 허용한다.
if [ "$STACK" = "operation" ]; then
  API_EGRESS="$API_EGRESS
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
      ports:
        - protocol: TCP
          port: 443"
fi
render "$HERE/admin-be.yaml" | sed -e "s|__ADMIN_IMAGE__|$ADMIN_IMAGE|" -e "s|__CONFIG_HASH__|$CONFIG_HASH|" \
  | API_EGRESS="$API_EGRESS" awk '{ if (index($0, "__API_EGRESS__")) print ENVIRON["API_EGRESS"]; else print }' \
  | kubectl apply -f -
kubectl -n "$NS" rollout status deployment/admin-prod --timeout=10m
kubectl -n "$NS" rollout status deployment/redis-bg-master --timeout=5m
kubectl -n "$NS" rollout status deployment/admin-redis --timeout=5m
echo "admin_be 이미지: ${ADMIN_IMAGE##*[@:]}"

step "기준 데이터 확인 (스택 admin DB)"
# 신청 화면에 필요한 서버·자원 그룹·노드·GPU·이미지와 메일 문구는 최초 1회 스택에 심어 두면 된다.
# 운영은 9/15부터 평소 0대로 내려가 있는 게 정상이라(비용 절감), 운영 DB에서 매번 새로 복사해 올
# 원본이 없다 — 스택에 이미 있는 기준 데이터를 그대로 쓴다. 스택에 기준 데이터가 아예 없으면
# (새 스택 최초 기동 등) 다른 스택에서 옮기거나 운영을 잠시 띄워 수동으로 넣어야 한다.
smy()  { kubectl -n "$NS" exec mysql-0 -- sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N "$@"' _ "$@" </dev/null; }
HAVE=$(smy -e "SELECT COUNT(*) FROM web_admin.resource_groups" 2>/dev/null || echo 0)
if [ "${HAVE:-0}" -gt 0 ]; then
  echo "스택의 기존 기준 데이터를 씀 (자원 그룹 ${HAVE}행)"
else
  echo "스택에 기준 데이터가 없음 — 다른 스택에서 기준 데이터를 옮기거나 운영을 잠시 띄워 수동으로 넣어야 함"; exit 1
fi

step "사용자 컨테이너 이미지 태그"
# 스택의 이미지 목록을 새로 빌드한 dguailab/decs 날짜 태그로 맞춘다(시작 스크립트 수정 반영). 새로 만들거나
# 마이그레이션하는 컨테이너부터 적용되고, 떠 있는 컨테이너는 그대로다. 이미지 태그는 공개 정보라 출력한다.
DECS_TAG=260915
DECS_VARIANTS='cuda(11[.]8-tf2[.]13|12[.]2-tf2[.]15|12[.]3-tf2[.]16|12[.]5-tf2[.]20|12[.]8-tf2[.]20)'
smy -e "UPDATE web_admin.container_image SET image_version = REGEXP_REPLACE(image_version, '-[0-9]{6}\$', '-$DECS_TAG')
  WHERE image_name LIKE '%dguailab/decs' AND image_version REGEXP '^${DECS_VARIANTS}-ubuntu22[.]04-[0-9]{6}\$';
  UPDATE web_admin.container_image SET image_version = CONCAT(image_version, '-ubuntu22.04-$DECS_TAG')
  WHERE image_name LIKE '%dguailab/decs' AND image_version REGEXP '^${DECS_VARIANTS}\$'"
smy -e "SELECT CONCAT(image_version, ' (', COUNT(*), ')') FROM web_admin.container_image
  WHERE image_name LIKE '%dguailab/decs' GROUP BY image_version ORDER BY image_version" | sed 's/^/  /'

step "프론트엔드"
if [ -n "$FE_IMAGE" ]; then
  render "$HERE/admin-fe.yaml" | sed -e "s|__FE_IMAGE__|$FE_IMAGE|" -e "s|__FE_HOST__|$FE_HOST|" | kubectl apply -f -
  kubectl -n "$NS" rollout status deployment/ailab-frontend --timeout=5m
else
  echo "프론트엔드 이미지가 없어 건너뜀"
fi

step "검증 (admin_be·프론트엔드 연결, 접두어 제한)"
CS_POD=$(running_pod "$NS" app=containerssh-config-server)
kubectl -n "$NS" exec -i "$CS_POD" -- env NAME="${PREFIX}000" PREFIX="$PREFIX" UID_MIN="$UID_MIN" UID_MAX="$UID_MAX" FE="${FE_IMAGE:+http://ailab-frontend}" FE_HOST="$FE_HOST" python - <<'PY'
import base64, json, os, sys, time, requests
api = requests.Session()
api.headers["X-Internal-Token"] = os.environ.get("CONFIG_API_TOKEN", "")
base, name = "http://127.0.0.1:8000", os.environ["NAME"]
lo, hi = int(os.environ["UID_MIN"]), int(os.environ["UID_MAX"])
ok = True
def check(label, cond, detail=""):
    global ok
    print(("OK  " if cond else "NG  ") + label + ("" if cond else f"  ({detail})"))
    ok = ok and cond
was = os.environ.get("ADMIN_BE_INTERNAL_URL", "")
# admin_be를 막 교체한 직후에는 옛 Pod 주소로 가는 연결이 잠시 남을 수 있어 1분까지 기다린다.
err = ""
for _ in range(12):
    try:
        requests.get(f"{was}/api/requests/config/{name}", timeout=5)
        err = ""
        break
    except Exception as e:
        err = type(e).__name__
        time.sleep(5)
check("admin_be(WAS) 응답", not err, err)
# 접두어 없는 이름은 거절돼야 한다. 막히지 않더라도 request_id가 숫자가 아니어서 400으로 끝나 작업은 등록되지 않는다.
# 단, 실운영(operation)은 접두어 강제를 일부러 꺼둬서(PREFIX=) 실사용자가 원하는 이름을 그대로
# 쓰게 했으므로 이 거절 자체가 일어나지 않는다 — 이 스택에서는 검증 대상이 아니다.
if os.environ["PREFIX"]:
    r = api.post(f"{base}/operations/revoke", timeout=30,
                      json={"request_id": "not-a-number", "username": "guardprobe000", "delete_account": True})
    check(f"접두어({os.environ['PREFIX']}) 없는 계정 거절", r.status_code == 403, r.status_code)
else:
    print("OK  접두어 제약 없음 (실사용자 이름 그대로 허용, 검증 대상 아님)")
fe = os.environ.get("FE", "")

def settled(label, fn, ok):
    """프론트엔드 이미지가 바뀐 배포는 새 Pod가 서비스·ingress에 붙기까지 잠시 연결 오류·502가 난다.
    admin_be 응답 확인처럼 1분까지 다시 시도한 뒤 판정한다."""
    detail = ""
    for _ in range(12):
        try:
            code = fn().status_code
            if ok(code):
                check(label, True)
                return
            detail = code
        except Exception as e:
            detail = type(e).__name__
        time.sleep(5)
    check(label, False, detail)

if fe:
    settled("프론트엔드 응답", lambda: requests.get(f"{fe}/", timeout=10), lambda c: c == 200)
    # /api/는 nginx가 이 스택의 admin_be로 넘긴다. 502·504면 대상이 틀렸거나 닿지 않는 것이다.
    settled("프론트엔드 /api/ → 스택 admin_be",
            lambda: requests.get(f"{fe}/api/requests/config/{name}", timeout=10),
            lambda c: c not in (502, 503, 504))
    # 밖에서 들어오는 경로(ingress 컨트롤러, nodePort 30081)에 호스트 규칙이 붙었는지
    settled("30081 호스트 규칙으로 스택 화면",
            lambda: requests.get("http://nginx-ailab-ingress-nginx-controller.ailab-frontend.svc.cluster.local/",
                                 headers={"Host": os.environ["FE_HOST"]}, timeout=10),
            lambda c: c == 200)
# 계정 생성·회수는 아래 비동기 작업 검증에서 확인한다(동기 계정 API는 없앴다).
sys.exit(0 if ok else 1)
PY

# 시험 계정 이름은 리눅스 계정명 규칙(숫자로 시작 불가)을 지켜야 한다. 세 실험 스택은 접두어가
# 글자로 시작해 문제없지만, 실운영(operation)은 접두어를 비워둬서(PREFIX=) 그대로 쓰면 "001"처럼
# 숫자로 시작해 홈 디렉터리 생성 단계에서 (정당하게) 거부된다.
PROBE_NAME="${PREFIX}001"
[ -z "$PREFIX" ] && PROBE_NAME="guardprobe001"

step "검증 (비동기 작업 큐 — 제어기, 테스트 계정 $PROBE_NAME)"
# v2.0 비동기 흐름(POST /operations/provision·revoke가 작업만 등록 → 제어기가 뒤에서 실행)이 실제로
# 도는지, 계정이 스택 UID 대역 안에서 만들어지고 회수되는지 확인한다.
# 작업의 신청 번호는 admin_be 신청 PK와 같은 형식(양의 정수)만 받는다. 실제 신청과 겹치지 않도록
# 현재 시각(초)을 쓴다 — 스택 admin_be의 신청 번호는 1부터 늘어나므로 닿지 않는다.
JOB_RID=$(date +%s)
JOB_RID2=$((JOB_RID + 1))
# 계정 회수는 keytab을 지울 farm 노드를 모르면 보류한다(baseline admin_be와 같은 조건). 회수 작업
# 시험에는 그래서 노드를 하나 실어 보낸다. 공개 로그에 남지 않게 값은 출력하지 않고 넘기기만 한다.
JOB_NODE=$(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -i '^farm' | head -1)
kubectl -n "$NS" exec -i "$CS_POD" -- env NAME="$PROBE_NAME" RID="$JOB_RID" RID2="$JOB_RID2" NODE="$JOB_NODE" UID_MIN="$UID_MIN" UID_MAX="$UID_MAX" python - <<'PY'
import base64, os, sys, time, requests
api = requests.Session()
api.headers["X-Internal-Token"] = os.environ.get("CONFIG_API_TOKEN", "")
base, name = "http://127.0.0.1:8000", os.environ["NAME"]
rid, rid2, node = os.environ["RID"], os.environ["RID2"], os.environ.get("NODE") or None
ok = True
def check(label, cond, detail=""):
    global ok
    print(("OK  " if cond else "NG  ") + label + ("" if cond else f"  ({detail})"))
    ok = ok and cond

def account_line():
    """이 Pod가 쓰는 스택 계정 대장(/kube_share/passwd)에서 시험 계정 행을 찾는다.
    이 스택의 첫 배포라 아직 계정을 하나도 만든 적이 없으면 파일 자체가 없을 수 있다 —
    그때는 당연히 시험 계정도 없는 것이다."""
    try:
        with open("/kube_share/passwd") as f:
            return next((l for l in f if l.split(":", 1)[0] == name), None)
    except FileNotFoundError:
        return None

def account_status():
    return 200 if account_line() else 404

def new_password():
    return base64.b64encode(os.urandom(12).hex().encode()).decode()

def wait(fn, times=24, gap=5):
    """비동기라 결과가 바로 나오지 않는다. 참이 될 때까지 기다렸다가 돌려준다."""
    for _ in range(times):
        value = fn()
        if value:
            return value
        time.sleep(gap)
    return None

if account_status() == 200:  # 이전 실행에서 남은 시험 계정은 회수 작업으로 먼저 정리
    api.post(f"{base}/operations/revoke", timeout=30, json={
        "request_id": str(int(rid) - 1), "username": name, "node_name": node, "delete_account": True})
    wait(lambda: account_status() == 404)

# 회수는 실사용자 홈은 보존하지만, 이 시험 계정은 매번 같은 이름을 재사용한다. 홈을 남겨 두면
# 다음 배포가 다른(다시 계산된) uid를 배정할 때 NAS의 기존 소유자와 어긋나 HOME_OWNER_MISMATCH로
# 막힌다(2026-09-22 실측). 이 계정은 시험 전용이라 매번 지우고 새로 만든다.
import main as _main_mod
with _main_mod.app.app_context():
    try:
        _main_mod.delete_user_home_directory(name)
    except Exception as e:
        print(f"시험 계정 홈 정리 실패(무시하고 계속): {e}")

# 1) 생성 작업. 이 테스트 계정은 admin_be에 사용자 설정이 없어, 계정·홈·principal까지 만든 뒤 설정
#    조회에서 실패한다. 거기서 끝나지 않고 이번 작업이 만든 계정을 제어기가 되돌리는 것까지가 정상이다.
r = api.post(f"{base}/operations/provision", timeout=30, json={
    "request_id": rid, "username": name, "account": {"passwd_base64": new_password()}})
check("작업 등록 (provision) 202", r.status_code == 202, f"{r.status_code} {r.text[:200]}")

def finished():
    body = api.get(f"{base}/operations/provision/{rid}", timeout=10).json()
    return body if body.get("phase") in ("SUCCESS", "FAIL", "UNKNOWN") else None

body = wait(finished) or {}
check("제어기가 작업을 실행함 (끝 상태로 종료)", bool(body), body.get("phase", "시간 초과"))
expected = body.get("phase") == "FAIL" and body.get("error_code") == "USER_CONFIG_NOT_FOUND"
detail = ""
if not expected:
    # result(생성 성공 시 자원)는 실패 시 비어 있다 — 어느 단계에서 왜 실패했는지는 단계 기록(재시도 행의
    # resource_type=단계 이름, error_code)에만 남는다.
    steps = api.get(f"{base}/operations/provision/{rid}/steps", timeout=10).json()
    detail = steps.get("jobs", [{}])[0].get("steps") if steps.get("jobs") else steps
check("사용자 설정 없음으로 실패 (USER_CONFIG_NOT_FOUND)", expected,
      f"{body.get('phase')}/{body.get('error_code')}: {detail}")
# 되돌리기는 노드를 모르면 보류한다(ACCOUNT_NODE_UNKNOWN). 이 시험은 노드가 정해지기 전 단계에서
# 실패하므로 계정은 보류되어 남는 것이 정상이다 — baseline admin_be도 같은 상황에서 삭제하지 않고 알린다.
check("되돌리기 보류 규칙대로 계정이 남음", account_status() == 200, account_status())
line = account_line()
uid = int(line.split(":")[2]) if line else None
lo, hi = int(os.environ["UID_MIN"]), int(os.environ["UID_MAX"])
check(f"UID가 대역 {lo}~{hi} 안", uid is not None and lo <= uid <= hi, uid)

# 2) 회수 작업. 위에서 남은 계정을 회수 작업으로 지운다. 보류 조건에 걸리지 않게 노드를 실어 보낸다.
check("회수에 쓸 farm 노드를 찾음", node is not None, "노드 목록이 비어 있음")
r = api.post(f"{base}/operations/revoke", timeout=30,
                  json={"request_id": rid2, "username": name, "node_name": node, "delete_account": True})
check("작업 등록 (revoke) 202", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
check("제어기가 회수 작업을 실행함 (계정 대장에서 사라짐)",
      wait(lambda: account_status() == 404) is True, account_status())

if account_status() != 404:
    print(f"    참고: 시험 계정 {name}이 계정 대장에 남음 — 다음 배포의 사전 정리에서 다시 회수한다")
sys.exit(0 if ok else 1)
PY

echo "--- admin_be 나가는 연결 (메일만 허용)"
BE_POD=$(running_pod "$NS" app=admin-prod)
# 실험 스택 셋은 Slack(443)이 막혀 있어야 정상이지만, 실운영(operation)은 실제 알림을 보내야 하므로
# 반대로 열려 있어야 정상이다.
if [ "$STACK" = "operation" ]; then
  SLACK_CHECK='t hooks.slack.com 443 && echo "OK  Slack(443) 연결됨" || echo "NG  Slack(443)이 막혀 있음"'
else
  SLACK_CHECK='t hooks.slack.com 443 && echo "NG  Slack(443) 연결이 열려 있음" || echo "OK  Slack(443) 차단"'
fi
NET=$(kubectl -n "$NS" exec "$BE_POD" -- bash -c '
  t() { timeout 6 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }
  t smtp.gmail.com 587 && echo "OK  메일(SMTP 587) 연결됨" || echo "NG  메일(SMTP 587) 연결 안 됨"
  '"$SLACK_CHECK"'
  t my-mysql.ailab-be.svc.cluster.local 3306 && echo "NG  운영 DB 연결이 열려 있음" || echo "OK  운영 DB 차단"
  t kubernetes.default.svc.cluster.local 443 && echo "OK  쿠버네티스 API 연결됨(관리자 Pod 조회)" || echo "NG  쿠버네티스 API 연결 안 됨"')
echo "$NET"
echo "$NET" | grep -q "^NG" && exit 1
SA_REF=system:serviceaccount:$NS:admin-prod
if [ "$(kubectl auth can-i list pods -n "$NS" --as="$SA_REF" 2>/dev/null)" = yes ] \
   && [ "$(kubectl auth can-i get pods/log -n "$NS" --as="$SA_REF" 2>/dev/null)" = yes ]; then
  echo "OK  스택 네임스페이스 Pod·로그 조회 권한 있음"
else
  echo "NG  스택 네임스페이스 Pod·로그 조회 권한 없음"; exit 1
fi
if [ "$(kubectl auth can-i list pods -n "$PROD_NS" --as="$SA_REF" 2>/dev/null)" = yes ]; then
  echo "NG  운영 네임스페이스 Pod 조회 권한이 열려 있음"; exit 1
else
  echo "OK  운영 네임스페이스 Pod 조회 권한 없음"
fi
echo "--- 스택 DB의 작업 이력"
kubectl -n "$NS" exec mysql-0 -- sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" -N -e \"SELECT action, phase FROM operation_state_db.operation_log WHERE request_id IN ('smoke-${PREFIX}000','$JOB_RID','$JOB_RID2') ORDER BY id\" 2>/dev/null" | tail -20
# 실험 스택 접두어가 실운영 계정 대장(bare kube_share root)에 새어 들었는지 보는 확인이다.
# 실운영(operation)은 접두어가 없어(PREFIX=) 빈 문자열이 모든 줄과 매칭돼 뜻이 없으므로 건너뛴다.
if [ -n "$PREFIX" ]; then
  LEAK=$(ledger sh -c "grep -c '^${PREFIX}' /kube_share/passwd || true")
  [ "$LEAK" = 0 ] && echo "OK  운영 계정 대장에 ${PREFIX} 계정 없음" || { echo "NG  운영 계정 대장에 ${PREFIX} 계정 ${LEAK}개"; exit 1; }
else
  echo "OK  접두어 없음 (실운영, 누출 검사 대상 아님)"
fi

step "완료"
echo "네임스페이스      $NS"
echo "config-server     $RELEASE (nodePort $CONFIG_NODEPORT, 이미지 태그 ${IMAGE##*:})"
if [ -n "$FE_IMAGE" ]; then
  echo "화면              http://$FE_HOST:30081"
fi
echo "UID 대역          $UID_MIN~$UID_MAX"
echo "공용 GID 대역     $SHARED_GID_MIN~$SHARED_GID_MAX"
echo "NodePort 대역     $NP_MIN~$NP_MAX"
echo "테스트 계정 접두어 $PREFIX"
echo "VERIFY_MODE       $VERIFY_MODE"
