"""#146 그룹을 AD 에도 올린다 — 파일에만 쓰면 sec=krb5 NFS 에서 아무 효력이 없다."""
import pytest

import main
from lifecycle_steps import provision


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


# ---------- 그룹 생성 ----------

def test_new_group_goes_to_ad(etc, group_job):
    seed, sent = etc
    r = group_job("create", **{"name": "teamx", "gid": 70000})
    assert r.phase == "SUCCESS"
    assert sent == ["group-create teamx 70000"]


def test_ad_failure_fails_the_job_and_the_same_request_finishes_it(etc, group_job, monkeypatch):
    """실패해도 원장의 줄을 되돌리지 않는다. 되돌리면 다시 만들 때 gid 가 새로 배정돼, AD 에 먼저 남은
    그룹의 gid 와 어긋난다. 같은 요청을 다시 보내면 그 줄의 gid 로 이어서 끝낸다."""
    seed, sent = etc

    def boom(cmd, stdin_data=""):
        raise RuntimeError("AD DC 접속 실패")
    monkeypatch.setattr(main, "_farm_ad_ssh", boom)
    r = group_job("create", name="teamx")
    assert r.phase == "FAIL"
    assert r.error == "AD_GROUP_CREATE_FAILED"

    monkeypatch.setattr(main, "_farm_ad_ssh", lambda cmd, stdin_data="": sent.append(cmd) or "")
    again = group_job("create", name="teamx")
    assert again.phase == "SUCCESS" and again.gid == 70000
    assert sent == ["group-create teamx 70000"]


def test_ad_failure_is_retried_until_it_recovers(etc, group_job, monkeypatch):
    """재시도하는 방식에서는 잠깐 막힌 AD 를 작업이 스스로 넘긴다."""
    seed, sent = etc
    calls = []

    def flaky(cmd, stdin_data=""):
        calls.append(cmd)
        if len(calls) == 1:
            raise RuntimeError("AD DC 접속 실패")
        return ""
    monkeypatch.setattr(main, "_farm_ad_ssh", flaky)
    r = group_job("create", mode="full", name="teamx")
    assert r.phase == "SUCCESS" and r.gid == 70000
    assert calls == ["group-create teamx 70000"] * 2
    with main.app.app_context():
        assert [l for l in main.read_group_lines() if l.startswith("teamx:")] == ["teamx:x:70000:"]


def test_group_line_used_by_others_is_not_taken_over(etc, group_job):
    """같은 이름의 줄에 요청에 없는 멤버가 있으면 남이 쓰는 그룹이다 — 이어 쓰지 않는다."""
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"], group=["teamx:x:70000:bob"])
    r = group_job("create", name="teamx", members=["alice"])
    assert r.phase == "FAIL" and r.error == "GROUP_NAME_EXISTS"
    assert sent == []
    assert _members("teamx") == ["bob"]


def test_registration_stores_the_job_and_same_number_is_409(etc, api, logs, monkeypatch):
    stored = {}
    monkeypatch.setattr(main, "save_job_input",
                        lambda a, k, job: (a, k) not in stored and not stored.update({(a, k): job}))
    body = {"request_id": 7, "op": "create", "name": "teamx"}
    r = api.post("/operations/group", json=body)
    assert r.status_code == 202 and r.get_json()["request_id"] == "7"
    # 컨테이너 신청 번호와 섞이지 않게 작업 기록의 키에는 접두어가 붙는다.
    assert stored[("CHANGE_GROUP", "group-op-7")]["op"] == "create"
    assert logs[0]["request_id"] == "group-op-7" and logs[0]["action"] == main.Action.CHANGE_GROUP
    assert api.post("/operations/group", json=body).status_code == 409


def test_prefix_guard_covers_group_jobs(etc, api, monkeypatch):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"], group=["teamx:x:70000:"])
    monkeypatch.setattr(main, "ACCOUNT_PREFIX", "exp-np-")
    for body in [{"op": "add", "username": "alice", "groups": ["teamx"]},
                 {"op": "remove", "username": "alice", "name": "teamx"},
                 {"op": "create", "name": "teamy", "members": ["alice"]}]:
        assert api.post("/operations/group", json={"request_id": 1, **body}).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("post", "/groups"), ("post", "/users/alice/groups"), ("delete", "/users/alice/groups/teamx"),
    ("post", "/operations/nas-gss-flush"),
])
def test_sync_routes_are_gone(api, method, path):
    assert getattr(api, method)(path, json={}).status_code in (404, 405)


def test_group_name_must_not_collide_with_a_user(etc, group_job):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"])
    r = group_job("create", **{"name": "alice", "gid": 70000})
    assert r.status == 400
    assert r.error == "GROUP_NAME_CONFLICTS_USER"
    assert sent == []          # AD 로 나가기 전에 막혀야 한다


def test_group_name_reserved_by_the_image_is_rejected(etc, group_job):
    """이미지가 이미 가진 이름이면 Pod 가 기동하지 못한다 — 만들기 전에 막는다(#152).
    group 파일 중복 검사로는 못 잡는다. 시드에 없는 이름이라 409 에 걸리지 않기 때문이다."""
    seed, sent = etc
    with main.app.app_context():
        seeded = _group_names()
    for bad in ["render", "docker", "_ssh", "nova", "svmanager"]:
        assert bad not in seeded, f"{bad} 가 시드에 있으면 이 시험이 의미 없다"
        r = group_job("create", **{"name": bad, "gid": 70000})
        assert r.status == 409, bad
        assert r.error == "GROUP_NAME_RESERVED", bad
    assert sent == []          # AD 로 나가기 전에 막혀야 한다
    with main.app.app_context():
        assert _group_names() == seeded


def test_group_name_charset_is_validated(etc, group_job):
    seed, sent = etc
    for bad in ["Team X", "team;rm -rf /", "TEAM", "1team"]:
        r = group_job("create", **{"name": bad, "gid": 70000})
        assert r.status == 400, bad
    assert sent == []


def test_step_retries_ad_failure(etc, monkeypatch):
    seed, sent = etc

    def boom(cmd, stdin_data=""):
        raise ConnectionError("ad ssh refused")
    monkeypatch.setattr(main, "_farm_ad_ssh", boom)
    ctx = {"request_id": "r1", "name": "alice", "supp_groups": [{"name": "teamx", "gid": 70000}]}
    with main.app.app_context():
        with pytest.raises(main.StepFailed) as err:
            provision.step_sync_ad_groups(ctx)
    assert err.value.retry is True
    assert err.value.body["error"] == "AD_GROUP_SYNC_FAILED"


# ---------- 그룹 추가 ----------

def test_adding_user_to_group_goes_to_ad(etc, group_job):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])
    r = group_job("add", username="alice", groups=["teamx"])
    assert r.phase == "SUCCESS"
    assert sent == ["group-addmember teamx alice"]


def test_ad_failure_leaves_the_group_file_untouched(etc, group_job, monkeypatch):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])

    def boom(cmd, stdin_data=""):
        raise RuntimeError("AD DC 접속 실패")
    monkeypatch.setattr(main, "_farm_ad_ssh", boom)
    r = group_job("add", username="alice", groups=["teamx"])
    assert r.phase == "FAIL"
    with main.app.app_context():
        line = [l for l in main.read_group_lines() if l.startswith("teamx:")][0]
        assert main.parse_group_line(line)["members"] == []


# ---------- 그룹 제거 ----------

def _members(name):
    line = [l for l in main.read_group_lines() if l.startswith(f"{name}:")][0]
    return main.parse_group_line(line)["members"]


def test_removing_user_from_group_goes_to_ad_then_file_then_pods(etc, group_job, pod_group_remove):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:alice,bob"])
    r = group_job("remove", username="alice", name="teamx")
    assert r.phase == "SUCCESS"
    assert sent == ["group-removemember teamx alice"]
    assert _members("teamx") == ["bob"]
    assert pod_group_remove == [("alice", ["teamx"])]


def test_removing_a_non_member_still_clears_ad(etc, group_job):
    """파일과 AD 가 어긋나 있을 수 있다 — 파일에 없어도 AD 쪽은 확실히 비운다."""
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:bob"])
    assert group_job("remove", username="alice", name="teamx").phase == "SUCCESS"
    assert sent == ["group-removemember teamx alice"]
    assert _members("teamx") == ["bob"]


def test_revoked_account_membership_can_still_be_removed(etc, group_job):
    seed, sent = etc
    seed(group=["teamx:x:70000:alice"])
    assert group_job("remove", username="alice", name="teamx").phase == "SUCCESS"
    assert _members("teamx") == []


def test_primary_group_is_refused(etc, group_job):
    seed, sent = etc
    seed(passwd=["alice:x:21000:70000::/home/alice:/bin/bash"], group=["teamx:x:70000:"])
    r = group_job("remove", username="alice", name="teamx")
    assert r.status == 409
    assert r.error == "PRIMARY_GROUP"
    assert sent == []


def test_unknown_group_is_not_found(etc, group_job):
    seed, sent = etc
    r = group_job("remove", username="alice", name="nope")
    assert r.status == 404
    assert r.error == "GROUP_NOT_FOUND"
    assert sent == []


@pytest.mark.parametrize("names", [{"username": "Alice", "name": "teamx"}, {"username": "alice", "name": "team x"}])
def test_names_outside_unix_rules_never_reach_ad(etc, group_job, names):
    seed, sent = etc
    seed(group=["teamx:x:70000:alice"])
    r = group_job("remove", **names)
    assert r.status == 400
    assert r.error == "INVALID_REQUEST"
    assert sent == []


def test_ad_failure_on_remove_leaves_the_group_file_untouched(etc, group_job, monkeypatch, pod_group_remove):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"], group=["teamx:x:70000:alice"])

    def boom(cmd, stdin_data=""):
        raise RuntimeError("AD DC 접속 실패")
    monkeypatch.setattr(main, "_farm_ad_ssh", boom)
    r = group_job("remove", username="alice", name="teamx")
    assert r.phase == "FAIL"
    assert r.error == "AD_GROUP_MEMBER_FAILED"
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


def test_new_group_with_members_adds_them_in_ad(etc, group_job):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"])
    r = group_job("create", **{"name": "teamx", "gid": 70000, "members": ["alice"]})
    assert r.phase == "SUCCESS"
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

def test_adding_user_to_group_syncs_running_pods(etc, group_job, pod_group_sync):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:", "teamy:x:70001:"])
    r = group_job("add", username="alice", groups=["teamx", "teamy"])
    assert r.phase == "SUCCESS"
    assert pod_group_sync == [("alice", {"teamx": 70000, "teamy": 70001})]
    assert r.pods == {"synced": [], "failed": []}


def test_pod_sync_failure_does_not_fail_the_request(etc, group_job, monkeypatch):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])
    monkeypatch.setattr(main, "sync_running_pod_groups",
                        lambda u, g: {"synced": [], "failed": ["ailab-alice-1"]})
    r = group_job("add", username="alice", groups=["teamx"])
    assert r.phase == "SUCCESS"
    assert "alice" in next(l for l in main.read_group_lines() if l.startswith("teamx:"))
    assert r.pods["failed"] == ["ailab-alice-1"]


def test_no_pod_sync_when_ad_rejects(etc, group_job, monkeypatch, pod_group_sync):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])

    def boom(cmd, stdin_data=""):
        raise RuntimeError("AD DC 접속 실패")
    monkeypatch.setattr(main, "_farm_ad_ssh", boom)
    assert group_job("add", username="alice", groups=["teamx"]).phase == "FAIL"
    assert pod_group_sync == []
