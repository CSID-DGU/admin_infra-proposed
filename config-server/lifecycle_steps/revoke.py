"""회수 단계 — Service·노드포트·Pod·keytab·계정

main.py에서 얇게 이동한 코드다. main 소속 헬퍼·설정은 테스트가 main.*를 대역으로 바꾸는
계약을 지키기 위해 지연 프록시(_main)로 호출 시점에 바인딩한다.
"""
import base64
import crypt
import json
import os
import re
import subprocess
import time

import requests
import urllib3
from kubernetes import client

from adapters.operation_log import Action, Phase


class _MainProxy:
    def __getattr__(self, name):
        import main
        return getattr(main, name)


_main = _MainProxy()


def step_delete_services(ctx):
    request_id, username, pod_name = ctx["request_id"], ctx["username"], ctx["pod_name"]
    rollback = ctx["rollback"]
    ns = _main.app.config["NAMESPACE"]

    _main.app.logger.info("[DELETE POD] deleting NodePort services")
    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  resource_type="service", action=Action.DELETE_SERVICE, phase=Phase.START)
    try:
        _main.delete_nodeport_services(pod_name, ns)
        rollback["servicesDeleted"] = True
    except client.exceptions.ApiException as e:
        _main.app.logger.exception("[DELETE POD] service deletion failed")
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      resource_type="service", action=Action.DELETE_SERVICE, phase=Phase.FAIL,
                      error_code="NODEPORT_SERVICE_DELETE_FAILED", error_detail=str(e.body))
        raise _main.StepFailed(_main.infra_error(
            "DELETE_NODEPORT_SERVICE",
            "NODEPORT_SERVICE_DELETE_FAILED",
            e.body,
            rollback=rollback,
            pod_name=pod_name,
            **_main.k8s_error_fields(e),
        ), 500)
    except Exception as e:
        _main.app.logger.exception("[DELETE POD] service deletion failed")
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      resource_type="service", action=Action.DELETE_SERVICE, phase=_main._fail_phase(e),
                      error_code="NODEPORT_SERVICE_DELETE_FAILED", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "DELETE_NODEPORT_SERVICE",
            "NODEPORT_SERVICE_DELETE_FAILED",
            str(e),
            rollback=rollback,
            pod_name=pod_name,
        ), 500, cause=e)
    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  resource_type="service", action=Action.DELETE_SERVICE, phase=Phase.SUCCESS)

def step_release_nodeports(ctx):
    request_id, username, pod_name = ctx["request_id"], ctx["username"], ctx["pod_name"]
    rollback = ctx["rollback"]

    _main.app.logger.info("[DELETE POD] releasing NodePort allocations")
    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  resource_type="nodeport", action=Action.RELEASE_NODEPORT, phase=Phase.START)
    try:
        _main.release_nodeports(pod_name)
        rollback["nodeportsReleased"] = True
    except Exception as e:
        _main.app.logger.exception("[DELETE POD] nodeport release failed")
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      resource_type="nodeport", action=Action.RELEASE_NODEPORT, phase=_main._fail_phase(e),
                      error_code="NODEPORT_RELEASE_FAILED", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "RELEASE_NODEPORT",
            "NODEPORT_RELEASE_FAILED",
            str(e),
            rollback=rollback,
            pod_name=pod_name,
        ), 500, cause=e)
    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  resource_type="nodeport", action=Action.RELEASE_NODEPORT, phase=Phase.SUCCESS)

def step_delete_pod_k8s(ctx):
    """Pod가 이미 없으면 ctx["already_absent"]를 세우고 성공으로 끝낸다(뒤의 keytab 정리는 건너뜀)."""
    request_id, username, pod_name = ctx["request_id"], ctx["username"], ctx["pod_name"]
    rollback = ctx["rollback"]
    ns = _main.app.config["NAMESPACE"]

    _main.app.logger.info(f"[DELETE POD] deleting pod from namespace={ns}")
    try:
        _main.load_k8s()
        v1 = client.CoreV1Api()
    except Exception as e:
        _main.app.logger.exception("[DELETE POD] k8s client setup failed")
        raise _main.StepFailed(_main.infra_error(
            "DELETE_POD",
            "K8S_CLIENT_SETUP_FAILED",
            str(e),
            rollback=rollback,
            pod_name=pod_name,
        ), 500)

    pod_node_name = None
    if _main.app.config.get("KRB5_REALM"):
        try:
            pod_node_name = v1.read_namespaced_pod(pod_name, ns).spec.node_name
        except Exception:
            _main.app.logger.warning("[DELETE POD] pod node lookup failed, farm 정리 건너뜀: %s", pod_name, exc_info=True)
    ctx["pod_node_name"] = pod_node_name

    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  node_name=pod_node_name, resource_type="pod",
                  action=Action.DELETE_POD_K8S, phase=Phase.START)
    try:
        v1.delete_namespaced_pod(pod_name, ns)
        rollback["podDeleteRequested"] = True
    except client.exceptions.ApiException as e:
        if e.status == 404:
            rollback["podDeleted"] = True
            _main.app.logger.info("[DELETE POD] pod already absent: %s", pod_name)
            _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                          node_name=pod_node_name, resource_type="pod",
                          action=Action.DELETE_POD_K8S, phase=Phase.SUCCESS,
                          error_detail="pod already absent")
            ctx["already_absent"] = True
            return
        _main.app.logger.exception("[DELETE POD] pod deletion failed")
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=pod_node_name, resource_type="pod",
                      action=Action.DELETE_POD_K8S, phase=Phase.FAIL,
                      error_code="POD_DELETE_FAILED", error_detail=str(e.body))
        raise _main.StepFailed(_main.infra_error(
            "DELETE_POD",
            "POD_DELETE_FAILED",
            e.body,
            rollback=rollback,
            pod_name=pod_name,
            **_main.k8s_error_fields(e),
        ), 500)
    except Exception as e:
        _main.app.logger.exception("[DELETE POD] pod deletion failed")
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=pod_node_name, resource_type="pod",
                      action=Action.DELETE_POD_K8S, phase=_main._fail_phase(e),
                      error_code="POD_DELETE_FAILED", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "DELETE_POD",
            "POD_DELETE_FAILED",
            str(e),
            rollback=rollback,
            pod_name=pod_name,
        ), 500, cause=e)

    _main.app.logger.info("[DELETE POD] waiting for pod deletion to complete")
    try:
        deleted = _main.wait_for_pod_deleted(v1, pod_name, ns, timeout_sec=60)
    except client.exceptions.ApiException as e:
        _main.app.logger.exception("[DELETE POD] deletion polling failed")
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=pod_node_name, resource_type="pod",
                      action=Action.DELETE_POD_K8S, phase=Phase.FAIL,
                      error_code="POD_DELETE_FAILED", error_detail=str(e.body))
        raise _main.StepFailed(_main.infra_error(
            "DELETE_POD",
            "POD_DELETE_FAILED",
            e.body,
            rollback=rollback,
            pod_name=pod_name,
            **_main.k8s_error_fields(e),
        ), 500)
    except Exception as e:
        _main.app.logger.exception("[DELETE POD] deletion polling failed")
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=pod_node_name, resource_type="pod",
                      action=Action.DELETE_POD_K8S, phase=_main._fail_phase(e),
                      error_code="POD_DELETE_FAILED", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "DELETE_POD",
            "POD_DELETE_FAILED",
            str(e),
            rollback=rollback,
            pod_name=pod_name,
        ), 500, cause=e)

    if not deleted:
        _main.app.logger.warning("[DELETE POD] pod deletion timed out: %s", pod_name)
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=pod_node_name, resource_type="pod",
                      action=Action.DELETE_POD_K8S, phase=Phase.FAIL,
                      error_code="POD_DELETE_TIMEOUT", error_detail="pod deletion did not complete within timeout")
        raise _main.StepFailed(_main.infra_error(
            "DELETE_POD",
            "POD_DELETE_TIMEOUT",
            "pod deletion did not complete within timeout",
            rollback=rollback,
            pod_name=pod_name,
        ), 500)

    rollback["podDeleted"] = True
    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  node_name=pod_node_name, resource_type="pod",
                  action=Action.DELETE_POD_K8S, phase=Phase.SUCCESS)
    _main.app.logger.info(f"[DELETE POD] pod deleted successfully: {pod_name}")

def step_cleanup_pod_node_krb5(ctx):
    """지운 Pod가 있던 노드에서 keytab을 정리한다.

    같은 사용자의 다른 Pod가 그 노드에 남아 있으면 지우지 않는다. keytab을 지우면 호스트의
    티켓 갱신 타이머까지 사라져, 살아있는 Pod의 TGT가 만료된 뒤 되살아나지 않는다. 그러면
    sec=krb5로 마운트된 홈이 nobody로 매핑되어 사용자가 자기 홈에 접근하지 못한다.
    계정 회수(step_check_account_revocable)에 있는 보류 규칙과 같은 이유이며, 여기서는
    노드 단위로 본다 — 다른 노드의 Pod는 이 노드의 keytab이 필요 없다."""
    if ctx.get("already_absent"):
        return
    username, pod_node_name = ctx["username"], ctx.get("pod_node_name")
    if not (_main.app.config.get("KRB5_REALM") and pod_node_name):
        return

    try:
        _main.load_k8s()
        pods = client.CoreV1Api().list_namespaced_pod(
            _main.app.config["NAMESPACE"], label_selector=f"username={username}").items
        others = [p.metadata.name for p in pods
                  if p.metadata.name != ctx.get("pod_name")
                  and getattr(p.spec, "node_name", None) == pod_node_name]
    except Exception as e:
        # 남은 Pod를 확인하지 못하면 지우지 않는다. 잘못 지우면 살아있는 사용자의 접근이 끊기고,
        # 남겨 두면 재조정 잡이 고아 후보로 보고한다 — 실패 방향을 안전한 쪽으로 고정한다.
        _main.app.logger.warning(f"[DELETE POD] 남은 Pod 확인 실패로 keytab 정리 보류: {username} @ {pod_node_name} — {e}")
        return

    if others:
        _main.app.logger.info(
            f"[DELETE POD] {username}의 Pod {len(others)}개가 {pod_node_name}에 남아 있어 keytab을 유지한다")
        return

    try:
        _main._remove_krb5_from_farm(username, pod_node_name)
    except Exception as e:
        _main.app.logger.warning(f"[DELETE POD] farm 정리 실패, 재조정 잡에 위임: {username} ← {pod_node_name} — {e}")
        _main._record_krb5_cleanup_pending(username, pod_node_name)

def _new_delete_rollback():
    return {
        "servicesDeleted": False,
        "nodeportsReleased": False,
        "podDeleteRequested": False,
        "podDeleted": False,
    }

POD_DELETE_STEPS = [
    step_delete_services,
    step_release_nodeports,
    step_delete_pod_k8s,
    step_cleanup_pod_node_krb5,
]

def step_check_account_revocable(ctx):
    """계정 회수 전 확인. baseline admin_be가 계정 삭제를 보류하는 두 조건과 같다.
    ① keytab을 지울 farm 노드를 모르면 보류한다. 모르는 채로 지우면 모든 farm 노드를 훑어 같은 이름의
       무관한 계정까지 건드릴 수 있다.
    ② 같은 사용자의 컨테이너가 남아 있으면 보류한다. 같은 작업에서 지운 Pod는 제외한다."""
    username = ctx["username"]
    if _main.app.config.get("KRB5_REALM") and not (ctx.get("node_name") or ctx.get("pod_node_name")):
        _main.app.logger.warning(f"[ACCOUNTS] {username}의 farm 노드를 알 수 없어 계정 회수를 보류")
        raise _main.StepFailed(_main.infra_error(
            "DELETE_ACCOUNT", "ACCOUNT_NODE_UNKNOWN",
            f"farm node of {username!r} is unknown; pass node_name",
        ), 409)
    _main.load_k8s()
    pods = client.CoreV1Api().list_namespaced_pod(
        _main.app.config["NAMESPACE"], label_selector=f"username={username}").items
    remaining = [p.metadata.name for p in pods if p.metadata.name != ctx.get("pod_name")]
    if remaining:
        _main.app.logger.warning(f"[ACCOUNTS] {username}의 컨테이너 {len(remaining)}개가 남아 있어 계정 회수를 보류")
        raise _main.StepFailed(_main.infra_error(
            "DELETE_ACCOUNT", "ACCOUNT_IN_USE",
            f"{len(remaining)} other pod(s) of {username!r} still use this account",
        ), 409)

def step_delete_account(ctx):
    request_id, username, node_name = ctx["request_id"], ctx["username"], ctx.get("node_name")

    _main.log_operation(request_id=request_id, username=username, node_name=node_name,
                  resource_type="account", action=Action.DELETE_ACCOUNT, phase=Phase.START)

    # Remove from /etc/passwd
    lines = _main.read_passwd_lines()
    new_lines = []
    removed_user = None
    for line in lines:
        rec = _main.parse_passwd_line(line)
        if rec and rec["name"] == username:
            removed_user = rec
            continue
        new_lines.append(line)
    if removed_user is None:
        # 이 엔드포인트는 멱등이라 호출자(admin_be)가 404를 "이미 삭제됨"으로 처리한다.
        # 이력에는 이 호출이 아무것도 지우지 않았다는 사실 그대로 남기되, 지표를 뽑을 때
        # 실제 삭제 실패와 섞이지 않도록 error_code로 구분한다. 목표 상태에 이미 도달한
        # 경우를 별도로 표현하는 것은 자원 재조회가 들어오는 v3.0의 몫이다.
        _main.log_operation(request_id=request_id, username=username, node_name=node_name,
                      resource_type="account", action=Action.DELETE_ACCOUNT, phase=Phase.FAIL,
                      error_code="USER_NOT_FOUND",
                      error_detail=f"user {username!r} not present in passwd")
        raise _main.StepFailed({"error": "user not found"}, 404)
    # passwd/shadow/group 세 파일을 지워야 계정 제거가 끝난다. 중간에 실패하면 계정이
    # 반만 지워진 채 남으므로, 그 사실이 이력에 남도록 묶어서 감싼다.
    try:
        _main.write_passwd_lines(new_lines)

        # Remove from /shadow
        sh_lines = _main.read_shadow_lines()
        sh_new = []
        for sl in sh_lines:
            srec = _main.parse_shadow_line(sl)
            if srec and srec["name"] == username:
                continue
            sh_new.append(sl)
        _main.write_shadow_lines(sh_new)

        # Clean /etc/group: remove user from all member lists; delete any group that had this user
        # (either explicitly in members or implicitly as the primary GID group) if now empty.
        g_lines = _main.read_group_lines()
        g_new = []
        for gl in g_lines:
            grec = _main.parse_group_line(gl)
            if not grec:
                g_new.append(gl)
                continue

            had_user_member = username in grec.get("members", [])
            is_primary_group = (removed_user is not None and grec.get("gid") == removed_user.get("gid"))

            # Remove from explicit members list
            if had_user_member:
                grec["members"] = [m for m in grec["members"] if m != username]

            # If this group had the user (explicitly or via primary gid) and is now empty, drop the group
            if (had_user_member or is_primary_group) and not grec.get("members"):
                continue

            g_new.append(_main.format_group_entry(grec))

        _main.write_group_lines(g_new)
    except Exception as e:
        _main.log_operation(request_id=request_id, username=username, node_name=node_name,
                      resource_type="account", action=Action.DELETE_ACCOUNT, phase=Phase.FAIL,
                      error_code="ACCOUNT_FILE_WRITE_FAILED", error_detail=str(e))
        raise

    _main.log_operation(request_id=request_id, username=username, node_name=node_name,
                  resource_type="account", action=Action.DELETE_ACCOUNT, phase=Phase.SUCCESS)

def step_delete_home(ctx):
    request_id, username, node_name = ctx["request_id"], ctx["username"], ctx.get("node_name")

    _main.log_operation(request_id=request_id, username=username, node_name=node_name,
                  resource_type="storage", action=Action.DELETE_HOME, phase=Phase.START)
    try:
        _main.delete_user_home_directory(username)
        _main.log_operation(request_id=request_id, username=username, node_name=node_name,
                      resource_type="storage", action=Action.DELETE_HOME, phase=Phase.SUCCESS)
    except Exception as e:
        _main.app.logger.warning("[ACCOUNTS] home dir deletion failed for user=%s (account files already removed)", username, exc_info=True)
        _main.log_operation(request_id=request_id, username=username, node_name=node_name,
                      resource_type="storage", action=Action.DELETE_HOME, phase=_main._fail_phase(e),
                      error_code="HOME_DELETE_FAILED", error_detail=str(e))

def step_remove_krb5(ctx):
    """keytab을 지울 노드: 호출자가 준 node_name, 없으면 같은 작업에서 지운 Pod가 떠 있던 노드."""
    request_id, username = ctx["request_id"], ctx["username"]
    node_name = ctx.get("node_name") or ctx.get("pod_node_name")

    if not _main.app.config.get("KRB5_REALM"):
        return
    _main.log_operation(request_id=request_id, username=username, node_name=node_name,
                  resource_type="kerberos", action=Action.REMOVE_KRB5, phase=Phase.START)
    try:
        _main._delete_krb5_principal_and_secret(username)
    except Exception as e:
        _main.log_operation(request_id=request_id, username=username, node_name=node_name,
                      resource_type="kerberos", action=Action.REMOVE_KRB5, phase=_main._fail_phase(e),
                      error_code="KRB5_PRINCIPAL_DELETE_FAILED", error_detail=str(e))
        raise
    if node_name:
        try:
            _main._remove_krb5_from_farm(username, node_name)
            _main.log_operation(request_id=request_id, username=username, node_name=node_name,
                          resource_type="kerberos", action=Action.REMOVE_KRB5, phase=Phase.SUCCESS)
        except Exception as e:
            _main.app.logger.warning(f"[KRB5] farm 정리 실패, 재조정 잡에 위임: {node_name} — {e}")
            # principal은 지웠지만 노드의 keytab이 남았다. 재조정 잡이 나중에 치우므로
            # 응답은 성공이지만, 이 시점의 접근 경로는 아직 살아있다 — 회수 완료로
            # 기록됐는데 접근이 남는 경우가 정확히 여기서 나온다.
            _main.log_operation(request_id=request_id, username=username, node_name=node_name,
                          resource_type="kerberos", action=Action.REMOVE_KRB5, phase=_main._fail_phase(e),
                          error_code="KRB5_FARM_CLEANUP_FAILED", error_detail=str(e))
            _main._record_krb5_cleanup_pending(username, node_name)
    else:
        _main.app.logger.warning(
            f"[ACCOUNTS] node_name 없이 사용자 삭제 요청됨 — 설정된 모든 farm 노드를 훑음: {username} "
            "(무관한 farm의 동일 이름 레거시 계정을 건드릴 수 있음, 호출자가 node_name을 넘기도록 수정 필요)"
        )
        _main._remove_krb5_from_all_farms(username)
        _main.log_operation(request_id=request_id, username=username,
                      resource_type="kerberos", action=Action.REMOVE_KRB5, phase=Phase.SUCCESS)

ACCOUNT_DELETE_STEPS = [
    step_delete_account,
    step_delete_home,
    step_remove_krb5,
]
