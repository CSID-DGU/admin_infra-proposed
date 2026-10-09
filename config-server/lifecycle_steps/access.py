"""접속 차단·해제 작업 단계 — 계정의 모든 Pod 의 모든 접속 포트를 한 번에 막거나 푼다.

Service 를 지우지 않고 선택자만 바꾼다. 지우면 외부 포트 배정이 풀려(reconcile_nodeport_allocations) 그 번호가
다른 Pod 에 넘어가고, 풀었을 때 같은 포트로 돌아오지 못한다. 선택자에 어떤 Pod 에도 없는 조건을 붙이면 Service 는
남고 넘길 대상만 없어져, 외부 포트로 온 연결이 노드에서 거절된다.

이미 맞춰진 Service 는 그대로 두므로 중간에 끊긴 작업은 처음부터 다시 실행하면 이어서 끝난다. 차단 중에 새로
만들어지는 Service 는 이 작업이 아니라 만드는 쪽(create_nodeport_services 의 blocked)이 막힌 채로 만든다.
"""
from kubernetes import client

from utils import ACCESS_BLOCK_LABEL


class _MainProxy:
    def __getattr__(self, name):
        import main
        return getattr(main, name)


_main = _MainProxy()

SERVICE_SELECTOR = "username={username},app=ailab-nodeport"


def _is_blocked(service):
    return ACCESS_BLOCK_LABEL in (service.spec.selector or {})


def step_set_account_access(ctx):
    username, blocked = ctx["username"], ctx["blocked"]
    namespace = _main.app.config["NAMESPACE"]
    # 병합 패치에서 None 은 그 키를 지운다 — 나머지 선택자(pod_name)는 건드리지 않는다.
    patch = {"spec": {"selector": {ACCESS_BLOCK_LABEL: "true" if blocked else None}}}

    try:
        _main.load_k8s()
        v1 = client.CoreV1Api()
        services = v1.list_namespaced_service(
            namespace=namespace, label_selector=SERVICE_SELECTOR.format(username=username)).items
        changed = 0
        for service in services:
            if _is_blocked(service) == blocked:
                continue
            v1.patch_namespaced_service(service.metadata.name, namespace, patch)
            changed += 1
    except Exception as e:
        _main.app.logger.exception("[ACCESS] 접속 %s 실패: user=%s", "차단" if blocked else "해제", username)
        raise _main.StepFailed(
            _main.infra_error("CHANGE_ACCESS", "ACCESS_CHANGE_FAILED", str(e)), 500, cause=e)

    _main.app.logger.info("[ACCESS] user=%s blocked=%s services=%d changed=%d",
                          username, blocked, len(services), changed)
    ctx["access_services"] = len(services)


ACCESS_CHANGE_STEPS = [step_set_account_access]
