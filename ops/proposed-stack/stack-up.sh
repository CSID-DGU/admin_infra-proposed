#!/usr/bin/env bash
# 제안 시스템 실험 스택(ailab-noprobe / ailab-full)을 한 번에 띄운다. 여러 번 실행해도 결과가 같다.
#
#   stack-up.sh <noprobe|full> <config-server 이미지(저장소:태그)>
#
# admin_infra의 "Deploy Proposed Stack" 워크플로가 배포 서버에서 실행한다. 공개 레포의 Actions 로그에
# 그대로 남으므로 비밀번호, 운영 설정값, 실사용자 계정 이름은 절대 출력하지 않는다(값은 파이프로만 넘김).
set -euo pipefail

STACK=${1:?"스택 이름(noprobe|full)"}
IMAGE=${2:?"config-server 이미지(저장소:태그)"}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
[ -r /etc/kubernetes/ci-deployer.conf ] && export KUBECONFIG=/etc/kubernetes/ci-deployer.conf

PROD_NS=ailab-infra
PROD_RELEASE=containerssh-config-server
PROD_BE_NS=default

# 할당 값. 운영(UID 20000대, NodePort 30000~32767 전체, config-server 30082, admin_be 30083)과
# 겹치지 않게 잡았다. 바꿀 때는 docs/환경 구축 가이드의 표도 같이 고친다.
case "$STACK" in
  noprobe) UID_MIN=50000; UID_MAX=54999; NP_MIN=32000; NP_MAX=32249; CONFIG_NODEPORT=30182; PREFIX=exp-np- ;;
  full)    UID_MIN=55000; UID_MAX=59999; NP_MIN=32250; NP_MAX=32499; CONFIG_NODEPORT=30282; PREFIX=exp-fu- ;;
  *) echo "알 수 없는 스택: $STACK"; exit 2 ;;
esac
NS=ailab-$STACK
RELEASE=config-server-$STACK

step() { echo; echo "=== $*"; }
render() { sed -e "s|__NS__|$NS|g" "$@"; }
rnd() { head -c "${1:-16}" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
running_pod() { kubectl -n "$1" get pod -l "$2" --field-selector=status.phase=Running -o name | head -1; }
getpw() { kubectl -n "$NS" get secret stack-db -o jsonpath="{.data.$1}" | base64 -d; }

step "사전 확인"
PROD_POD=$(running_pod "$PROD_NS" app=containerssh-config-server)
[ -n "$PROD_POD" ] || { echo "운영 config-server Pod를 찾지 못함"; exit 1; }
USED=$(kubectl -n "$PROD_NS" exec "$PROD_POD" -- awk -F: -v lo="$UID_MIN" -v hi="$UID_MAX" '$3>=lo && $3<=hi {n++} END {print n+0}' /kube_share/passwd)
[ "$USED" = 0 ] || { echo "운영 계정 대장에 UID $UID_MIN~$UID_MAX 계정이 ${USED}개 있음. 대역을 옮겨야 함"; exit 1; }
TAKEN=$(kubectl get svc -A -o jsonpath='{range .items[*]}{.metadata.namespace}{" "}{range .spec.ports[*]}{.nodePort}{" "}{end}{"\n"}{end}' \
  | awk -v ns="$NS" -v p="$CONFIG_NODEPORT" '$1 != ns { for (i = 2; i <= NF; i++) if ($i == p) c++ } END { print c+0 }')
[ "$TAKEN" = 0 ] || { echo "nodePort $CONFIG_NODEPORT를 다른 네임스페이스가 쓰고 있음"; exit 1; }
echo "UID $UID_MIN~$UID_MAX 비어 있음, config-server nodePort $CONFIG_NODEPORT 사용 가능"

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

step "이미지 저장 PVC"
helm upgrade --install pvc-image-store "$ROOT/pvc-image-chart" -n "$NS" --set config.namespace="$NS"

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
KUBE_SHARE=$(sed -n 's/^[[:space:]]*kubeSharePath:[[:space:]]*//p' "$BASE" | head -1 | tr -d "\"'")
[ -n "$KUBE_SHARE" ] || { echo "운영 kubeSharePath를 찾지 못함"; exit 1; }
helm upgrade --install "$RELEASE" "$ROOT/config-server/Chart" -n "$NS" -f "$BASE" \
  --set namespace="$NS" --set config.namespace="$NS" \
  --set image.repository="${IMAGE%:*}" --set image.tag="${IMAGE##*:}" --set image.pullPolicy=IfNotPresent \
  --set service.nodePort="$CONFIG_NODEPORT" \
  --set nfs.kubeSharePath="$KUBE_SHARE/exp-$STACK" \
  --set infra.adminBeInternalUrl="http://admin-prod.$NS" \
  --set accounts.uidMin="$UID_MIN" --set accounts.uidMax="$UID_MAX" \
  --set nodeport.min="$NP_MIN" --set nodeport.max="$NP_MAX" \
  --set verifyMode="$STACK" \
  --set redis.host="redis-bg-master.$NS.svc.cluster.local" \
  --set db.host=infra-mysql --set logDb.host=log-mysql \
  --wait --timeout 10m

step "admin_be"
ADMIN_IMAGE=$(kubectl -n "$PROD_BE_NS" get pod -l app=admin-prod --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}')
ADMIN_IMAGE=${ADMIN_IMAGE#docker-pullable://}
[ -n "$ADMIN_IMAGE" ] || { echo "운영 admin_be 이미지를 찾지 못함"; exit 1; }
# 운영 이미지에 들어 있는 설정 중 운영 자원을 가리키는 것은 모두 여기서 덮어쓴다. 알림(Slack·메일)은
# 닿지 않는 주소로 돌리고, 서명키도 새로 줘서 운영에서 발급한 토큰이 여기서 통하지 않게 한다.
SINK=http://127.0.0.1:9/
CONFIG_JSON=$(cat <<EOF
{"spring":{"datasource":{"url":"jdbc:mysql://admin-mysql.$NS.svc.cluster.local:3306/web_admin?serverTimezone=Asia/Seoul&useSSL=false&allowPublicKeyRetrieval=true","username":"admin_user","password":"$(getpw admin_user)"},"data":{"redis":{"host":"admin-redis.$NS.svc.cluster.local","port":6379,"password":"$(getpw admin_redis)"}},"mail":{"host":"127.0.0.1","port":2525,"username":"disabled","password":"disabled"},"jpa":{"hibernate":{"ddl-auto":"update"}}},"config":{"base-url":"http://containerssh-config-service.$NS.svc.cluster.local"},"slack-webhook-url":{"error-log":"$SINK","noti":"$SINK","farm-admin":"$SINK","lab-admin":"$SINK"},"slack":{"bot-token":"disabled"},"prometheus":{"base-url":"http://127.0.0.1:9"},"jwt":{"secret":"$(getpw jwt_secret)"}}
EOF
)
kubectl -n "$NS" create secret generic admin-be-config --from-literal=SPRING_APPLICATION_JSON="$CONFIG_JSON" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
CONFIG_HASH=$(printf '%s' "$CONFIG_JSON" | sha256sum | cut -c1-16)
render "$HERE/admin-be.yaml" | sed -e "s|__ADMIN_IMAGE__|$ADMIN_IMAGE|" -e "s|__CONFIG_HASH__|$CONFIG_HASH|" | kubectl apply -f -
kubectl -n "$NS" rollout status deployment/admin-prod --timeout=10m
kubectl -n "$NS" rollout status deployment/redis-bg-master --timeout=5m
kubectl -n "$NS" rollout status deployment/admin-redis --timeout=5m
echo "admin_be 이미지: ${ADMIN_IMAGE##*@}"

step "검증 (테스트 계정 ${PREFIX}000 생성 후 삭제)"
CS_POD=$(running_pod "$NS" app=containerssh-config-server)
kubectl -n "$NS" exec -i "$CS_POD" -- env NAME="${PREFIX}000" UID_MIN="$UID_MIN" UID_MAX="$UID_MAX" python - <<'PY'
import base64, os, sys, requests
base, name = "http://127.0.0.1:8000", os.environ["NAME"]
lo, hi = int(os.environ["UID_MIN"]), int(os.environ["UID_MAX"])
ok = True
def check(label, cond, detail=""):
    global ok
    print(("OK  " if cond else "NG  ") + label + ("" if cond else f"  ({detail})"))
    ok = ok and cond
was = os.environ.get("ADMIN_BE_INTERNAL_URL", "")
try:
    r = requests.get(f"{was}/api/requests/config/{name}", timeout=10)
    check("admin_be(WAS) 응답", True, r.status_code)
except Exception as e:
    check("admin_be(WAS) 응답", False, type(e).__name__)
requests.delete(f"{base}/accounts/users/{name}", timeout=120)  # 이전 실행에서 남은 것이 있으면 정리
r = requests.put(f"{base}/accounts/users", timeout=120, json={
    "request_id": f"smoke-{name}", "name": name, "passwd_base64": base64.b64encode(os.urandom(12)).decode(),
    "gecos": "stack smoke test", "primary_group_name": name, "enable_sudo": False, "supplementary_groups": []})
uid = (r.json().get("user") or {}).get("uid") if r.status_code == 201 else None
check("계정 생성", r.status_code == 201, r.status_code)
check(f"UID가 대역 {lo}~{hi} 안", uid is not None and lo <= int(uid) <= hi, uid)
r = requests.delete(f"{base}/accounts/users/{name}", timeout=120)
check("계정 삭제", r.status_code == 200, r.status_code)
sys.exit(0 if ok else 1)
PY
echo "--- 스택 DB의 작업 이력"
kubectl -n "$NS" exec mysql-0 -- sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" -N -e \"SELECT action, phase FROM operation_state_db.operation_log WHERE request_id='smoke-${PREFIX}000' ORDER BY id\" 2>/dev/null" | tail -20
LEAK=$(kubectl -n "$PROD_NS" exec "$PROD_POD" -- sh -c "grep -c '^${PREFIX}' /kube_share/passwd || true")
[ "$LEAK" = 0 ] && echo "OK  운영 계정 대장에 ${PREFIX} 계정 없음" || { echo "NG  운영 계정 대장에 ${PREFIX} 계정 ${LEAK}개"; exit 1; }

step "완료"
echo "네임스페이스      $NS"
echo "config-server     $RELEASE (nodePort $CONFIG_NODEPORT, 이미지 태그 ${IMAGE##*:})"
echo "UID 대역          $UID_MIN~$UID_MAX"
echo "NodePort 대역     $NP_MIN~$NP_MAX"
echo "테스트 계정 접두어 $PREFIX"
echo "VERIFY_MODE       $STACK"
