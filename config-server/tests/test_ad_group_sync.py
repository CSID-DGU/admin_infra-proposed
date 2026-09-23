"""#146 그룹을 AD 에도 올린다 — 파일에만 쓰면 sec=krb5 NFS 에서 아무 효력이 없다."""
import pytest

import main
from lifecycle_steps import provision


@pytest.fixture(autouse=True)
def team_dirs(monkeypatch):
    """NAS 에 팀 디렉터리를 만드는 호출을 가로챈다. 반환: 만든 (이름, gid) 목록"""
    made = []
    monkeypatch.setattr(main, "create_team_directory", lambda name, gid: made.append((name, gid)))
    return made


@pytest.fixture
def etc(tmp_path, monkeypatch):
    """계정 원장을 임시 파일로 돌리고 AD 호출을 가로챈다.
    반환: (seed 함수, AD 로 나간 원격 명령 목록)"""
    base = str(tmp_path / "etc")
    for k, val in list(main.app.config.items()):
        if isinstance(val, str) and val.startswith(main.BASE_ETC_DIR):
            monkeypatch.setitem(main.app.config, k, base + val[len(main.BASE_ETC_DIR):])
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "TEST.REALM")
    monkeypatch.setattr(main, "UID_MIN", 21000)
    monkeypatch.setattr(main, "UID_MAX", 49999)

    sent = []
    monkeypatch.setattr(main, "_farm_ad_ssh", lambda cmd, stdin_data="": sent.append(cmd) or "")

    with main.app.app_context():
        main.ensure_etc_layout()

        def seed(passwd=(), group=()):
            main.write_passwd_lines(main.read_passwd_lines() + list(passwd))
            main.write_group_lines(main.read_group_lines() + list(group))
        yield seed, sent


def _group_names():
    return {r["name"] for l in main.read_group_lines() if (r := main.parse_group_line(l))}


# ---------- POST /groups ----------

def test_new_group_goes_to_ad(etc, api):
    seed, sent = etc
    r = api.post("/groups", json={"name": "teamx", "gid": 70000})
    assert r.status_code == 201
    assert sent == ["group-create teamx 70000"]


def test_ad_failure_rolls_back_the_group_file(etc, api, monkeypatch):
    seed, sent = etc

    def boom(cmd, stdin_data=""):
        raise RuntimeError("AD DC 접속 실패")
    monkeypatch.setattr(main, "_farm_ad_ssh", boom)
    r = api.post("/groups", json={"name": "teamx", "gid": 70000})
    assert r.status_code == 500
    assert r.get_json()["error"] == "AD_GROUP_CREATE_FAILED"
    # 파일에만 있고 AD 에는 없는 "있는데 안 먹는" 그룹이 남으면 안 된다
    with main.app.app_context():
        assert "teamx" not in _group_names()


def test_group_name_must_not_collide_with_a_user(etc, api):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"])
    r = api.post("/groups", json={"name": "alice", "gid": 70000})
    assert r.status_code == 400
    assert r.get_json()["error"] == "GROUP_NAME_CONFLICTS_USER"
    assert sent == []          # AD 로 나가기 전에 막혀야 한다


def test_group_name_reserved_by_the_image_is_rejected(etc, api):
    """이미지가 이미 가진 이름이면 Pod 가 기동하지 못한다 — 만들기 전에 막는다(#152).
    group 파일 중복 검사로는 못 잡는다. 시드에 없는 이름이라 409 에 걸리지 않기 때문이다."""
    seed, sent = etc
    with main.app.app_context():
        seeded = _group_names()
    for bad in ["render", "docker", "_ssh", "nova", "svmanager"]:
        assert bad not in seeded, f"{bad} 가 시드에 있으면 이 시험이 의미 없다"
        r = api.post("/groups", json={"name": bad, "gid": 70000})
        assert r.status_code == 409, bad
        assert r.get_json()["error"] == "GROUP_NAME_RESERVED", bad
    assert sent == []          # AD 로 나가기 전에 막혀야 한다
    with main.app.app_context():
        assert _group_names() == seeded


def test_group_name_charset_is_validated(etc, api):
    seed, sent = etc
    for bad in ["Team X", "team;rm -rf /", "TEAM", "1team"]:
        r = api.post("/groups", json={"name": bad, "gid": 70000})
        assert r.status_code == 400, bad
    assert sent == []


# ---------- 팀 공유 디렉터리 (#154) ----------

def test_new_group_gets_a_team_directory(etc, api, team_dirs):
    seed, sent = etc
    r = api.post("/groups", json={"name": "teamx", "gid": 70000})
    assert r.status_code == 201
    assert team_dirs == [("teamx", 70000)]


def test_team_directory_is_made_after_the_ad_group(etc, api, monkeypatch):
    """NAS 는 AD 그룹을 보고 판정한다. AD 에 없으면 chown 할 gid 도 NAS 가 모른다."""
    seed, sent = etc
    order = []
    monkeypatch.setattr(main, "_farm_ad_ssh", lambda cmd, stdin_data="": order.append("ad") or "")
    monkeypatch.setattr(main, "create_team_directory", lambda name, gid: order.append("dir"))
    api.post("/groups", json={"name": "teamx", "gid": 70000})
    assert order == ["ad", "dir"]


def test_team_directory_failure_rolls_back_the_group_file(etc, api, monkeypatch):
    seed, sent = etc

    def boom(name, gid):
        raise RuntimeError("NAS SSH 실패")
    monkeypatch.setattr(main, "create_team_directory", boom)
    r = api.post("/groups", json={"name": "teamx", "gid": 70000})
    assert r.status_code == 500
    assert r.get_json()["error"] == "TEAM_DIR_CREATE_FAILED"
    with main.app.app_context():
        assert "teamx" not in _group_names()
    # 같은 요청을 다시 보내면 끝까지 간다 — AD 그룹 생성은 멱등이다
    monkeypatch.setattr(main, "create_team_directory", lambda name, gid: None)
    assert api.post("/groups", json={"name": "teamx", "gid": 70000}).status_code == 201


def test_no_team_directory_when_ad_is_disabled(etc, api, monkeypatch, team_dirs):
    seed, sent = etc
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "")
    assert api.post("/groups", json={"name": "teamx", "gid": 70000}).status_code == 201
    assert team_dirs == []


def test_step_fills_in_team_directory_for_older_groups(etc, team_dirs):
    """이 변경 전에 만든 그룹은 디렉터리가 없다 — 멤버가 들어오는 프로비저닝에서 채운다."""
    seed, sent = etc
    ctx = {"request_id": "r1", "name": "alice", "supp_groups": [{"name": "teamx", "gid": 70000}]}
    with main.app.app_context():
        provision.step_sync_ad_groups(ctx)
    assert team_dirs == [("teamx", 70000)]


def test_step_does_not_retry_team_dir_group_mismatch(etc, monkeypatch, logs):
    """gid 가 다른 기존 디렉터리는 사람이 확인해야 풀린다 — 재시도 대상이면 같은 실패만 반복한다."""
    seed, sent = etc

    def mismatch(name, gid):
        raise main.TeamDirGroupMismatch("이미 gid 70001 소유")
    monkeypatch.setattr(main, "create_team_directory", mismatch)
    ctx = {"request_id": "r1", "name": "alice", "supp_groups": [{"name": "teamx", "gid": 70000}]}
    with main.app.app_context():
        with pytest.raises(main.StepFailed) as err:
            provision.step_sync_ad_groups(ctx)
    assert err.value.retry is False
    assert err.value.body["error"] == "TEAM_DIR_GROUP_MISMATCH"
    assert any(r.get("error_code") == "TEAM_DIR_GROUP_MISMATCH" for r in logs)


def test_step_retries_plain_nas_failure(etc, monkeypatch):
    seed, sent = etc

    def boom(name, gid):
        raise ConnectionError("nas ssh refused")
    monkeypatch.setattr(main, "create_team_directory", boom)
    ctx = {"request_id": "r1", "name": "alice", "supp_groups": [{"name": "teamx", "gid": 70000}]}
    with main.app.app_context():
        with pytest.raises(main.StepFailed) as err:
            provision.step_sync_ad_groups(ctx)
    assert err.value.retry is True
    assert err.value.body["error"] == "AD_GROUP_SYNC_FAILED"


# ---------- POST /users/<username>/groups ----------

def test_adding_user_to_group_goes_to_ad(etc, api):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])
    r = api.post("/users/alice/groups", json={"groups": ["teamx"]})
    assert r.status_code == 200
    assert sent == ["group-addmember teamx alice"]


def test_adding_user_to_older_group_fills_in_its_team_directory(etc, api, team_dirs):
    """승인은 신규·재사용 계정 모두 이 경로를 탄다 — 디렉터리가 없던 옛 그룹도 여기서 채운다."""
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])
    assert api.post("/users/alice/groups", json={"groups": ["teamx"]}).status_code == 200
    assert team_dirs == [("teamx", 70000)]


def test_team_dir_failure_on_member_add_leaves_the_group_file_untouched(etc, api, monkeypatch):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])

    def boom(name, gid):
        raise RuntimeError("NAS SSH 실패")
    monkeypatch.setattr(main, "create_team_directory", boom)
    r = api.post("/users/alice/groups", json={"groups": ["teamx"]})
    assert r.status_code == 500
    assert r.get_json()["error"] == "TEAM_DIR_CREATE_FAILED"
    with main.app.app_context():
        line = [l for l in main.read_group_lines() if l.startswith("teamx:")][0]
        assert main.parse_group_line(line)["members"] == []


def test_team_dir_mismatch_on_member_add_is_a_conflict(etc, api, monkeypatch):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])

    def mismatch(name, gid):
        raise main.TeamDirGroupMismatch("이미 gid 70001 소유")
    monkeypatch.setattr(main, "create_team_directory", mismatch)
    r = api.post("/users/alice/groups", json={"groups": ["teamx"]})
    assert r.status_code == 409
    assert r.get_json()["error"] == "TEAM_DIR_GROUP_MISMATCH"


def test_ad_failure_leaves_the_group_file_untouched(etc, api, monkeypatch):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])

    def boom(cmd, stdin_data=""):
        raise RuntimeError("AD DC 접속 실패")
    monkeypatch.setattr(main, "_farm_ad_ssh", boom)
    r = api.post("/users/alice/groups", json={"groups": ["teamx"]})
    assert r.status_code == 500
    with main.app.app_context():
        line = [l for l in main.read_group_lines() if l.startswith("teamx:")][0]
        assert main.parse_group_line(line)["members"] == []


# ---------- DELETE /users/<username>/groups/<groupname> ----------

def _members(name):
    line = [l for l in main.read_group_lines() if l.startswith(f"{name}:")][0]
    return main.parse_group_line(line)["members"]


def test_removing_user_from_group_goes_to_ad_then_file_then_pods(etc, api, pod_group_remove):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:alice,bob"])
    r = api.delete("/users/alice/groups/teamx")
    assert r.status_code == 200
    assert sent == ["group-removemember teamx alice"]
    assert _members("teamx") == ["bob"]
    assert pod_group_remove == [("alice", ["teamx"])]


def test_removing_a_non_member_still_clears_ad(etc, api):
    """파일과 AD 가 어긋나 있을 수 있다 — 파일에 없어도 AD 쪽은 확실히 비운다."""
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:bob"])
    assert api.delete("/users/alice/groups/teamx").status_code == 200
    assert sent == ["group-removemember teamx alice"]
    assert _members("teamx") == ["bob"]


def test_revoked_account_membership_can_still_be_removed(etc, api):
    seed, sent = etc
    seed(group=["teamx:x:70000:alice"])
    assert api.delete("/users/alice/groups/teamx").status_code == 200
    assert _members("teamx") == []


def test_primary_group_is_refused(etc, api):
    seed, sent = etc
    seed(passwd=["alice:x:21000:70000::/home/alice:/bin/bash"], group=["teamx:x:70000:"])
    r = api.delete("/users/alice/groups/teamx")
    assert r.status_code == 409
    assert r.get_json()["error"] == "PRIMARY_GROUP"
    assert sent == []


def test_unknown_group_is_not_found(etc, api):
    seed, sent = etc
    r = api.delete("/users/alice/groups/nope")
    assert r.status_code == 404
    assert r.get_json()["error"] == "GROUP_NOT_FOUND"
    assert sent == []


@pytest.mark.parametrize("path", ["/users/Alice/groups/teamx", "/users/alice/groups/team%20x"])
def test_names_outside_unix_rules_never_reach_ad(etc, api, path):
    seed, sent = etc
    seed(group=["teamx:x:70000:alice"])
    r = api.delete(path)
    assert r.status_code == 400
    assert r.get_json()["error"] == "INVALID_NAME"
    assert sent == []


def test_ad_failure_on_remove_leaves_the_group_file_untouched(etc, api, monkeypatch, pod_group_remove):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"], group=["teamx:x:70000:alice"])

    def boom(cmd, stdin_data=""):
        raise RuntimeError("AD DC 접속 실패")
    monkeypatch.setattr(main, "_farm_ad_ssh", boom)
    r = api.delete("/users/alice/groups/teamx")
    assert r.status_code == 500
    assert r.get_json()["error"] == "AD_GROUP_MEMBER_FAILED"
    assert _members("teamx") == ["alice"]
    assert pod_group_remove == []


# ---------- step_sync_ad_groups ----------

def test_step_creates_group_then_adds_member(etc, logs):
    seed, sent = etc
    ctx = {"request_id": "r1", "name": "alice",
           "supp_groups": [{"name": "teamx", "gid": 70000}, {"name": "teamy", "gid": 70001}]}
    with main.app.app_context():
        provision.step_sync_ad_groups(ctx)
    assert sent == ["group-create teamx 70000", "group-addmember teamx alice",
                    "group-create teamy 70001", "group-addmember teamy alice"]


def test_step_is_skipped_when_ad_is_disabled(etc, monkeypatch):
    seed, sent = etc
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "")
    ctx = {"request_id": "r1", "name": "alice", "supp_groups": [{"name": "teamx", "gid": 70000}]}
    with main.app.app_context():
        provision.step_sync_ad_groups(ctx)
    assert sent == []


def test_step_runs_after_the_ad_user_exists(etc):
    """AD 사용자를 만드는 단계보다 뒤에 있어야 멤버로 넣을 대상이 있다."""
    names = [s.__name__ for s in main.ACCOUNT_CREATE_STEPS]
    assert names.index("step_sync_ad_groups") > names.index("step_create_krb5_principal")


# ---------- step_trigger_nas_gss_flush (#181) ----------

@pytest.fixture
def flush_calls(monkeypatch):
    import reconcile_krb5
    calls = []
    monkeypatch.setattr(reconcile_krb5, "trigger_nas_gss_flush_ondemand", lambda: calls.append(1) or True)
    return calls


def test_reuse_path_triggers_nas_flush_after_ad_sync(etc, flush_calls):
    """재사용 계정은 NAS 에 옛 그룹 목록이 굳어 있다 — AD 를 바꾼 뒤 비워야 새 팀 디렉터리가 열린다."""
    names = [s.__name__ for s in main.SUPP_GROUPS_ONLY_STEPS]
    assert names.index("step_trigger_nas_gss_flush") > names.index("step_sync_ad_groups")
    ctx = {"request_id": "r1", "name": "alice", "supp_groups": [{"name": "teamx", "gid": 70000}]}
    with main.app.app_context():
        provision.step_trigger_nas_gss_flush(ctx)
    assert flush_calls == [1]


def test_nas_flush_is_skipped_without_groups_or_ad(etc, monkeypatch, flush_calls):
    with main.app.app_context():
        provision.step_trigger_nas_gss_flush({"request_id": "r1", "name": "alice", "supp_groups": []})
        monkeypatch.setitem(main.app.config, "KRB5_REALM", "")
        provision.step_trigger_nas_gss_flush(
            {"request_id": "r1", "name": "alice", "supp_groups": [{"name": "teamx", "gid": 70000}]})
    assert flush_calls == []


def test_nas_flush_trigger_failure_does_not_fail_the_job(etc, monkeypatch):
    """flush 는 부가 효과다 — 30분 크론이 안전망이라 작업을 실패시키면 안 된다."""
    import reconcile_krb5

    def boom():
        raise RuntimeError("redis down")
    monkeypatch.setattr(reconcile_krb5, "trigger_nas_gss_flush_ondemand", boom)
    ctx = {"request_id": "r1", "name": "alice", "supp_groups": [{"name": "teamx", "gid": 70000}]}
    with main.app.app_context():
        provision.step_trigger_nas_gss_flush(ctx)


def test_new_group_with_members_adds_them_in_ad(etc, api):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"])
    r = api.post("/groups", json={"name": "teamx", "gid": 70000, "members": ["alice"]})
    assert r.status_code == 201
    assert sent == ["group-create teamx 70000", "group-addmember teamx alice"]


# ---------- DC 폴백: 접속 실패와 거절을 구분한다 ----------

def _fake_ssh_runs(results):
    """subprocess.run 대역. results 는 (returncode, stdout, stderr) 목록."""
    import types
    seq = list(results)
    seen = []

    def run(cmd, **kw):
        seen.append(cmd[-1])
        rc, out, err = seq.pop(0)
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)
    return run, seen


def test_transport_failure_falls_through_to_the_next_dc(monkeypatch):
    monkeypatch.setitem(main.app.config, "FARM_AD_DC_NODES",
                        [{"name": "farm2", "host": "h2", "port": "22"},
                         {"name": "farm6", "host": "h6", "port": "22"}])
    run, seen = _fake_ssh_runs([(255, "", "ssh: connect failed"), (0, "ok", "")])
    monkeypatch.setattr(main.subprocess, "run", run)
    with main.app.app_context():
        assert main._farm_ad_ssh("group-create teamx 70000") == "ok"
    assert len(seen) == 2          # farm2 접속 실패 → farm6 으로 넘어갔다


def test_remote_rejection_stops_immediately_and_keeps_the_real_reason(monkeypatch):
    """DC 들은 같은 samdb 를 복제한다. 거절을 다음 DC 로 넘기면 왕복만 늘고,
    마지막 DC 의 메시지가 진짜 이유를 덮어쓴다."""
    monkeypatch.setitem(main.app.config, "FARM_AD_DC_NODES",
                        [{"name": "farm2", "host": "h2", "port": "22"},
                         {"name": "farm6", "host": "h6", "port": "22"}])
    run, seen = _fake_ssh_runs([(1, "", "refusing to change gidNumber of teamx: 70000 -> 70002"),
                                (0, "ok", "")])
    monkeypatch.setattr(main.subprocess, "run", run)
    with main.app.app_context():
        with pytest.raises(RuntimeError) as e:
            main._farm_ad_ssh("group-create teamx 70002")
    assert len(seen) == 1                                  # farm6 으로 넘어가지 않았다
    assert "refusing to change gidNumber" in str(e.value)  # 진짜 이유가 남았다
    assert "farm2" in str(e.value)


# ---------- 떠 있는 Pod 의 /etc/group (admin_infra_server#25) ----------

def test_adding_user_to_group_syncs_running_pods(etc, api, pod_group_sync):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:", "teamy:x:70001:"])
    r = api.post("/users/alice/groups", json={"groups": ["teamx", "teamy"]})
    assert r.status_code == 200
    assert pod_group_sync == [("alice", {"teamx": 70000, "teamy": 70001})]
    assert r.get_json()["pods"] == {"synced": [], "failed": []}


def test_pod_sync_failure_does_not_fail_the_request(etc, api, monkeypatch):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])
    monkeypatch.setattr(main, "sync_running_pod_groups",
                        lambda u, g: {"synced": [], "failed": ["ailab-alice-1"]})
    r = api.post("/users/alice/groups", json={"groups": ["teamx"]})
    assert r.status_code == 200
    assert "alice" in next(l for l in main.read_group_lines() if l.startswith("teamx:"))
    assert r.get_json()["pods"]["failed"] == ["ailab-alice-1"]


def test_no_pod_sync_when_ad_rejects(etc, api, monkeypatch, pod_group_sync):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])

    def boom(cmd, stdin_data=""):
        raise RuntimeError("AD DC 접속 실패")
    monkeypatch.setattr(main, "_farm_ad_ssh", boom)
    assert api.post("/users/alice/groups", json={"groups": ["teamx"]}).status_code == 500
    assert pod_group_sync == []
