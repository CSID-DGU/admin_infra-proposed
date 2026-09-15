#!/usr/bin/env bash
# 실험 스택을 내린다.
#
#   stack-down.sh <baseline|noprobe|full>
#
# 네임스페이스를 지우기 전에 이 스택이 만든 계정(접두어로 구분)과 컨테이너를 회수 작업으로 먼저 정리한다.
# AD principal·farm keytab은 운영과 공유하는 자원이라 네임스페이스를 지워도 사라지지 않는다.
# 홈 디렉터리는 회수 작업의 규칙대로 보존한다.
set -euo pipefail

STACK=${1:?"스택 이름(baseline|noprobe|full)"}
[ -r /etc/kubernetes/ci-deployer.conf ] && export KUBECONFIG=/etc/kubernetes/ci-deployer.conf
case "$STACK" in
  baseline) PREFIX=exp-bl- ;;
  noprobe) PREFIX=exp-np- ;;
  full)    PREFIX=exp-fu- ;;
  *) echo "알 수 없는 스택: $STACK"; exit 2 ;;
esac
NS=ailab-$STACK
RELEASE=config-server-$STACK

if ! kubectl get namespace "$NS" >/dev/null 2>&1; then
  echo "$NS 없음"; exit 0
fi

echo "=== 계정·컨테이너 회수 (${PREFIX}*)"
CS_POD=$(kubectl -n "$NS" get pod -l "app=containerssh-config-server,!job-name" --field-selector=status.phase=Running -o name | head -1)
if [ -n "$CS_POD" ]; then
  # 컨테이너가 떠 있는 계정은 그 컨테이너 회수와 함께 계정을 지운다(keytab 노드를 알 수 있다).
  # 컨테이너가 없는 계정은 keytab이 배포되지 않았을 가능성이 커 farm 노드 하나를 지정해 보류 없이 지운다.
  PODS=$(kubectl -n "$NS" get pods -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep '^ailab-' || true)
  FARM_NODE=$(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | grep -i '^farm' | head -1)
  kubectl -n "$NS" exec -i "$CS_POD" -- env PREFIX="$PREFIX" PODS="$PODS" NODE="$FARM_NODE" python - <<'PY'
import os, time, requests
base, prefix, node = "http://127.0.0.1:8000", os.environ["PREFIX"], os.environ.get("NODE") or None
pods = [p for p in os.environ.get("PODS", "").split() if p]
names = [l.split(":", 1)[0] for l in open("/kube_share/passwd") if l.startswith(prefix)]
rid = int(time.time()) * 10   # 작업 신청 번호(양의 정수). admin_be 신청 번호(1부터)와 겹치지 않는다.
jobs = []

def register(body):
    global rid
    rid += 1
    r = requests.post(f"{base}/operations/revoke", json={"request_id": rid, **body}, timeout=30)
    if r.status_code == 202:
        jobs.append(str(rid))
    else:
        print(f"등록 실패 {r.status_code}: {r.text[:120]}")

for name in names:
    own = [p for p in pods if p.startswith(f"ailab-{name}-")]
    for extra in own[1:]:
        register({"pod_name": extra})
    if own:
        register({"pod_name": own[0], "delete_account": True})
    else:
        register({"username": name, "node_name": node, "delete_account": True})

deadline, results = time.time() + 900, {}
while jobs and time.time() < deadline:
    for j in list(jobs):
        phase = requests.get(f"{base}/operations/revoke/{j}", timeout=10).json().get("phase")
        if phase in ("SUCCESS", "FAIL", "UNKNOWN"):
            results[j] = phase
            jobs.remove(j)
    time.sleep(5)
failed = sum(1 for p in results.values() if p != "SUCCESS")
print(f"계정 {len(names)}개, 회수 작업 {len(results) + len(jobs)}개 — 실패 {failed}개, 시간 초과 {len(jobs)}개")
PY
else
  echo "config-server가 떠 있지 않아 계정을 정리하지 못함. AD·farm에 ${PREFIX} 계정이 남아 있을 수 있음"
fi

echo "=== helm 릴리스와 네임스페이스 삭제"
helm -n "$NS" uninstall "$RELEASE" --wait 2>/dev/null || true
helm -n "$NS" uninstall pvc-image-store --wait 2>/dev/null || true
kubectl delete namespace "$NS" --wait --timeout=10m
echo "완료. 이미지 저장 PV는 Retain 정책이라 남는다(kubectl get pv | grep $NS)."
