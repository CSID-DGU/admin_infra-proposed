"""마이그레이션 단계 — 새 노드에 Pod를 먼저 만들고, 준비되면 기존 Pod를 정리한다.

Pod 준비·스펙·생성·Ready 대기·Service는 생성 단계(provision)를 그대로 쓰고, 옮길 노드 고르기·기존 Pod의
로그인 비밀번호 이어받기·기존 Pod 정리만 여기에 둔다. 홈 디렉터리는 NAS라 그대로 이어지고, 컨테이너 안의
시스템 변경(설치한 패키지 등)은 옮기지 않는다.

같은 노드에서 다시 만들 때(recreate)만 컨테이너 변경분을 그 노드의 이미지로 구워 새 Pod에 이어 줄 수 있다
(adapters/node_image.py). 구운 이미지는 그 노드에만 있어 다른 노드로 옮길 때는 쓰지 못한다.
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
    if ctx.get("recreate"):
        ctx["node"] = current
        _log_select(ctx, Phase.SUCCESS, node=current, detail={"node": current, "recreate": True})
        _main.app.logger.info(f"[MIGRATE] request_id={ctx['request_id']} recreate on {current}")
        return
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


def _shell_container(pod):
    for status in (getattr(pod.status, "container_statuses", None) or []):
        if status.name == "shell":
            return status
    return None


def step_migrate_commit_image(ctx):
    """같은 노드에서 다시 만들 때 기존 컨테이너의 변경분을 그 노드의 이미지로 굽는다. 굽지 못하면 기본 이미지로
    넘어가지 않고 작업을 실패로 끝낸다 — 조용히 넘어가면 사용자는 설치한 것이 사라진 Pod를 받는다."""
    if not (ctx.get("recreate") and ctx.get("keep_changes")):
        return
    ns = _main.app.config["NAMESPACE"]
    ref = _main.node_image.ref_for(ns, ctx["pod_name"])
    common = dict(request_id=ctx["request_id"], username=ctx["username"], pod_name=ctx["old_pod_name"],
                  node_name=ctx["from_node"], resource_type="image", action=Action.COMMIT_IMAGE)
    _main.set_pod_creation_status(ctx["request_id"], "committing_image", "컨테이너 변경분 저장 중")
    _main.log_operation(phase=Phase.START, **common)

    def fail(code, detail):
        _main.set_pod_creation_status(ctx["request_id"], "failed", "컨테이너 변경분 저장 실패")
        _main.log_operation(phase=Phase.FAIL, error_code=code, error_detail=str(detail)[:1000], **common)
        raise _main.StepFailed(_main.infra_error("MIGRATE_COMMIT_IMAGE", code, str(detail)), 422, retry=False)

    _main.load_k8s()
    container = _shell_container(client.CoreV1Api().read_namespaced_pod(ctx["old_pod_name"], ns))
    if container is None or not container.container_id:
        fail("IMAGE_COMMIT_NO_CONTAINER", "old pod has no container to commit")
    settings = _main.node_image_settings()
    try:
        added = _main.node_image.commit(settings, ctx["from_node"], container.container_id, ref,
                                        running=container.state.running is not None)
    except _main.node_image.NodeImageError as e:
        fail(e.code, e.detail)
    limit = _main.app.config["USER_IMAGE_MAX_ADDED_BYTES"]
    if added > limit:
        _main.remove_node_image_quietly(ctx["from_node"], ref)
        fail("IMAGE_CHANGES_TOO_LARGE", f"added {added} bytes > limit {limit}")
    ctx["committed_image"] = ref
    _main.log_operation(phase=Phase.SUCCESS, error_detail=json.dumps({"image": ref, "added_bytes": added}), **common)


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
        _main.load_k8s()
        old_image = _old_pod_image(ns, old_pod)
        try:
            _main.delete_pod_util(old_pod, ns)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        _main.delete_account_secret(client.CoreV1Api(), ns, old_pod)
        # 기존 Pod가 구운 이미지로 떠 있었으면 그 이름은 더 쓸 곳이 없다. 새 이미지가 그 층을 이어 쓰면 층은
        # 남고 이름만 지워진다. 컨테이너가 사라진 뒤에야 지울 수 있어 Pod 삭제를 기다린다.
        if _main.node_image.is_user_image(old_image) and old_image != ctx.get("committed_image"):
            _main.wait_for_pod_deleted(client.CoreV1Api(), old_pod, ns)
            _main.remove_node_image_quietly(ctx.get("from_node"), old_image)
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


def _old_pod_image(ns, old_pod):
    try:
        return client.CoreV1Api().read_namespaced_pod(old_pod, ns).spec.containers[0].image
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return None
        raise


MIGRATE_STEPS = [
    step_migrate_select_target,
    step_fetch_user_config,
    step_migrate_inherit_password,
    step_prepare_pod,
    step_migrate_commit_image,
    step_build_pod_spec,
    step_create_pod_k8s,
    step_wait_ready,
    step_create_services,
    step_migrate_cleanup_old,
]
