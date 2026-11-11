"""노드 커널 오류 로그 수집 — 그 노드에 일회용 Pod를 띄워 호스트의 /var/log를 읽기 전용으로 마운트하고
GPU 드라이버 오류 줄만 꺼낸다.

특권 없이 읽기 위해 dmesg 대신 호스트가 이미 파일로 남겨 둔 kern.log를 읽는다(컨테이너의 dmesg 와
/dev/kmsg 는 CAP_SYSLOG 가 없어 막힌다). 노드에 그 파일이 없으면 없다고 돌려준다(없는 것을 비어 있는 것으로
치지 않는다)."""
import time

from kubernetes import client

_POLL_SEC = 2
_TIMEOUT_SEC = 60
# GPU 드라이버 오류만 잡는다. 대소문자 구분 없이 nvidia 로 넓히면 HDMI 오디오("HDA NVidia HDMI")와 디스플레이
# (nvidia-modeset 5초 반복) 줄이 끝 200줄을 채워 정작 Xid 가 밀려난다.
GPU_LOG_PATTERN = "NVRM|Xid|[Ff]allen off the bus"
# 읽을 곳을 순서대로 본다: 커널 로그(kern.log.1 은 logrotate 직후 현재 파일이 거의 빈 경우를 위해) →
# 없으면 syslog(커널 줄이 같이 들어 있다) → 없으면 journal 원본 파일(바이너리라 grep -a, 압축된 줄은 놓칠 수 있다).
# 읽을 파일이 하나도 없으면 그렇게 적고, 있는데 GPU 줄이 없으면 그렇게 적는다("오류 없음"과 "수집 못 함"을 가른다).
_SCRIPT = (
    "o=/tmp/gpu-lines; : > $o; found=0; src=; "
    "for set in 'kern.log.1 kern.log' 'syslog.1 syslog'; do "
    "for n in $set; do f=/host-log/$n; "
    f"if [ -r \"$f\" ]; then found=1; grep -E '{GPU_LOG_PATTERN}' \"$f\" >> $o || true; fi; done; "
    "if [ $found = 1 ]; then src=\"$set\"; break; fi; done; "
    "if [ $found = 0 ]; then for j in /host-log/journal/*/system.journal; do "
    f"if [ -r \"$j\" ]; then found=1; src=journal; grep -aE '{GPU_LOG_PATTERN}' \"$j\" >> $o || true; fi; done; fi; "
    "if [ $found = 0 ]; then echo 'no readable kernel log (kern.log, syslog, journal) on this node'; "
    "elif [ -s $o ]; then echo \"source: $src\"; tail -n 200 $o; "
    "else echo \"no GPU driver lines in: $src\"; fi"
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
