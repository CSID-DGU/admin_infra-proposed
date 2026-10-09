"""접속 차단·해제 작업 단계 — 계정의 모든 Pod 의 모든 접속 포트를 한 번에 막거나 푼다.

Service 를 지우지 않고 선택자만 바꾼다. 지우면 외부 포트 배정이 풀려(reconcile_nodeport_allocations) 그 번호가
다른 Pod 에 넘어가고, 풀었을 때 같은 포트로 돌아오지 못한다. 선택자에 어떤 Pod 에도 없는 조건을 붙이면 Service 는
남고 넘길 대상만 없어져, 외부 포트로 온 연결이 노드에서 거절된다.

이미 맞춰진 Service 는 그대로 두므로 중간에 끊긴 작업은 처음부터 다시 실행하면 이어서 끝난다. 차단 중에 새로
만들어지는 Service 는 이 작업이 아니라 만드는 쪽(create_nodeport_services 의 blocked)이 막힌 채로 만든다.

늦게 다시 실행된 옛 작업이 그 뒤의 결정을 덮지 못하게, 작업이 등록된 시각(decided_at)을 Service 에 적어 둔다.
규칙은 하나다 — 더 나중 결정으로 반대 상태가 된 Service 가 하나라도 있으면 이 작업은 물러난다. 시각이 같으면 차단이
이긴다. 물러난 작업은 성공이 아니라 실패(ACCESS_SUPERSEDED)로 끝난다: 아무것도 바꾸지 않았는데 반영됐다고 기록되면
부르는 쪽이 실제와 다른 상태를 믿게 된다.

시각은 뒤로 가지 않게 적고(이미 더 나중 시각이 적힌 Service 는 그대로), 바꿀 것이 없는 Service 에도 적는다 — 적지
않으면 바꿀 것이 없던 차단을 옛 해제가 지나친다. 읽은 뒤 다른 작업이 바꾼 Service 는 resourceVersion 이 달라
apiserver 가 409 로 거절하고, 단계가 처음부터 다시 실행되어 새로 읽는다.

막힌 채로 Service 를 만드는 작업(컨테이너 이동·포트 변경)도 자기 등록 시각을 적는다 — 계정의 Service 가 전부 새로
만들어져도 그 앞의 해제는 물러난다. 열린 채로 만드는 Service 에는 적지 않는다: 그 시각이 뒤따르는 차단을 물러나게
하면 안 된다.
"""
from kubernetes import client

from utils import ACCESS_BLOCK_LABEL, ACCESS_DECIDED_AT_ANNOTATION as DECIDED_AT_ANNOTATION


class _MainProxy:
    def __getattr__(self, name):
        import main
        return getattr(main, name)


_main = _MainProxy()

SERVICE_SELECTOR = "username={username},app=ailab-nodeport"


def _is_blocked(service):
    return ACCESS_BLOCK_LABEL in (service.spec.selector or {})


def _decided_at(service):
    try:
        return int((service.metadata.annotations or {}).get(DECIDED_AT_ANNOTATION) or 0)
    except ValueError:
        return 0


def _superseded(services, blocked, decided_at):
    """더 나중 결정으로 반대 상태가 된 Service 가 있는가. 시각이 같으면 차단이 이긴다."""
    if blocked:
        return any(not _is_blocked(s) and _decided_at(s) > decided_at for s in services)
    return any(_is_blocked(s) and _decided_at(s) >= decided_at for s in services)


def step_set_account_access(ctx):
    username, blocked, decided_at = ctx["username"], ctx["blocked"], ctx["decided_at"]
    namespace = _main.app.config["NAMESPACE"]

    try:
        _main.load_k8s()
        v1 = client.CoreV1Api()
        services = v1.list_namespaced_service(
            namespace=namespace, label_selector=SERVICE_SELECTOR.format(username=username)).items
        superseded = _superseded(services, blocked, decided_at)
        stale = [] if superseded else [
            service for service in services
            if _is_blocked(service) != blocked or _decided_at(service) < decided_at]
        for service in stale:
            # 병합 패치에서 None 은 그 키를 지운다 — 나머지 선택자(pod_name)는 건드리지 않는다.
            patch = {
                "metadata": {
                    "resourceVersion": service.metadata.resource_version,
                    "annotations": {DECIDED_AT_ANNOTATION: str(max(decided_at, _decided_at(service)))}},
                "spec": {"selector": {ACCESS_BLOCK_LABEL: "true" if blocked else None}},
            }
            v1.patch_namespaced_service(service.metadata.name, namespace, patch)
    except Exception as e:
        _main.app.logger.exception("[ACCESS] 접속 %s 실패: user=%s", "차단" if blocked else "해제", username)
        raise _main.StepFailed(
            _main.infra_error("CHANGE_ACCESS", "ACCESS_CHANGE_FAILED", str(e)), 500, cause=e)

    if superseded:
        _main.app.logger.warning("[ACCESS] 더 나중 결정이 이미 적용돼 물러남: user=%s blocked=%s decided_at=%d",
                                 username, blocked, decided_at)
        raise _main.StepFailed(_main.infra_error(
            "CHANGE_ACCESS", "ACCESS_SUPERSEDED",
            f"a later decision already set the opposite state for {username}"), 409, retry=False)

    _main.app.logger.info("[ACCESS] user=%s blocked=%s services=%d changed=%d",
                          username, blocked, len(services), len(stale))
    ctx["access_services"] = len(services)


ACCESS_CHANGE_STEPS = [step_set_account_access]
