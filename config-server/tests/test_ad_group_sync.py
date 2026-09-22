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


# ---------- POST /users/<username>/groups ----------

def test_adding_user_to_group_goes_to_ad(etc, api):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])
    r = api.post("/users/alice/groups", json={"groups": ["teamx"]})
    assert r.status_code == 200
    assert sent == ["group-addmember teamx alice"]


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
