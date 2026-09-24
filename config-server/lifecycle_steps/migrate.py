"""마이그레이션 단계 — 새 노드에 Pod를 먼저 만들고, 준비되면 기존 Pod를 정리한다.

Pod 준비·스펙·생성·Ready 대기·Service는 생성 단계(provision)를 그대로 쓰고, 옮길 노드 고르기·기존 Pod의
로그인 비밀번호 이어받기·기존 Pod 정리만 여기에 둔다. 홈 디렉터리는 NAS라 그대로 이어지고, 컨테이너 안의
시스템 변경(설치한 패키지 등)은 옮기지 않는다.
"""
import json

from kubernetes import client

from adapters.operation_log import Action, Phase
from lifecycle_steps.provision import (
    step_fetch_user_config, step_prepare_pod, step_build_pod_spec, step_create_pod_k8s,
    step_wait_ready, step_create_services,
)


class _MainProxy:
    def __getattr__(self, name):
        import main
        return getattr(main, name)


_main = _MainProxy()


def _log_select(ctx, phase, node=None, error_code=None, detail=None):
    _main.log_operation(request_id=ctx["request_id"], username=ctx["username"], pod_name=ctx.get("old_pod_name"),
                        node_name=node, action=Action.SELECT_NODE, phase=phase, error_code=error_code,
                        error_detail=json.dumps(detail, ensure_ascii=False) if detail else None)


def _fail(ctx, status, code, detail):
    _main.set_pod_creation_status(ctx["request_id"], "failed", "마이그레이션 실패")
    _log_select(ctx, Phase.FAIL, error_code=code, detail={"reason": detail})
    raise _main.StepFailed(_main.infra_error("MIGRATE_SELECT_NODE", code, detail), status)


def _skip(ctx, reason, detail):
    ctx["skipped"], ctx["skip_reason"] = True, reason
    _log_select(ctx, Phase.SUCCESS, node=ctx.get("from_node"), detail={"reason": reason, **detail})
    _main.set_pod_creation_status(ctx["request_id"], "ready", "마이그레이션 건너뜀")
    _main.app.logger.info(f"[MIGRATE] skipped request_id={ctx['request_id']} reason={reason}")


def step_migrate_select_target(ctx):
    """기존 Pod와 현재 노드를 확인하고 옮길 노드를 고른다. 옮길 이유가 없으면 작업을 건너뜀으로 끝낸다."""
    ns = _main.app.config["NAMESPACE"]
    _main.set_pod_creation_status(ctx["request_id"], "selecting_node", "옮길 노드 선택 중")
    _log_select(ctx, Phase.START)

    nodes = []
    for name in ctx["nodes"]:
        resolved = _main.resolve_k8s_node_name(name)
        if not resolved:
            _fail(ctx, 400, "UNKNOWN_NODE", f"unknown kubernetes node: {name!r}")
        nodes.append(resolved)

    old_pod = ctx.get("old_pod_name") or _main.get_existing_pod(ns, ctx["username"])
    if not old_pod:
        _fail(ctx, 404, "POD_NOT_FOUND", "no running pod")
    _main.load_k8s()
    try:
        pod = client.CoreV1Api().read_namespaced_pod(old_pod, ns)
    except client.exceptions.ApiException as e:
        if e.status == 404:
            ctx["old_pod_name"] = old_pod
            _fail(ctx, 404, "POD_NOT_FOUND", f"pod not found: {old_pod}")
        raise
    current = pod.spec.node_name
    ctx["old_pod_name"], ctx["from_node"] = old_pod, current
    if current not in nodes:
        _fail(ctx, 400, "CURRENT_NODE_NOT_IN_CANDIDATES", f"current node {current!r} is not in given nodes")

    candidates = [n for n in nodes if n != current]
    if not candidates:
        _skip(ctx, "no_candidate_node", {})
        return

    prom, timeout = _main.app.config["PROM_URL"], _main.app.config["HTTP_TIMEOUT_SEC"]
    current_score = _main.get_node_gpu_score(current, prom, timeout)
    scores = {n: _main.get_node_gpu_score(n, prom, timeout) for n in candidates}
    best, best_score = min(scores.items(), key=lambda item: item[1])
    if not ctx.get("force") and best_score > current_score * (1 - ctx.get("min_ratio", 0.2)):
        _skip(ctx, "no_significant_improvement", {"node": best})
        return
    ctx["node"] = best
    _log_select(ctx, Phase.SUCCESS, node=best, detail={"node": best})
    _main.app.logger.info(f"[MIGRATE] request_id={ctx['request_id']} {current} -> {best}")


def step_migrate_inherit_password(ctx):
    """신청의 비밀번호 해시는 승인 완료 뒤 지워지므로 기존 Pod의 로그인 비밀번호 Secret을 이어받는다.
    사용자 설정 조회가 재개 때마다 다시 돌기 때문에 이 단계도 매번 다시 돈다."""
    ns = _main.app.config["NAMESPACE"]
    _main.load_k8s()
    try:
        ctx["user_info"]["passwd_hash"] = _main.login_password_for_recreate(
            client.CoreV1Api(), ns, ctx["old_pod_name"], ctx["user_info"])
    except _main.LoginPasswordMissing as e:
        _main.set_pod_creation_status(ctx["request_id"], "failed", "로그인 비밀번호 없음")
        _main.log_operation(request_id=ctx["request_id"], username=ctx["username"], pod_name=ctx["old_pod_name"],
                            action=Action.CREATE_POD_K8S, phase=Phase.FAIL,
                            error_code="LOGIN_PASSWORD_MISSING", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error("MIGRATE", "LOGIN_PASSWORD_MISSING", str(e)), 422)


def step_migrate_cleanup_old(ctx):
    """새 Pod가 서비스 중이므로 기존 Pod 정리가 실패해도 작업은 성공으로 두고 결과에 남긴다
    (실패로 돌리면 호출자가 다시 옮기려다 Pod가 하나 더 생긴다)."""
    ns = _main.app.config["NAMESPACE"]
    old_pod = ctx["old_pod_name"]
    common = dict(request_id=ctx["request_id"], username=ctx["username"], pod_name=old_pod,
                  node_name=ctx.get("from_node"), resource_type="pod", action=Action.DELETE_POD_K8S)
    _main.log_operation(phase=Phase.START, **common)
    try:
        _main.delete_nodeport_services(old_pod, ns)
        _main.release_nodeports(old_pod)
        try:
            _main.delete_pod_util(old_pod, ns)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        _main.load_k8s()
        _main.delete_account_secret(client.CoreV1Api(), ns, old_pod)
        # 기존 노드의 keytab도 정리한다(같은 사용자의 다른 Pod가 그 노드에 남아 있으면 유지). 안 지우면 회수 때는
        # 마지막 노드만 정리되므로 기존 노드에 keytab이 계속 남는다.
        _main.step_cleanup_pod_node_krb5({"username": ctx["username"], "pod_name": old_pod,
                                          "pod_node_name": ctx.get("from_node")})
    except Exception as e:
        _main.app.logger.exception(f"[MIGRATE] 기존 Pod({old_pod}) 정리 실패 — 새 Pod는 정상, 수동 정리 필요")
        ctx["old_pod_cleanup"] = "failed"
        _main.log_operation(phase=Phase.FAIL, error_code="OLD_POD_CLEANUP_FAILED", error_detail=str(e)[:1000], **common)
    else:
        _main.log_operation(phase=Phase.SUCCESS, **common)
    _main.set_pod_creation_status(ctx["request_id"], "ready", f"마이그레이션 완료 (node={ctx['node']})")


MIGRATE_STEPS = [
    step_migrate_select_target,
    step_fetch_user_config,
    step_migrate_inherit_password,
    step_prepare_pod,
    step_build_pod_spec,
    step_create_pod_k8s,
    step_wait_ready,
    step_create_services,
    step_migrate_cleanup_old,
]
