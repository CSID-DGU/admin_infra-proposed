"""공용 그룹 작업 단계 — 그룹 생성, 사용자의 그룹 추가·제거.

세 작업 모두 AD(권한의 원천) · 계정 원장(group 파일) · NAS 팀 디렉터리 · 떠 있는 Pod 를 차례로 맞춘다.
어느 조각이든 이미 맞춰져 있으면 그대로 두고 다음으로 가므로, 중간에 끊긴 작업은 처음부터 다시 실행하면
이어서 끝난다. 그래서 실패해도 앞서 한 일을 되돌리지 않는다 — 되돌리면 AD 에 남은 그룹과 다시 배정한
gid 가 어긋나 같은 요청을 다시 처리할 수 없게 된다.

입력 검사(check_*)는 등록 경로도 함께 쓴다. 등록 때 걸러 바로 답하고, 실행 직전에 한 번 더 본다(등록과
실행 사이에 원장이 바뀔 수 있다).
"""


import json

from adapters.operation_log import Action, Phase


class _MainProxy:
    def __getattr__(self, name):
        import main
        return getattr(main, name)


_main = _MainProxy()


def _failed(op, code, detail, status, cause=None, retry=True):
    return _main.StepFailed(_main.infra_error(op, code, detail), status, cause=cause, retry=retry)


def _ledger_users():
    return {r["name"]: r for line in _main.read_passwd_lines() if (r := _main.parse_passwd_line(line))}


def _ledger_groups():
    return {r["name"]: r for line in _main.read_group_lines() if (r := _main.parse_group_line(line))}


def check_create(name, gid, members):
    users = _ledger_users()
    # AD 에서 사용자와 그룹은 sAMAccountName 을 공유한다 — 같은 이름이면 나중에 계정 생성이 실패한다(#146).
    if name in users:
        raise _failed("ADD_GROUP", "GROUP_NAME_CONFLICTS_USER",
                      f"group name collides with an existing user: {name}", 400)
    # 이미지가 이미 쥔 이름이면 Pod 가 기동하지 못한다(#152).
    if name in _main.RESERVED_GROUP_NAMES:
        raise _failed("ADD_GROUP", "GROUP_NAME_RESERVED",
                      f"group name is reserved by the container image: {name}", 409)
    invalid = [m for m in members if m not in users]
    if invalid:
        raise _failed("ADD_GROUP", "INVALID_GROUP_MEMBER",
                      f"invalid members (users not found): {', '.join(invalid)}", 400)
    # 호출자가 gid 를 직접 주는 경로 — 개인 그룹 대역(=uid 대역)을 침범하면 여기서 막는다.
    low, high = _main.SHARED_GID_MIN, _main.SHARED_GID_MAX
    if gid is not None and (gid < low or (high is not None and gid > high)):
        raise _failed("ADD_GROUP", "GID_OUT_OF_RANGE", f"gid {gid} outside shared gid range {low}~{high}", 400)


def check_add(username, groups):
    if username not in _ledger_users():
        raise _failed("ADD_USER_GROUPS", "USER_NOT_FOUND", f"user not found: {username}", 404)
    known = _ledger_groups()
    missing = [g for g in groups if g not in known]
    if missing:
        raise _failed("ADD_USER_GROUPS", "GROUP_NOT_FOUND", f"groups not found: {', '.join(missing)}", 404)


def check_remove(username, groupname):
    group = _ledger_groups().get(groupname)
    if not group:
        raise _failed("REMOVE_USER_GROUP", "GROUP_NOT_FOUND", f"group not found: {groupname}", 404)
    # 회수된 계정은 passwd 에 없을 수 있다 — 그때도 남은 멤버십을 치울 수 있게 거부하지 않는다.
    user = _ledger_users().get(username)
    if user and user["gid"] == group["gid"]:
        raise _failed("REMOVE_USER_GROUP", "PRIMARY_GROUP",
                      f"{groupname} is the primary group of {username}", 409)


def _issue_shared_gid(name, lines):
    """공용 gid 대역에서 다음 번호를 정한다. 원장 잠금 안에서 불러야 한다 — 정한 번호를 줄로 쓰고 발급 기록에
    올리기 전에 다른 작업이 같은 번호를 집지 않게 한다."""
    try:
        issued_max = _main.read_issued_id_max("shared_gid")
    except Exception as e:
        _main.app.logger.exception("[ACCOUNTS] gid 발급 기록을 읽지 못해 그룹 생성 중단: %s", name)
        raise _failed("ADD_GROUP", "ISSUED_ID_RECORD_FAILED", "cannot read issued gid record", 500, cause=e)
    gid = _main._allocate_next_gid(lines, min_gid=_main.SHARED_GID_MIN, issued_max=issued_max)
    if _main.SHARED_GID_MAX is not None and gid > _main.SHARED_GID_MAX:
        raise _failed("ADD_GROUP", "GID_RANGE_EXHAUSTED",
                      f"gid range {_main.SHARED_GID_MIN}~{_main.SHARED_GID_MAX} exhausted", 500, retry=False)
    return gid


def _in_shared_band(gid):
    return gid >= _main.SHARED_GID_MIN and (_main.SHARED_GID_MAX is None or gid <= _main.SHARED_GID_MAX)


def _shared_group_gid(name, username):
    """새 공유 그룹(gid 없이 온 보조 그룹)의 gid. 원장에 같은 이름의 줄이 있으면 그 gid 를, 없으면 새로 발급해
    멤버 없는 줄을 써 두고 그 gid 를 돌려준다. 멤버 추가·AD·팀 디렉터리는 뒤의 계정·그룹 단계가 맞춘다.

    같은 이름의 줄은 앞선 시도(이 작업의 이어하기, 같은 그룹을 고른 다른 신청의 실패한 작업)가 남긴 것이다 —
    admin_be 는 자기 DB 에 있는 이름으로는 새 그룹을 만들지 않고, 같은 그룹의 생성 작업이 둘 동시에 돌지 않게
    막는다. 그래서 멤버가 있어도 이어받는다(기존 계정의 실패한 작업은 멤버를 되돌리지 않는다). 공용 대역 밖의
    줄(개인·시스템 그룹)은 이어받지 않는다."""
    _main.ensure_etc_layout()
    with _main.ledger_lock(), _main.LockedFile(_main.app.config["GROUP_PATH"], "r+") as f:
        lines = f.read().splitlines()
        existing = next((r for line in lines if (r := _main.parse_group_line(line)) and r["name"] == name), None)
        if existing:
            gid = existing["gid"]
            if not _in_shared_band(gid):
                raise _failed("ADD_GROUP", "GROUP_NAME_EXISTS",
                              f"group name is taken outside the shared gid range: {name} ({gid})", 409, retry=False)
            others = sorted(set(existing["members"]) - {username})
            if others:
                # 감사용 기록. 실사용자 이름은 남기지 않는다.
                _main.app.logger.warning("[ACCOUNTS] 새 공유 그룹이 원장에 이미 있어 이어받음(다른 멤버 %d명): %s(%s)",
                                         len(others), name, gid)
            return gid
        gid = _issue_shared_gid(name, lines)
        try:
            _main.record_issued_id("shared_gid", gid)
        except Exception as e:
            _main.app.logger.exception("[ACCOUNTS] gid 발급 기록 실패로 그룹 생성 중단: %s(%s)", name, gid)
            raise _failed("ADD_GROUP", "ISSUED_ID_RECORD_FAILED", "cannot record issued gid", 500, cause=e)
        lines.append(_main.format_group_entry({"name": name, "passwd": "x", "gid": gid, "members": []}))
        f.seek(0)
        f.write("\n".join(lines) + "\n")
        f.truncate()
    _main.app.logger.info("[ACCOUNTS] 새 공유 그룹 gid 발급: %s(%s)", name, gid)
    return gid


def step_resolve_new_groups(ctx):
    """생성 작업의 보조 그룹 중 gid 없이 온 것(admin_be 의 승인 대기 그룹)에 gid 를 정해 ctx["supp_groups"]에 채운다.
    뒤의 계정·그룹 단계는 모두 gid 를 전제로 하므로 맨 앞에서 돈다.

    이어하기 때도 매번 다시 돈다(ALWAYS_RERUN) — 정한 gid 는 이어하기 컨텍스트에 남지 않고 작업 입력에서 다시
    만들어진다. 같은 이름의 줄을 이어받으므로 몇 번 돌아도 같은 gid 가 나온다."""
    supp_groups = ctx.get("supp_groups") or []
    if all(sg.get("gid") is not None for sg in supp_groups):
        return
    username = ctx.get("name") or ctx["username"]
    # 작업 단계 기록에 "새 공유 그룹 준비"로 남긴다. action 은 계정 단계와 같은 CREATE_ACCOUNT 를 쓴다 — 작업
    # 단위 action(CHANGE_GROUP 등)으로 START 를 남기면 제어기가 끝나지 않은 그룹 작업으로 잘못 집는다.
    log = lambda phase, **kw: _main.log_operation(request_id=ctx["request_id"], username=username,
                                                   resource_type="new_groups", action=Action.CREATE_ACCOUNT,
                                                   phase=phase, **kw)
    log(Phase.START)
    resolved = []
    try:
        for sg in supp_groups:
            if sg.get("gid") is not None:
                resolved.append(sg)
                continue
            # 등록과 실행 사이에 원장이 바뀔 수 있어 실행 직전에 이름을 본다(계정명 충돌·이미지 예약 이름).
            check_create(sg["name"], None, [])
            resolved.append({**sg, "gid": _shared_group_gid(sg["name"], username)})
    except _main.StepFailed as e:
        code = e.body.get("error") if isinstance(e.body, dict) else None
        log(_main._fail_phase(e), error_code=str(code or "NEW_GROUP_FAILED")[:64], error_detail=str(e.body)[:1000])
        raise
    except Exception as e:
        log(_main._fail_phase(e), error_code="NEW_GROUP_FAILED", error_detail=str(e)[:1000])
        raise
    ctx["supp_groups"] = resolved
    log(Phase.SUCCESS, error_detail=json.dumps({"groups": {sg["name"]: sg["gid"] for sg in resolved}}))


def _write_group_line(name, gid, members):
    """그룹 줄을 원장에 쓰고 gid 를 돌려준다. 같은 이름의 줄이 이미 있으면 그 줄을 이어 쓴다 — 앞선 시도가
    남긴 줄이다(등록 전에 admin_be 가 자기 DB 로 이름 중복을 거른다). 다른 gid 를 요구하거나 요청에 없는
    멤버가 들어 있는 줄은 남이 쓰는 그룹이므로 건드리지 않는다."""
    _main.ensure_etc_layout()
    with _main.ledger_lock(), _main.LockedFile(_main.app.config["GROUP_PATH"], "r+") as f:
        lines = f.read().splitlines()
        records = [_main.parse_group_line(line) for line in lines]
        existing = next((r for r in records if r and r["name"] == name), None)
        if existing:
            if (gid is not None and existing["gid"] != gid) or set(existing["members"]) - set(members):
                raise _failed("ADD_GROUP", "GROUP_NAME_EXISTS", f"group already exists (name: {name})", 409)
            gid = existing["gid"]
        elif gid is None:
            gid = _issue_shared_gid(name, lines)
        elif any(r and r["gid"] == gid for r in records):
            raise _failed("ADD_GROUP", "GROUP_GID_EXISTS", f"group already exists (gid: {gid})", 409)
        if not existing:
            # 직접 준 gid(옛 팀을 원래 번호로 되살리는 운영 경로)도 기록해, 자동 배정이 그 번호를 다시 주지 않게 한다.
            try:
                _main.record_issued_id("shared_gid", gid)
            except Exception as e:
                _main.app.logger.exception("[ACCOUNTS] gid 발급 기록 실패로 그룹 생성 중단: %s(%s)", name, gid)
                raise _failed("ADD_GROUP", "ISSUED_ID_RECORD_FAILED", "cannot record issued gid", 500, cause=e)
        entry = _main.format_group_entry({"name": name, "passwd": "x", "gid": gid, "members": sorted(members)})
        lines = [entry if r and r["name"] == name else line for r, line in zip(records, lines)]
        if not existing:
            lines.append(entry)
        f.seek(0)
        f.write("\n".join(lines) + "\n")
        f.truncate()
    return gid


def _ensure_team_dirs(op, gids):
    try:
        for name, gid in sorted(gids.items()):
            _main._ensure_team_dir(name, gid)
    except _main.TeamDirGroupMismatch as e:
        # 이미 있는 디렉터리의 gid 가 다르면 사람이 NAS 를 확인해야 풀린다 — 재시도는 같은 결과만 반복한다.
        _main.app.logger.error("[ACCOUNTS] 팀 디렉터리 gid 불일치: %s", e)
        raise _failed(op, "TEAM_DIR_GROUP_MISMATCH", str(e), 409, retry=False)
    except Exception as e:
        _main.app.logger.exception("[ACCOUNTS] 팀 디렉터리 생성 실패: %s", sorted(gids))
        raise _failed(op, "TEAM_DIR_CREATE_FAILED",
                      f"failed to create team directories: {', '.join(sorted(gids))}", 500, cause=e)


def _flush_nas_group_cache():
    """NAS 는 맺어 둔 GSS 컨텍스트의 옛 그룹 목록을 쓴다 — 비워야 떠 있는 세션에 반영된다(#161).
    실패해도 30분 크론이 같은 일을 하므로 작업을 실패시키지 않는다."""
    if not _main._ad_enabled():
        return
    try:
        # reconcile_krb5 는 main 을 import 한다 — 순환을 피하려고 늦게 불러온다.
        from reconcile_krb5 import trigger_nas_gss_flush_ondemand
        trigger_nas_gss_flush_ondemand()
    except Exception:
        _main.app.logger.exception("[NAS GSS 온디맨드] 그룹 변경 후 트리거 실패 — 30분 크론에 맡김")


def step_group_create(ctx):
    name, members = ctx["group_name"], ctx["group_members"]
    check_create(name, ctx.get("group_requested_gid"), members)
    gid = _write_group_line(name, ctx.get("group_requested_gid"), members)
    # AD 에 올려야 NAS 가 이 그룹을 인정한다(#146). 팀 디렉터리는 그 뒤에 만든다 — AD 에 없는 gid 는 NAS 가 모른다.
    try:
        _main._create_ad_group(name, gid)
        for member in sorted(members):
            _main._add_ad_group_member(name, member)
    except Exception as e:
        _main.app.logger.exception("[ACCOUNTS] AD 그룹 생성 실패: %s(%s)", name, gid)
        raise _failed("ADD_GROUP", "AD_GROUP_CREATE_FAILED", f"failed to create group in AD: {name}", 500, cause=e)
    _ensure_team_dirs("ADD_GROUP", {name: gid})
    ctx["group_gid"] = gid


def step_group_add_member(ctx):
    username, groups = ctx["username"], ctx["group_names"]
    check_add(username, groups)
    known = _ledger_groups()
    gids = {g: known[g]["gid"] for g in groups}
    # AD 를 먼저 맞춘다 — 실패해도 group 파일이 더럽혀지지 않는다.
    try:
        for g in sorted(gids):
            _main._add_ad_group_member(g, username)
    except Exception as e:
        _main.app.logger.exception("[ACCOUNTS] AD 그룹 멤버 추가 실패: %s -> %s", username, sorted(gids))
        raise _failed("ADD_USER_GROUPS", "AD_GROUP_MEMBER_FAILED",
                      f"failed to add {username} to groups in AD", 500, cause=e)
    # 팀 디렉터리가 생기기 전에 만든 그룹도 여기서 채워야 멤버가 같이 쓸 자리가 생긴다(#154).
    _ensure_team_dirs("ADD_USER_GROUPS", gids)
    _main._set_group_membership(gids, username, member=True)
    # 이미 떠 있는 Pod 는 기동 때 구운 /etc/group 을 그대로 쓴다(admin_infra_server#25). 권한 원천(AD)은
    # 이미 반영됐으므로 실패해도 작업은 성공으로 둔다.
    ctx["group_pods"] = _main.sync_running_pod_groups(username, gids)
    _flush_nas_group_cache()


def step_group_remove_member(ctx):
    username, groupname = ctx["username"], ctx["group_name"]
    check_remove(username, groupname)
    # 추가와 같은 순서 — AD 가 실패하면 group 파일은 그대로 두어 재시도가 같은 상태에서 시작한다.
    try:
        _main._remove_ad_group_member(groupname, username)
    except Exception as e:
        _main.app.logger.exception("[ACCOUNTS] AD 그룹 멤버 제거 실패: %s -> %s", username, groupname)
        raise _failed("REMOVE_USER_GROUP", "AD_GROUP_MEMBER_FAILED",
                      f"failed to remove {username} from {groupname} in AD", 500, cause=e)
    _main._set_group_membership([groupname], username, member=False)
    ctx["group_pods"] = _main.remove_running_pod_groups(username, [groupname])
    _flush_nas_group_cache()


GROUP_STEPS = {
    "create": [step_group_create],
    "add": [step_group_add_member],
    "remove": [step_group_remove_member],
}
