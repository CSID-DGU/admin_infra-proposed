"""사용이 끝난 계정의 홈 삭제 단계 — 마지막 컨테이너가 끝나고 보존 기간이 지난 홈을 NAS 에서 지운다.
지워진 뒤 그 계정이 다시 승인되면 컨테이너를 만들기 전에 빈 홈을 다시 만든다(step_restore_missing_home).

지우는 것은 되돌릴 수 없다. 그래서 지우기 전에 세 가지를 확인하고, 하나라도 어긋나면 지우지 않고 실패로 끝낸다.
① 그 계정의 컨테이너가 없다 ② 그 계정의 생성·이동 작업이 돌고 있지 않다 ③ 홈의 소유자가 admin_be 가 아는
그 계정의 uid 다. 홈이 이미 없으면 성공이다 — 같은 작업이 두 번 와도 결과가 같다. 확인하지 못한 것은 통과로 치지 않는다.
보존 기간이 지났는지는 admin_be 가 판단한다. 여기서는 지금 지워도 되는 상태인지만 본다.
"""
from kubernetes import client

from adapters.operation_log import Action, Phase


class _MainProxy:
    def __getattr__(self, name):
        import main
        return getattr(main, name)


_main = _MainProxy()

# 홈을 다시 쓰게 만드는 작업. 이 작업이 돌고 있으면 곧 컨테이너가 그 홈을 붙인다.
_HOME_USING_JOB_KINDS = ("provision", "migrate")
# 끝나지 않은 작업을 이만큼까지 훑는다. 평소엔 한 자릿수라 넉넉한 값이다.
_UNFINISHED_JOB_SCAN_LIMIT = 1000


def _refuse(code, detail):
    """지우면 안 되는 상태다. 다시 해도 같은 결과라 재시도하지 않는다."""
    raise _main.StepFailed(_main.infra_error("DELETE_HOME", code, detail), 409, retry=False)


def _require_home_unused(username):
    _main.load_k8s()
    pods = client.CoreV1Api().list_namespaced_pod(
        _main.app.config["NAMESPACE"], label_selector=f"username={username}").items
    if pods:
        _refuse("HOME_IN_USE", f"{len(pods)} pod(s) of {username!r} still use this home")
    unfinished = _main.find_unfinished_jobs(limit=_UNFINISHED_JOB_SCAN_LIMIT)
    if len(unfinished) >= _UNFINISHED_JOB_SCAN_LIMIT:
        # 한도에 걸려 잘린 목록이다. 이 계정의 작업이 잘린 쪽에 있을 수 있으므로 없다고 단정하지 않는다.
        _refuse("HOME_IN_USE", f"too many unfinished jobs to confirm none uses the home of {username!r}")
    running = [kind for kind, _, job_user, _ in unfinished
               if job_user == username and kind in _HOME_USING_JOB_KINDS]
    if running:
        _refuse("HOME_IN_USE", f"{running[0]} job of {username!r} is not finished")


def step_delete_expired_home(ctx):
    request_id, username, expected_uid = ctx["request_id"], ctx["username"], ctx["expected_uid"]

    _require_home_unused(username)

    owner = _main.user_home_owner_uid(username)
    if owner is None:
        # 홈이 없는 것과 홈들이 놓인 경로가 안 보이는 것은 조회 결과가 같다. 뒤쪽을 성공으로 끝내면
        # 지우지 않은 홈이 지운 것으로 기록돼 다시 시도되지 않는다.
        if not _main.home_root_is_reachable():
            _refuse("HOME_ROOT_UNREACHABLE", f"cannot confirm the home of {username!r} is gone")
        ctx["home_deleted"] = False
        _main.app.logger.info(f"[HOME] 지울 홈이 이미 없음: {username}")
        return
    if owner != expected_uid:
        # 같은 이름을 다른 사람이 쓰던 홈일 수 있다. 누구 것인지 모르는 홈은 지우지 않는다.
        _refuse("HOME_OWNER_MISMATCH",
                f"home of {username!r} is owned by uid {owner}, expected {expected_uid}")

    _main.log_operation(request_id=request_id, username=username, resource_type="home",
                        action=Action.DELETE_HOME, phase=Phase.START)
    try:
        _main.delete_user_home_directory(username)
    except Exception as e:
        _main.log_operation(request_id=request_id, username=username, resource_type="home",
                            action=Action.DELETE_HOME, phase=Phase.FAIL,
                            error_code="HOME_DELETE_FAILED", error_detail=str(e)[:1000])
        raise
    _main.log_operation(request_id=request_id, username=username, resource_type="home",
                        action=Action.DELETE_HOME, phase=Phase.SUCCESS)
    ctx["home_deleted"] = True
    _main.app.logger.info(f"[HOME] 보존 기간이 지난 홈 삭제 완료: {username}")


HOME_DELETE_STEPS = [step_delete_expired_home]


def _ledger_ids(username):
    for line in _main.read_passwd_lines():
        rec = _main.parse_passwd_line(line)
        if rec and rec["name"] == username:
            return rec["uid"], rec["gid"]
    return None


def step_restore_missing_home(ctx):
    """이미 있는 계정으로 컨테이너를 만들 때, 홈이 없으면 계정 대장의 uid·gid 로 다시 만든다.

    계정은 남기고 홈만 지우는 경로(보존 기간 경과)가 있어서, 계정이 있다고 홈도 있는 것은 아니다. 홈 없이
    진행하면 노드가 홈 소유자를 끝내 확인하지 못해 keytab 배포 앞에서 한도까지 기다리다 실패한다.
    이미 있는 홈은 사용자 데이터라 손대지 않는다 — 소유자·권한도 그대로 둔다."""
    request_id, username = ctx["request_id"], ctx["username"]
    ids = _ledger_ids(username)
    if ids is None:
        # 대장에 없는 계정은 뒤의 Pod 사양 단계가 제 오류로 멈춘다. 여기서 uid 를 지어내지 않는다.
        return
    uid, gid = ids
    try:
        if _main.user_home_owner_uid(username) is not None:
            return
        if not _main.home_root_is_reachable():
            raise RuntimeError("home root is not reachable on the NAS")
    except Exception as e:
        _main.log_operation(request_id=request_id, username=username, resource_type="storage",
                            action=Action.CREATE_HOME, phase=_main._fail_phase(e),
                            error_code="NAS_SSH_FAILED", error_detail=str(e)[:1000])
        raise _main.StepFailed(_main.infra_error(
            "CREATE_HOME_DIRECTORY", "NAS_SSH_FAILED", f"cannot confirm the home of {username!r}"), 500, cause=e)

    _main.log_operation(request_id=request_id, username=username, resource_type="storage",
                        action=Action.CREATE_HOME, phase=Phase.START)
    try:
        _main.create_user_home_directory(username, uid, gid)
    except Exception as e:
        _main.log_operation(request_id=request_id, username=username, resource_type="storage",
                            action=Action.CREATE_HOME, phase=_main._fail_phase(e),
                            error_code="NAS_SSH_FAILED", error_detail=str(e)[:1000])
        raise _main.StepFailed(_main.infra_error(
            "CREATE_HOME_DIRECTORY", "NAS_SSH_FAILED", f"failed to create home directory for {username}"),
            500, cause=e)
    _main.log_operation(request_id=request_id, username=username, resource_type="storage",
                        action=Action.CREATE_HOME, phase=Phase.SUCCESS)
    _main.app.logger.info(f"[HOME] 없어진 홈을 다시 만듦: {username} uid={uid}")
