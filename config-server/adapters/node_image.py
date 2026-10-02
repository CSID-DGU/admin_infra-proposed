"""노드 로컬 이미지 — 사용자 컨테이너의 변경분을 그 노드의 이미지로 굽고, 필요 없어지면 지운다.

구운 이미지는 레지스트리에 올리지 않고 구운 노드에만 둔다. 같은 노드에서 Pod를 다시 만들 때만 쓸 수 있다.
노드의 containerd에는 일회용 Pod(node_image_cli.py)로 접근한다 — 노드에 따로 설치하는 것이 없고, 작업이
없을 때는 containerd에 닿는 것이 아무것도 떠 있지 않다.

이 모듈은 작업 단계(재생성·이동·회수)를 모른다. 설정은 호출하는 쪽이 HelperSettings로 넘긴다.
"""
import json
import re
import time
from dataclasses import dataclass

from kubernetes import client

from node_image_cli import RESULT_PREFIX

# 실제로 존재하지 않는 레지스트리 이름이다. 이미지가 노드에서 사라졌을 때 kubelet이 공개 레지스트리에서
# 같은 이름을 받아 오는 일이 없게 한다(Pod도 내려받지 않도록 만든다 — PULL_POLICY).
REPOSITORY = "ailab.local/user-image"
PULL_POLICY = "Never"

_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(rf"^{re.escape(REPOSITORY)}/[a-z0-9]([a-z0-9-]*[a-z0-9])?:[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_POLL_SEC = 2
_DEADLINE_MARGIN_SEC = 60


class NodeImageError(Exception):
    """노드 이미지 작업 실패. code는 operation_log error_code로 그대로 쓴다."""

    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class HelperSettings:
    namespace: str      # 도우미 Pod를 띄울 네임스페이스
    image: str          # node_image_cli.py와 nerdctl이 들어 있는 이미지
    socket: str         # 노드의 containerd 소켓 경로
    timeout_sec: int    # 도우미 Pod 하나가 끝나기를 기다리는 최대 시간


def ref_for(namespace: str, pod_name: str) -> str:
    """Pod 하나에 이미지 하나. 여러 스택이 노드를 함께 쓰므로 네임스페이스로 나눈다."""
    return f"{REPOSITORY}/{namespace}:{pod_name}"


def is_user_image(image: str) -> bool:
    return bool(image) and image.startswith(REPOSITORY + "/")


def container_id(status_container_id: str) -> str:
    """Pod 상태의 containerID(`containerd://<64자리>`)에서 런타임 ID만 꺼낸다."""
    value = (status_container_id or "").split("://", 1)[-1]
    if not _CONTAINER_ID.match(value):
        raise NodeImageError("IMAGE_COMMIT_NO_CONTAINER", f"unexpected container id: {status_container_id!r}")
    return value


def commit(settings: HelperSettings, node: str, container: str, ref: str, running: bool) -> int:
    """컨테이너를 ref 이름의 이미지로 굽고, 기본 이미지 위에 쌓인 변경분 크기(바이트)를 돌려준다."""
    runtime_id = container_id(container)
    args = ["commit", "--container", runtime_id, "--ref", _checked_ref(ref)]
    if running:
        args.append("--running")
    try:
        return int(_run_helper(settings, node, args, "IMAGE_COMMIT_FAILED")["added_bytes"])
    except Exception:
        if running:
            _unpause_quietly(settings, node, runtime_id)
        raise


def _unpause_quietly(settings: HelperSettings, node: str, runtime_id: str) -> None:
    """실행 중인 컨테이너는 굽는 동안 멈춘다. 굽기가 중간에 끊기면 멈춘 채 남아 사용자의 작업이 얼어 있으므로
    실패한 뒤에는 한 번 풀어 준다. 풀지 못해도 원래 실패를 가리지 않는다."""
    try:
        _run_helper(settings, node, ["unpause", "--container", runtime_id], "IMAGE_UNPAUSE_FAILED")
    except Exception:
        pass


def remove(settings: HelperSettings, node: str, ref: str) -> bool:
    """이미지를 지운다. 이미 없으면 False. 아직 쓰는 컨테이너가 있으면 NodeImageError."""
    return bool(_run_helper(settings, node, ["remove", "--ref", _checked_ref(ref)], "IMAGE_REMOVE_FAILED")["removed"])


def _checked_ref(ref: str) -> str:
    if not _REF.match(ref or ""):
        raise NodeImageError("IMAGE_REF_INVALID", f"not a user image ref: {ref!r}")
    return ref


def _helper_pod(settings: HelperSettings, node: str, args):
    return {
        "metadata": {"generateName": "ailab-image-helper-", "labels": {"app": "ailab-image-helper"}},
        "spec": {
            "nodeName": node,
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            # 시간 초과는 기다리는 쪽(_run_helper)이 먼저 판정한다. 이 값은 config-server가 죽어 Pod가
            # 남았을 때의 안전장치라 조금 더 길게 둔다.
            "activeDeadlineSeconds": settings.timeout_sec + _DEADLINE_MARGIN_SEC,
            "containers": [{
                "name": "helper",
                "image": settings.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["python", "/app/node_image_cli.py", "--socket", settings.socket, *args],
                "volumeMounts": [{"name": "containerd", "mountPath": settings.socket}],
            }],
            "volumes": [{"name": "containerd", "hostPath": {"path": settings.socket, "type": "Socket"}}],
        },
    }


def _run_helper(settings: HelperSettings, node: str, args, fail_code: str) -> dict:
    if not settings.image:
        raise NodeImageError("IMAGE_HELPER_NOT_CONFIGURED", "IMAGE_HELPER_IMAGE is empty")
    v1 = client.CoreV1Api()
    name = v1.create_namespaced_pod(settings.namespace, _helper_pod(settings, node, args)).metadata.name
    try:
        deadline = time.monotonic() + settings.timeout_sec
        while True:
            phase = v1.read_namespaced_pod(name, settings.namespace).status.phase
            if phase in ("Succeeded", "Failed"):
                break
            if time.monotonic() >= deadline:
                raise NodeImageError("IMAGE_HELPER_TIMEOUT", f"helper pod {name} on {node} still {phase}")
            time.sleep(_POLL_SEC)
        result = _parse_result(v1.read_namespaced_pod_log(name, settings.namespace))
    finally:
        try:
            v1.delete_namespaced_pod(name, settings.namespace, grace_period_seconds=0)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
    if "error" in result:
        raise NodeImageError(fail_code, result["error"])
    return result


def _parse_result(log: str) -> dict:
    for line in reversed((log or "").splitlines()):
        if line.startswith(RESULT_PREFIX):
            return json.loads(line[len(RESULT_PREFIX):])
    return {"error": f"helper printed no result: {(log or '').strip()[-300:]}"}
