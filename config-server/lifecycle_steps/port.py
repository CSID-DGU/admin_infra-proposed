"""포트 변경 작업 단계 — 떠 있는 Pod 의 추가 포트를 Pod 를 다시 만들지 않고 더하고 뺀다.

입력은 "바뀐 뒤 추가 포트 전체 목록"이다. 지금 배정(nodeport_allocations)·Service 와 비교해 남는 것만 지우고
모자란 것만 만든다. 이미 맞춰진 조각은 그대로 두므로 중간에 끊긴 작업은 처음부터 다시 실행하면 이어서 끝난다.

기본 포트(ssh·jupyter)와 noVNC 포트는 이 작업이 건드리지 않는다 — noVNC 는 Pod 기동 때 환경변수로 켜지므로
포트만 더해서는 열리지 않는다.
"""
from kubernetes import client

from request_models import PROTECTED_PORTS


class _MainProxy:
    def __getattr__(self, name):
        import main
        return getattr(main, name)


_main = _MainProxy()

SERVICE_SELECTOR = "pod_name={pod_name},app=ailab-nodeport"


def _failed(code, detail, status, cause=None):
    return _main.StepFailed(_main.infra_error("CHANGE_PORT", code, detail), status, cause=cause)


def _allocations(pod_name):
    """이 Pod 에 배정된 포트 행. [(internal_port, node_port, purpose, username)]"""
    conn = _main.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT internal_port, node_port, purpose, username FROM nodeport_allocations "
                "WHERE pod_name=%s ORDER BY id",
                (pod_name,),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def _release(pod_name, internal_ports):
    conn = _main.get_db_connection()
    try:
        with conn.cursor() as cur:
            for port in internal_ports:
                cur.execute(
                    "DELETE FROM nodeport_allocations WHERE pod_name=%s AND internal_port=%s",
                    (pod_name, port),
                )
        conn.commit()
    finally:
        conn.close()


def _services_by_port(v1, namespace, pod_name):
    """내부 포트 → 그 포트의 Service 이름들."""
    found = {}
    services = v1.list_namespaced_service(
        namespace=namespace, label_selector=SERVICE_SELECTOR.format(pod_name=pod_name))
    for svc in services.items:
        for port in svc.spec.ports or []:
            found.setdefault(port.port, []).append(svc.metadata.name)
    return found


def _read_pod(v1, namespace, pod_name):
    try:
        pod = v1.read_namespaced_pod(pod_name, namespace)
    except client.exceptions.ApiException as e:
        if e.status == 404:
            raise _failed("POD_NOT_FOUND", f"pod not found: {pod_name}", 404)
        raise
    if pod.metadata.deletion_timestamp is not None:
        raise _failed("POD_NOT_FOUND", f"pod is being deleted: {pod_name}", 404)
    return pod


def step_change_ports(ctx):
    username, pod_name = ctx["username"], ctx["pod_name"]
    wanted = {p["internal_port"]: p.get("usage_purpose") or "custom" for p in ctx["wanted_ports"]}
    namespace = _main.app.config["NAMESPACE"]

    try:
        _main.load_k8s()
        v1 = client.CoreV1Api()
        pod = _read_pod(v1, namespace, pod_name)

        rows = _allocations(pod_name)
        if not rows:
            raise _failed("POD_NOT_FOUND", f"no port allocation for pod: {pod_name}", 404)
        if any(row[3] != username for row in rows):
            raise _failed("POD_OWNER_MISMATCH", f"pod {pod_name} does not belong to {username}", 409)

        extra = {row[0] for row in rows if row[0] not in PROTECTED_PORTS}
        removed = sorted(extra - set(wanted))
        added = sorted(set(wanted) - extra)

        # 뺄 포트는 Service 를 먼저 지운다 — 배정 행부터 지우면 Service 가 살아 있는 외부 포트가 다른 Pod 에 다시 배정된다.
        services = _services_by_port(v1, namespace, pod_name)
        for port in removed:
            for name in services.get(port, []):
                v1.delete_namespaced_service(name, namespace)
        if removed:
            _release(pod_name, removed)

        if added:
            _main.allocate_nodeports(
                username=username, pod_name=pod_name, node_name=pod.spec.node_name,
                ports=[{"internal_port": port, "usage_purpose": wanted[port]} for port in added])

        # 배정 행은 있는데 Service 가 없는 포트(앞선 시도가 그 사이에서 끊긴 경우)까지 여기서 채운다.
        ports = [{"internal_port": row[0], "external_port": row[1], "usage_purpose": row[2] or "custom"}
                 for row in _allocations(pod_name)]
        missing = [p for p in ports
                   if p["internal_port"] in wanted and p["internal_port"] not in services]
        if missing:
            _main.create_nodeport_services(username, namespace, pod_name, missing,
                                           blocked=bool(ctx.get("access_blocked")),
                                           decided_at=ctx.get("access_decided_at") or 0)
    except _main.StepFailed:
        raise
    except Exception as e:
        _main.app.logger.exception("[PORT] 포트 변경 실패: pod=%s", pod_name)
        raise _failed("PORT_CHANGE_FAILED", str(e), 500, cause=e)

    _main.app.logger.info("[PORT] pod=%s added=%s removed=%s", pod_name, added, removed)
    ctx["node"] = pod.spec.node_name
    ctx["allocated_ports"] = ports


PORT_CHANGE_STEPS = [step_change_ports]
