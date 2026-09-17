#!/usr/bin/env bash
# ailab-operation: 실사용자용 정식 운영 네임스페이스.
#
# baseline/noprobe/full(stack-up.sh)과 이게 다른 점: stack-up.sh는 매번 새 DB·새 계정 장부·새 Redis를
# 만들어서 운영과 완전히 격리시키는 게 목적이다(논문 실험이라 재현 가능해야 하니까). ailab-operation도
# 새 MySQL/Redis Pod는 새로 만들지만(운영 ailab-infra/ailab-be의 기존 StatefulSet은 그대로 꺼진 채로
# 둔다 — 계속 의존하지 않는 독립 배포로 만들려는 것), 그 안의 데이터(web_admin.users 등)는 기존 것을
# 한 번 덤프떠서 옮겨 담는다. 계정 장부(NAS kube_share)는 파일이라 옮길 필요 없이 그대로 직접 가리킨다.
#
#   operation-up.sh <config-server 이미지> <admin_be 이미지>
#
# 프론트엔드는 여기서 안 다룬다 — ailab-frontend의 기존 catch-all Ingress와 충돌해서 별도 처리가
# 필요하기 때문(맨 아래 안내 참고).
#
# 실행 전 전제조건: ailab-be의 my-mysql이 1개 이상으로 떠 있어야 한다(마이그레이션 때만 필요, 끝나면
# 다시 0으로 내려도 됨 — ailab-operation은 그 이후로 이 DB에 의존하지 않는다).
#   kubectl -n ailab-be scale sts my-mysql --replicas=1
set -euo pipefail

IMAGE=${1:?"config-server 이미지(저장소:태그)"}
BE_IMAGE=${2:?"admin_be 이미지(저장소:태그)"}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
if [ -r /etc/kubernetes/ci-deployer.conf ]; then
  export KUBECONFIG=/etc/kubernetes/ci-deployer.conf
fi

NS=ailab-operation
RELEASE=config-server-operation
CONFIG_NODEPORT=30086   # 클러스터 전체 NodePort 조회로 비어있는 걸 확인함 (2026-09-17 기준)
NP_MIN=30100
NP_MAX=31999            # baseline/noprobe/full이 쓰는 32000~32749와 안 겹치게 잡음

PROD_NS=ailab-infra
PROD_RELEASE=containerssh-config-server
PROD_DB_NS=ailab-be     # my-mysql(웹 계정·기준 데이터)이 있는 네임스페이스
PROD_DB_POD=my-mysql-0
PROD_BE_APP_NS=default  # admin-prod-config Secret과 admin_be 본체가 있는 네임스페이스 (DB와 다름)

step() { echo; echo "=== $*"; }
render() { sed -e "s|__NS__|$NS|g" "$@"; }
rnd() { head -c "${1:-16}" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
getpw() { kubectl -n "$NS" get secret stack-db -o jsonpath="{.data.$1}" | base64 -d; }

step "사전 확인 — 운영 config-server 값 읽기 (릴리스에 남아있어 Pod가 없어도 읽힘)"
BASE=$(mktemp); trap 'rm -f "$BASE"' EXIT
helm -n "$PROD_NS" get values "$PROD_RELEASE" -o yaml > "$BASE"
KUBE_SHARE=$(sed -n 's/^[[:space:]]*kubeSharePath:[[:space:]]*//p' "$BASE" | head -1 | tr -d "\"'")
NFS_SERVER=$(awk '/^nfs:/{f=1; next} f && /^[^[:space:]]/{f=0} f && /^[[:space:]]+server:/{sub(/^[[:space:]]+server:[[:space:]]*/, ""); gsub(/["\047]/, ""); print; exit}' "$BASE")
[ -n "$KUBE_SHARE" ] || { echo "운영 kubeSharePath를 찾지 못함"; exit 1; }
[ -n "$NFS_SERVER" ] || { echo "운영 nfs.server를 찾지 못함"; exit 1; }
echo "kube_share=$KUBE_SHARE (스택별 서브폴더 없이 그대로 씀 — baseline 등과 다른 핵심 차이)"

step "nodePort $CONFIG_NODEPORT 사용 가능한지 확인"
TAKEN=$(kubectl get svc -A -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{range .spec.ports[*]}{.nodePort}{" "}{end}{"\n"}{end}' \
  | awk -v ns="$NS" -v p="$CONFIG_NODEPORT" '$1 != ns { for (i = 2; i <= NF; i++) if ($i == p) c++ } END { print c+0 }')
[ "$TAKEN" = 0 ] || { echo "nodePort $CONFIG_NODEPORT를 다른 네임스페이스가 이미 쓰고 있음"; exit 1; }
echo "OK"

step "ailab-be/my-mysql 살아있는지 확인 (마이그레이션에 필요, 없으면 중단)"
READY=$(kubectl -n "$PROD_DB_NS" get sts my-mysql -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
[ "${READY:-0}" -ge 1 ] || { echo "$PROD_DB_NS/my-mysql이 0개로 내려가 있음 — 먼저 'kubectl -n $PROD_DB_NS scale sts my-mysql --replicas=1' 실행 필요, 중단"; exit 1; }
echo "OK"

step "네임스페이스 생성"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

step "사용자 컨테이너 우선순위 등급 (클러스터 전역, 이미 있으면 그대로)"
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

step "SSH 키 복사 ($PROD_NS → $NS)"
for s in nas-ssh-key farm-ssh-key farm-ad-ssh-key; do
  TYPE=$(kubectl -n "$PROD_NS" get secret "$s" -o jsonpath='{.type}')
  {
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\ntype: %s\ndata:\n' "$s" "$TYPE"
    kubectl -n "$PROD_NS" get secret "$s" -o go-template='{{range $k, $v := .data}}  {{$k}}: {{$v}}{{"\n"}}{{end}}'
  } | kubectl -n "$NS" apply -f - >/dev/null
  echo "$s"
done

step "DB 비밀번호"
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
# config-server가 farm·AD·NAS에 접속할 때 상대 서버를 확인하도록, 배포 서버에서 호스트 키를 모아 Secret으로
# 넣는다. 못 받으면 main.py가 StrictHostKeyChecking=no로 폴백해 접속은 되지만 상대 서버 확인이 꺼진다.
KH_CHANGED=0
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

step "MySQL (새 Pod, 이 네임스페이스 전용)"
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
echo "DB 3개와 테이블 준비 완료"

step "Redis (새 Pod, 이 네임스페이스 전용)"
render "$HERE/redis.yaml" | kubectl apply -f -
kubectl -n "$NS" rollout status deployment/redis-bg-master --timeout=5m
kubectl -n "$NS" rollout status deployment/admin-redis --timeout=5m

step "config-server ($RELEASE, $IMAGE)"
if helm -n "$NS" status "$RELEASE" -o json 2>/dev/null | grep -q '"status":"failed"'; then
  helm -n "$NS" uninstall "$RELEASE" --wait >/dev/null && echo "실패한 이전 설치 정리"
fi
helm upgrade --install "$RELEASE" "$ROOT/config-server/Chart" -n "$NS" -f "$BASE" \
  --set namespace="$NS" --set config.namespace="$NS" \
  --set image.repository="${IMAGE%:*}" --set image.tag="${IMAGE##*:}" --set image.pullPolicy=IfNotPresent \
  --set service.nodePort="$CONFIG_NODEPORT" \
  --set nfs.kubeSharePath="$KUBE_SHARE" \
  --set infra.adminBeInternalUrl="http://admin-prod.$NS" \
  --set accounts.uidMin=20000 --set accounts.uidMax=0 --set accounts.prefix= \
  --set nodeport.min="$NP_MIN" --set nodeport.max="$NP_MAX" \
  --set verifyMode=baseline \
  --set redis.host="redis-bg-master.$NS.svc.cluster.local" \
  --set db.host=infra-mysql --set logDb.host=log-mysql \
  --set imageStore.claimName= \
  --set controller.enabled=true \
  --wait --timeout 10m
if [ "$TOKEN_NEW" = 1 ] || [ "$KH_CHANGED" = 1 ]; then
  # 토큰·호스트 키 Secret은 Pod 템플릿에 드러나지 않아 helm 업그레이드만으로는 새 값을 읽지 않는다.
  kubectl -n "$NS" rollout restart deployment/"$RELEASE" deployment/"$RELEASE-controller" >/dev/null
  kubectl -n "$NS" rollout status deployment/"$RELEASE" --timeout=10m
  kubectl -n "$NS" rollout status deployment/"$RELEASE-controller" --timeout=10m
fi

step "admin_be"
kubectl -n "$PROD_BE_APP_NS" get secret admin-prod-config >/dev/null 2>&1 || { echo "운영 admin-prod-config Secret이 없음"; exit 1; }
{
  printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: admin-prod-config\ntype: Opaque\ndata:\n'
  kubectl -n "$PROD_BE_APP_NS" get secret admin-prod-config -o go-template='{{range $k, $v := .data}}  {{$k}}: {{$v}}{{"\n"}}{{end}}'
} | kubectl -n "$NS" apply -f - >/dev/null
PROD_CFG_HASH=$(kubectl -n "$PROD_BE_APP_NS" get secret admin-prod-config -o jsonpath='{.data}' | sha256sum | cut -c1-16)
# 실험 스택과 달리 Slack/메일 주소를 안 덮어쓴다 — 운영 설정(admin-prod-config)에 이미 들어있는 진짜
# 값을 그대로 쓴다. DB·Redis·config-server 주소, 서명키만 이 네임스페이스 자원으로 덮어쓴다.
CONFIG_JSON=$(cat <<EOF
{"spring":{"datasource":{"url":"jdbc:mysql://admin-mysql.$NS.svc.cluster.local:3306/web_admin?serverTimezone=Asia/Seoul&useSSL=false&allowPublicKeyRetrieval=true","username":"admin_user","password":"$(getpw admin_user)"},"data":{"redis":{"host":"admin-redis.$NS.svc.cluster.local","port":6379,"password":"$(getpw admin_redis)"}},"jpa":{"hibernate":{"ddl-auto":"update"}}},"config":{"base-url":"http://containerssh-config-service.$NS.svc.cluster.local","api-token":"$(getpw config_api_token)"},"kubernetes":{"pod-namespace":"$NS"},"jwt":{"secret":"$(getpw jwt_secret)"}}
EOF
)
kubectl -n "$NS" create secret generic admin-be-config --from-literal=SPRING_APPLICATION_JSON="$CONFIG_JSON" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
CONFIG_HASH=$(printf '%s%s' "$CONFIG_JSON" "$PROD_CFG_HASH" | sha256sum | cut -c1-16)
# admin-be.yaml 그대로 쓰되 맨 끝 NetworkPolicy(외부 연결을 DNS·메일만 남기고 차단)만 잘라낸다 —
# 그건 "실험 admin_be가 실수로 운영에 닿지 않게" 막는 안전장치라 ailab-operation(운영 자신)엔 안 맞는다.
# networking.k8s.io/v1는 이 파일에서 그 블록에만 쓰여 안전한 자르는 기준이다.
render "$HERE/admin-be.yaml" | sed -e "s|__ADMIN_IMAGE__|$BE_IMAGE|" -e "s|__CONFIG_HASH__|$CONFIG_HASH|" \
  | sed '/^apiVersion: networking.k8s.io\/v1$/,$d' \
  | kubectl apply -f -
kubectl -n "$NS" rollout status deployment/admin-prod --timeout=10m
echo "admin_be 이미지: ${BE_IMAGE##*[@:]}"

step "기존 데이터 이전 (ailab-be/my-mysql → 이 네임스페이스, 웹 계정·기준 데이터)"
# admin_be가 방금 떠서 ddl-auto로 web_admin 테이블(users 포함)을 막 만든 뒤라 여기서 옮긴다.
# users를 반드시 옮겨야 한다 — 안 옮기면 csuhyeon 등 기존 실사용자가 로그인 자체를 못 함(웹 계정이
# 없으니까). 나머지는 신청 화면에 필요한 기준 데이터. 운영과 스택 간 스키마가 다를 수 있어(ddl-auto가
# 지우지 않은 옛 열 등) 공통 열만 옮긴다.
REF_TABLES="users groups resource_groups nodes gpus container_image resource_group_images message_templates"
smy()  { kubectl -n "$NS" exec mysql-0 -- sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N "$@"' _ "$@" </dev/null; }
smyi() { kubectl -n "$NS" exec -i mysql-0 -- sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot "$@"' _ "$@"; }
smy -e "DROP DATABASE IF EXISTS refdata_src; CREATE DATABASE refdata_src"
kubectl -n "$PROD_DB_NS" exec "$PROD_DB_POD" -- sh -c \
  'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot --single-transaction --no-tablespaces --skip-triggers --set-gtid-purged=OFF "$MYSQL_DATABASE" "$@"' _ $REF_TABLES \
  | smyi refdata_src
for T in $REF_TABLES; do
  COLS=$(smy -e "SELECT GROUP_CONCAT(s.COLUMN_NAME ORDER BY s.ORDINAL_POSITION) FROM information_schema.COLUMNS s
    JOIN information_schema.COLUMNS p ON p.TABLE_SCHEMA='refdata_src' AND p.TABLE_NAME=s.TABLE_NAME AND LOWER(p.COLUMN_NAME)=LOWER(s.COLUMN_NAME)
    WHERE s.TABLE_SCHEMA='web_admin' AND s.TABLE_NAME='$T'")
  [ -n "$COLS" ] && [ "$COLS" != NULL ] || { echo "$T: 공통 열이 없음 (표가 없는지 확인)"; exit 1; }
  UPD=$(echo "$COLS" | tr ',' '\n' | sed 's/.*/&=VALUES(&)/' | paste -sd, -)
  # $T가 SQL 예약어(예: groups)일 수 있어 식별자로 쓸 땐 백틱으로 감싼다.
  smy -e "SET FOREIGN_KEY_CHECKS=0; INSERT INTO web_admin.\`$T\` ($COLS) SELECT $COLS FROM refdata_src.\`$T\` ON DUPLICATE KEY UPDATE $UPD"
  echo "$T: $(smy -e "SELECT COUNT(*) FROM refdata_src.\`$T\`")행 이전 완료"
done
smy -e "DROP DATABASE refdata_src"

echo
echo "=== config-server + admin_be 배포 완료 ==="
echo "다음 단계(프론트엔드): ailab-frontend 네임스페이스의 기존 catch-all Ingress부터 정리한 뒤,"
echo "admin-fe.yaml의 '- host: __FE_HOST__' 줄만 빼고(catch-all로) 별도 적용할 것. 예:"
echo '  render admin-fe.yaml | sed -e "s|__FE_IMAGE__|<이미지>|" -e "s|- host: __FE_HOST__|-|" | kubectl apply -f -'
