#!/usr/bin/env bash
# 실험 스택을 내린다.
#
#   stack-down.sh <noprobe|full>
#
# 네임스페이스를 지우기 전에 이 스택이 만든 테스트 계정(접두어로 구분)을 config-server API로 먼저 지운다.
# AD principal·NAS 홈·farm keytab은 운영과 공유하는 자원이라 네임스페이스를 지워도 사라지지 않는다.
set -euo pipefail

STACK=${1:?"스택 이름(noprobe|full)"}
[ -r /etc/kubernetes/ci-deployer.conf ] && export KUBECONFIG=/etc/kubernetes/ci-deployer.conf
case "$STACK" in
  noprobe) PREFIX=exp-np- ;;
  full)    PREFIX=exp-fu- ;;
  *) echo "알 수 없는 스택: $STACK"; exit 2 ;;
esac
NS=ailab-$STACK
RELEASE=config-server-$STACK

if ! kubectl get namespace "$NS" >/dev/null 2>&1; then
  echo "$NS 없음"; exit 0
fi

echo "=== 테스트 계정 정리 (${PREFIX}*)"
CS_POD=$(kubectl -n "$NS" get pod -l "app=containerssh-config-server,!job-name" --field-selector=status.phase=Running -o name | head -1)
if [ -n "$CS_POD" ]; then
  kubectl -n "$NS" exec -i "$CS_POD" -- env PREFIX="$PREFIX" python - <<'PY'
import os, requests
prefix, n, failed = os.environ["PREFIX"], 0, 0
names = [l.split(":", 1)[0] for l in open("/kube_share/passwd") if l.startswith(prefix)]
for name in names:
    r = requests.delete(f"http://127.0.0.1:8000/accounts/users/{name}", timeout=120)
    n += 1
    failed += r.status_code not in (200, 404)
print(f"삭제 시도 {n}개, 실패 {failed}개")
PY
else
  echo "config-server가 떠 있지 않아 테스트 계정을 정리하지 못함. AD·NAS에 ${PREFIX} 계정이 남아 있을 수 있음"
fi

echo "=== helm 릴리스와 네임스페이스 삭제"
helm -n "$NS" uninstall "$RELEASE" --wait 2>/dev/null || true
helm -n "$NS" uninstall pvc-image-store --wait 2>/dev/null || true
kubectl delete namespace "$NS" --wait --timeout=10m
echo "완료. 이미지 저장 PV는 Retain 정책이라 남는다(kubectl get pv | grep $NS)."
