#!/usr/bin/env bash
# 제안 시스템 실험 스택(ailab-noprobe / ailab-full)을 한 번에 띄운다. 여러 번 실행해도 결과가 같다.
#
#   stack-up.sh <noprobe|full> <config-server 이미지(저장소:태그)> [프론트엔드 이미지]
#
# admin_infra의 "Deploy Proposed Stack" 워크플로가 배포 서버에서 실행한다. 공개 레포의 Actions 로그에
# 그대로 남으므로 비밀번호, 운영 설정값, 실사용자 계정 이름은 절대 출력하지 않는다(값은 파이프로만 넘김).
set -euo pipefail

STACK=${1:?"스택 이름(noprobe|full)"}
IMAGE=${2:?"config-server 이미지(저장소:태그)"}
FE_IMAGE=${3:-}   # 비우면 프론트엔드를 올리지 않는다
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
[ -r /etc/kubernetes/ci-deployer.conf ] && export KUBECONFIG=/etc/kubernetes/ci-deployer.conf

PROD_NS=ailab-infra
PROD_RELEASE=containerssh-config-server
PROD_BE_NS=default
PROD_DB_NS=ailab-be      # 운영 admin_be의 DB (기준 데이터 복사용, 읽기만)
PROD_DB_POD=my-mysql-0

# 할당 값. 운영(UID 20000대, NodePort 30000~32767 전체, config-server 30082, admin_be 30083)과
# 겹치지 않게 잡았다. 바꿀 때는 docs/환경 구축 가이드의 표도 같이 고친다.
case "$STACK" in
  noprobe) UID_MIN=50000; UID_MAX=54999; NP_MIN=32000; NP_MAX=32249; CONFIG_NODEPORT=30182; PREFIX=exp-np- ;;
  full)    UID_MIN=55000; UID_MAX=59999; NP_MIN=32250; NP_MAX=32499; CONFIG_NODEPORT=30282; PREFIX=exp-fu- ;;
  *) echo "알 수 없는 스택: $STACK"; exit 2 ;;
esac
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
running_pod() { kubectl -n "$1" get pod -l "$2,!job-name" --field-selector=status.phase=Running -o name | head -1; }
getpw() { kubectl -n "$NS" get secret stack-db -o jsonpath="{.data.$1}" | base64 -d; }

step "사전 확인"
PROD_POD=$(running_pod "$PROD_NS" app=containerssh-config-server)
[ -n "$PROD_POD" ] || { echo "운영 config-server Pod를 찾지 못함"; exit 1; }
USED=$(kubectl -n "$PROD_NS" exec "$PROD_POD" -- awk -F: -v lo="$UID_MIN" -v hi="$UID_MAX" '$3>=lo && $3<=hi {n++} END {print n+0}' /kube_share/passwd)
[ "$USED" = 0 ] || { echo "운영 계정 대장에 UID $UID_MIN~$UID_MAX 계정이 ${USED}개 있음. 대역을 옮겨야 함"; exit 1; }
for PORT in "$CONFIG_NODEPORT"; do
  TAKEN=$(kubectl get svc -A -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{range .spec.ports[*]}{.nodePort}{" "}{end}{"\n"}{end}' \
    | awk -v ns="$NS" -v p="$PORT" '$1 != ns { for (i = 2; i <= NF; i++) if ($i == p) c++ } END { print c+0 }')
  [ "$TAKEN" = 0 ] || { echo "nodePort $PORT를 다른 네임스페이스가 쓰고 있음"; exit 1; }
done
HOST_TAKEN=$(kubectl get ing -A -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{range .spec.rules[*]}{.host}{" "}{end}{"\n"}{end}' \
  | awk -v ns="$NS" -v h="$FE_HOST" '$1 != ns { for (i = 2; i <= NF; i++) if ($i == h) c++ } END { print c+0 }')
[ "$HOST_TAKEN" = 0 ] || { echo "ingress 호스트 $FE_HOST를 다른 네임스페이스가 쓰고 있음"; exit 1; }
echo "UID $UID_MIN~$UID_MAX 비어 있음, config-server nodePort $CONFIG_NODEPORT 사용 가능, 화면 호스트 $FE_HOST 사용 가능"

step "네임스페이스 $NS"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$NS" ailab.dgu/proposed-stack="$STACK" --overwrite >/dev/null

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

step "계정 대장 경로"
# config-server는 nfs.kubeSharePath를 /kube_share로 마운트해 passwd/group/shadow를 둔다. 운영 경로의
# 하위 디렉터리를 스택 전용으로 쓰므로 테스트 계정이 운영 대장에 섞이지 않는다. 비어 있으면
# config-server가 처음 계정을 만들 때 기본 파일로 채운다.
kubectl -n "$PROD_NS" exec "$PROD_POD" -- mkdir -p "/kube_share/exp-$STACK"
echo "/kube_share/exp-$STACK"

step "config-server ($RELEASE, $IMAGE)"
BASE=$(mktemp); trap 'rm -f "$BASE"' EXIT
# NFS·NAS·Kerberos·farm 노드 설정은 운영 릴리스 값을 그대로 쓰고, 스택마다 달라야 하는 값만 덮어쓴다.
helm -n "$PROD_NS" get values "$PROD_RELEASE" -o yaml > "$BASE"
# 첫 설치가 실패한 릴리스는 upgrade가 받지 않을 수 있으므로 지우고 다시 설치한다.
if helm -n "$NS" status "$RELEASE" -o json 2>/dev/null | grep -q '"status":"failed"'; then
  helm -n "$NS" uninstall "$RELEASE" --wait >/dev/null && echo "실패한 이전 설치 정리"
fi
KUBE_SHARE=$(sed -n 's/^[[:space:]]*kubeSharePath:[[:space:]]*//p' "$BASE" | head -1 | tr -d "\"'")
[ -n "$KUBE_SHARE" ] || { echo "운영 kubeSharePath를 찾지 못함"; exit 1; }
helm upgrade --install "$RELEASE" "$ROOT/config-server/Chart" -n "$NS" -f "$BASE" \
  --set namespace="$NS" --set config.namespace="$NS" \
  --set image.repository="${IMAGE%:*}" --set image.tag="${IMAGE##*:}" --set image.pullPolicy=IfNotPresent \
  --set service.nodePort="$CONFIG_NODEPORT" \
  --set nfs.kubeSharePath="$KUBE_SHARE/exp-$STACK" \
  --set infra.adminBeInternalUrl="http://admin-prod.$NS" \
  --set accounts.uidMin="$UID_MIN" --set accounts.uidMax="$UID_MAX" --set accounts.prefix="$PREFIX" \
  --set nodeport.min="$NP_MIN" --set nodeport.max="$NP_MAX" \
  --set verifyMode="$STACK" \
  --set redis.host="redis-bg-master.$NS.svc.cluster.local" \
  --set db.host=infra-mysql --set logDb.host=log-mysql \
  --set imageStore.claimName= \
  --wait --timeout 10m

step "admin_be"
ADMIN_IMAGE=$(kubectl -n "$PROD_BE_NS" get pod -l app=admin-prod --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}')
ADMIN_IMAGE=${ADMIN_IMAGE#docker-pullable://}
[ -n "$ADMIN_IMAGE" ] || { echo "운영 admin_be 이미지를 찾지 못함"; exit 1; }
# 운영 admin_be는 설정 파일을 이미지가 아니라 admin-prod-config Secret으로 받는다. 같은 파일을 복사해
# 같은 위치에 넣고, 운영 자원을 가리키는 값만 아래 SPRING_APPLICATION_JSON으로 덮어쓴다.
kubectl -n "$PROD_BE_NS" get secret admin-prod-config >/dev/null 2>&1 || { echo "운영 admin-prod-config Secret이 없음"; exit 1; }
{
  printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: admin-prod-config\ntype: Opaque\ndata:\n'
  kubectl -n "$PROD_BE_NS" get secret admin-prod-config -o go-template='{{range $k, $v := .data}}  {{$k}}: {{$v}}{{"\n"}}{{end}}'
} | kubectl -n "$NS" apply -f - >/dev/null
PROD_CFG_HASH=$(kubectl -n "$PROD_BE_NS" get secret admin-prod-config -o jsonpath='{.data}' | sha256sum | cut -c1-16)
# 운영 설정 중 운영 자원을 가리키는 것은 모두 여기서 덮어쓴다. Slack은 닿지 않는 주소로 돌리고,
# 메일(가입 인증 코드, 만료 안내)은 운영 설정 그대로 보낸다. 서명키는 새로 줘서 운영에서 발급한 토큰이
# 여기서 통하지 않게 한다.
SINK=http://127.0.0.1:9/
CONFIG_JSON=$(cat <<EOF
{"spring":{"datasource":{"url":"jdbc:mysql://admin-mysql.$NS.svc.cluster.local:3306/web_admin?serverTimezone=Asia/Seoul&useSSL=false&allowPublicKeyRetrieval=true","username":"admin_user","password":"$(getpw admin_user)"},"data":{"redis":{"host":"admin-redis.$NS.svc.cluster.local","port":6379,"password":"$(getpw admin_redis)"}},"jpa":{"hibernate":{"ddl-auto":"update"}}},"config":{"base-url":"http://containerssh-config-service.$NS.svc.cluster.local"},"slack-webhook-url":{"error-log":"$SINK","noti":"$SINK","farm-admin":"$SINK","lab-admin":"$SINK"},"slack":{"bot-token":"disabled"},"prometheus":{"base-url":"http://127.0.0.1:9"},"jwt":{"secret":"$(getpw jwt_secret)"}}
EOF
)
kubectl -n "$NS" create secret generic admin-be-config --from-literal=SPRING_APPLICATION_JSON="$CONFIG_JSON" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
CONFIG_HASH=$(printf '%s%s' "$CONFIG_JSON" "$PROD_CFG_HASH" | sha256sum | cut -c1-16)
render "$HERE/admin-be.yaml" | sed -e "s|__ADMIN_IMAGE__|$ADMIN_IMAGE|" -e "s|__CONFIG_HASH__|$CONFIG_HASH|" | kubectl apply -f -
kubectl -n "$NS" rollout status deployment/admin-prod --timeout=10m
kubectl -n "$NS" rollout status deployment/redis-bg-master --timeout=5m
kubectl -n "$NS" rollout status deployment/admin-redis --timeout=5m
echo "admin_be 이미지: ${ADMIN_IMAGE##*@}"

step "기준 데이터 복사 (운영 admin DB → 스택)"
# 신청 화면에 필요한 서버·자원 그룹·노드·GPU·이미지와 메일 문구만 복사한다. 사용자·신청은 운영 개인정보이고,
# 그룹은 운영 GID에 묶여 있어 복사하지 않는다. 운영 DB는 읽기만 한다.
# 운영 DB에는 ddl-auto가 지우지 않은 옛 열이 남아 있을 수 있어, 임시 DB에 그대로 받은 뒤 공통 열만 옮긴다.
REF_TABLES="resource_groups nodes gpus container_image resource_group_images message_templates"
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
  [ -n "$COLS" ] && [ "$COLS" != NULL ] || { echo "$T: 운영과 스택에 공통 열이 없음 (표가 없는지 확인)"; exit 1; }
  SKIP=$(smy -e "SELECT IFNULL(GROUP_CONCAT(p.COLUMN_NAME), '-') FROM information_schema.COLUMNS p
    LEFT JOIN information_schema.COLUMNS s ON s.TABLE_SCHEMA='web_admin' AND s.TABLE_NAME=p.TABLE_NAME AND LOWER(s.COLUMN_NAME)=LOWER(p.COLUMN_NAME)
    WHERE p.TABLE_SCHEMA='refdata_src' AND p.TABLE_NAME='$T' AND s.COLUMN_NAME IS NULL")
  UPD=$(echo "$COLS" | tr ',' '\n' | sed 's/.*/&=VALUES(&)/' | paste -sd, -)
  smy -e "SET FOREIGN_KEY_CHECKS=0; INSERT INTO web_admin.$T ($COLS) SELECT $COLS FROM refdata_src.$T ON DUPLICATE KEY UPDATE $UPD"
  echo "$T: 운영 $(smy -e "SELECT COUNT(*) FROM refdata_src.$T")행 → 스택 $(smy -e "SELECT COUNT(*) FROM web_admin.$T")행 (운영에만 있는 열: $SKIP)"
done
smy -e "DROP DATABASE refdata_src"

step "프론트엔드"
if [ -n "$FE_IMAGE" ]; then
  render "$HERE/admin-fe.yaml" | sed -e "s|__FE_IMAGE__|$FE_IMAGE|" -e "s|__FE_HOST__|$FE_HOST|" | kubectl apply -f -
  kubectl -n "$NS" rollout status deployment/ailab-frontend --timeout=5m
else
  echo "프론트엔드 이미지가 없어 건너뜀"
fi

step "검증 (테스트 계정 ${PREFIX}000 생성 후 삭제)"
CS_POD=$(running_pod "$NS" app=containerssh-config-server)
kubectl -n "$NS" exec -i "$CS_POD" -- env NAME="${PREFIX}000" PREFIX="$PREFIX" UID_MIN="$UID_MIN" UID_MAX="$UID_MAX" FE="${FE_IMAGE:+http://ailab-frontend}" FE_HOST="$FE_HOST" python - <<'PY'
import base64, os, sys, time, requests
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
# 접두어 없는 이름은 거절돼야 한다. 이 이름은 스택 계정 대장에 없어서, 막히지 않더라도 404로 끝나고 아무것도 건드리지 않는다.
r = requests.delete(f"{base}/accounts/users/guardprobe000", timeout=30)
check(f"접두어({os.environ['PREFIX']}) 없는 계정 거절", r.status_code == 403, r.status_code)
fe = os.environ.get("FE", "")
if fe:
    try:
        r = requests.get(f"{fe}/", timeout=10)
        check("프론트엔드 응답", r.status_code == 200, r.status_code)
        # /api/는 nginx가 이 스택의 admin_be로 넘긴다. 502·504면 대상이 틀렸거나 닿지 않는 것이다.
        r = requests.get(f"{fe}/api/requests/config/{name}", timeout=10)
        check("프론트엔드 /api/ → 스택 admin_be", r.status_code not in (502, 503, 504), r.status_code)
        # 밖에서 들어오는 경로(ingress 컨트롤러, nodePort 30081)에 호스트 규칙이 붙었는지
        r = requests.get("http://nginx-ailab-ingress-nginx-controller.ailab-frontend.svc.cluster.local/",
                         headers={"Host": os.environ["FE_HOST"]}, timeout=10)
        check("30081 호스트 규칙으로 스택 화면", r.status_code == 200, r.status_code)
    except Exception as e:
        check("프론트엔드 응답", False, type(e).__name__)
# 계정 삭제는 계정·홈을 먼저 지우고, 노드를 지정하지 않으면 모든 farm 노드를 차례로 돌며 Kerberos 키를 지운다.
# 느린 노드가 있으면 이 뒷부분이 수 분 걸리므로 응답은 30초만 기다리고, 계정 대장에서 사라졌는지로 판정한다.
# 이 테스트 계정은 Pod를 만들지 않아 farm 노드에 키가 배포되지 않는다.
def delete_account():
    try:
        return requests.delete(f"{base}/accounts/users/{name}", timeout=30).status_code
    except requests.exceptions.ReadTimeout:
        return "응답 대기 30초 초과(Kerberos 정리 진행 중)"
delete_account()  # 이전 실행에서 남은 것이 있으면 정리
r = requests.put(f"{base}/accounts/users", timeout=120, json={
    "request_id": f"smoke-{name}", "name": name, "passwd_base64": base64.b64encode(os.urandom(12).hex().encode()).decode(),
    "gecos": "stack smoke test", "primary_group_name": name, "enable_sudo": False, "supplementary_groups": []})
uid = (r.json().get("user") or {}).get("uid") if r.status_code == 201 else None
check("계정 생성", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
check(f"UID가 대역 {lo}~{hi} 안", uid is not None and lo <= int(uid) <= hi, uid)
res = delete_account()
gone = requests.get(f"{base}/accounts/users/{name}", timeout=10).status_code == 404
check("계정 삭제 (계정 대장에서 사라짐)", gone, res)
if gone and res != 200:
    print(f"    참고: 삭제 응답 {res}")
sys.exit(0 if ok else 1)
PY
echo "--- admin_be 나가는 연결 (메일만 허용)"
BE_POD=$(running_pod "$NS" app=admin-prod)
NET=$(kubectl -n "$NS" exec "$BE_POD" -- bash -c '
  t() { timeout 6 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }
  t smtp.gmail.com 587 && echo "OK  메일(SMTP 587) 연결됨" || echo "NG  메일(SMTP 587) 연결 안 됨"
  t hooks.slack.com 443 && echo "NG  Slack(443) 연결이 열려 있음" || echo "OK  Slack(443) 차단"
  t my-mysql.ailab-be.svc.cluster.local 3306 && echo "NG  운영 DB 연결이 열려 있음" || echo "OK  운영 DB 차단"')
echo "$NET"
echo "$NET" | grep -q "^NG" && exit 1
echo "--- 스택 DB의 작업 이력"
kubectl -n "$NS" exec mysql-0 -- sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" -N -e \"SELECT action, phase FROM operation_state_db.operation_log WHERE request_id='smoke-${PREFIX}000' ORDER BY id\" 2>/dev/null" | tail -20
LEAK=$(kubectl -n "$PROD_NS" exec "$PROD_POD" -- sh -c "grep -c '^${PREFIX}' /kube_share/passwd || true")
[ "$LEAK" = 0 ] && echo "OK  운영 계정 대장에 ${PREFIX} 계정 없음" || { echo "NG  운영 계정 대장에 ${PREFIX} 계정 ${LEAK}개"; exit 1; }

step "완료"
echo "네임스페이스      $NS"
echo "config-server     $RELEASE (nodePort $CONFIG_NODEPORT, 이미지 태그 ${IMAGE##*:})"
[ -n "$FE_IMAGE" ] && echo "화면              http://$FE_HOST:30081"
echo "UID 대역          $UID_MIN~$UID_MAX"
echo "NodePort 대역     $NP_MIN~$NP_MAX"
echo "테스트 계정 접두어 $PREFIX"
echo "VERIFY_MODE       $STACK"
