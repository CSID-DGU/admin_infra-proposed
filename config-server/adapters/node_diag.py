"""노드 커널 오류 로그 수집 — 그 노드에 일회용 Pod를 띄워 호스트의 /var/log를 읽기 전용으로 마운트하고
GPU 드라이버 관련 줄만 꺼낸다.

특권 없이 읽기 위해 dmesg 대신 호스트가 이미 파일로 남겨 둔 kern.log를 읽는다(컨테이너의 dmesg 와
/dev/kmsg 는 CAP_SYSLOG 가 없어 막힌다). 노드에 그 파일이 없으면 없다고 돌려준다(없는 것을 비어 있는 것으로
치지 않는다)."""
import time

from kubernetes import client

_POLL_SEC = 2
_TIMEOUT_SEC = 60
# 현재 파일은 logrotate 직후 거의 비어 있을 수 있어 직전 파일(kern.log.1)도 함께 본다. 파일 전체에서 찾고
# 마지막 200줄만 남긴다. 파일이 없거나 GPU 줄이 없으면 그렇게 적는다(빈 출력을 "오류 없음"으로 읽지 않게).
_SCRIPT = (
    "o=/tmp/gpu-lines; : > $o; found=0; "
    "for f in /host-log/kern.log.1 /host-log/kern.log; do "
    "if [ -r \"$f\" ]; then found=1; grep -iE 'nvrm|xid|nvidia|fallen off the bus' \"$f\" >> $o || true; fi; done; "
    "if [ $found = 0 ]; then echo 'kern.log not readable on this node'; "
    "elif [ -s $o ]; then tail -n 200 $o; else echo 'no GPU-related lines in kern.log or kern.log.1'; fi"
)


def _pod(image, node):
    return {
        "metadata": {"generateName": "ailab-node-diag-", "labels": {"app": "ailab-node-diag"}},
        "spec": {
            "nodeName": node, "restartPolicy": "Never", "automountServiceAccountToken": False,
            "activeDeadlineSeconds": _TIMEOUT_SEC + 30,
            "containers": [{
                "name": "diag", "image": image, "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", _SCRIPT],
                "volumeMounts": [{"name": "log", "mountPath": "/host-log", "readOnly": True}],
            }],
            "volumes": [{"name": "log", "hostPath": {"path": "/var/log", "type": "Directory"}}],
        },
    }


def kernel_gpu_log(v1, namespace, image, node) -> str:
    if not image:
        raise RuntimeError("IMAGE_HELPER_IMAGE is empty")
    name = v1.create_namespaced_pod(namespace, _pod(image, node)).metadata.name
    try:
        deadline = time.monotonic() + _TIMEOUT_SEC
        while (phase := v1.read_namespaced_pod(name, namespace).status.phase) not in ("Succeeded", "Failed"):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"node diag pod {name} did not finish in {_TIMEOUT_SEC}s")
            time.sleep(_POLL_SEC)
        log = v1.read_namespaced_pod_log(name, namespace)
        if phase == "Failed":
            # 실패한 Pod 의 로그는 수집 결과가 아니다 — 성공 데이터로 돌려주지 않는다.
            raise RuntimeError(f"node diag pod {name} failed: {(log or '')[-300:]}")
        return log
    finally:
        try:
            v1.delete_namespaced_pod(name, namespace, grace_period_seconds=0)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
