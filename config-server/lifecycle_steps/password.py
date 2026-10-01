"""로그인 비밀번호 교체 단계 — 계정 원장(shadow)·계정 Secret·떠 있는 Pod 를 같은 해시로 맞춘다.

단계 하나가 세 곳을 모두 바꾼다. 중간에 실패하면 옛 해시로 되돌린 뒤 실패로 끝내므로, 되돌리기까지
끝난 실패는 "아무것도 바뀌지 않음"을 뜻한다. 되돌리지 못한 실패만 재시도 대상이다 — 같은 해시를 다시
쓰는 것이라 몇 번을 실행해도 결과가 같다.
"""


class _MainProxy:
    def __getattr__(self, name):
        import main
        return getattr(main, name)


_main = _MainProxy()


def _fail(ctx, old_hash, code, detail, cause=None, **extra):
    """옛 해시로 되돌리고 실패로 끝낸다. 다 되돌렸으면 다시 해도 같은 결과이므로 재시도하지 않는다."""
    username, passwd_hash = ctx["username"], ctx["passwd_hash"]
    # 중단됐다 이어받은 실행은 원장에 이미 새 해시가 들어 있어 옛 해시를 알 수 없다. 되돌릴 수 없으므로
    # 재시도로 새 해시를 끝까지 맞추게 둔다.
    rolled_back = old_hash != passwd_hash and _main._restore_password(username, old_hash)
    # 되돌렸으면 지금 상태를 아는 실패다. 원인이 응답 유실이어도 결과 불명으로 넘기지 않는다.
    raise _main.StepFailed(_main.infra_error("CHANGE_PASSWORD", code, detail, rolled_back=rolled_back, **extra),
                           500, cause=None if rolled_back else cause, retry=not rolled_back)


def step_change_login_password(ctx):
    username, passwd_hash = ctx["username"], ctx["passwd_hash"]

    # 원장을 먼저 바꾼다. 계정이 없으면 여기서 끝나 Secret·Pod 를 건드리지 않는다.
    old_hash = _main.set_ledger_password(username, passwd_hash)
    if old_hash is None:
        raise _main.StepFailed(_main.infra_error(
            "CHANGE_PASSWORD", "USER_NOT_FOUND", f"user not found: {username}"), 404)

    try:
        secrets = _main.update_account_secrets(username, passwd_hash)
    except Exception as e:
        _main.app.logger.exception("[ACCOUNTS] 계정 Secret 비밀번호 교체 실패: %s", username)
        _fail(ctx, old_hash, "SECRET_UPDATE_FAILED", f"failed to update account secrets: {username}", cause=e)

    pods = _main.sync_running_pod_password(username, passwd_hash)
    if pods["failed"] or pods.get("error"):
        _fail(ctx, old_hash, "POD_PASSWORD_SYNC_FAILED",
              f"failed to apply password to running pods: {username}", pods=pods)

    ctx["password_secrets"], ctx["password_pods"] = secrets, pods
    _main.app.logger.info("[ACCOUNTS] 비밀번호 교체 완료: %s secrets=%d pods=%d",
                          username, len(secrets), len(pods["synced"]))


PASSWORD_CHANGE_STEPS = [step_change_login_password]
