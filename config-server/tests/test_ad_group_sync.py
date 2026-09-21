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
    # 기본은 "떠 있는 Pod 없음" — 티켓 재발급 경로가 실제 DB 를 건드리지 않게 한다.
    # 재발급을 보는 시험은 farm fixture 가 이걸 덮는다.
    monkeypatch.setattr(main, "_nodes_running_user_pods", lambda u: [])

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


# ---------- #153 그룹 변경 뒤 티켓 강제 재발급 ----------

@pytest.fixture
def farm(monkeypatch):
    """사용자 Pod 가 떠 있는 노드와 farm SSH 를 대역으로 바꾼다."""
    calls = []
    monkeypatch.setattr(main, "_nodes_running_user_pods", lambda u: ["farm6", "farm9"])  # etc 의 기본값을 덮는다
    monkeypatch.setattr(main, "_get_farm_node_info",
                        lambda n: {"name": n, "host": f"10.0.0.{n[-1]}", "port": "22"})
    monkeypatch.setattr(main, "_farm_ssh",
                        lambda host, port, cmd, stdin_data="": calls.append(cmd) or "")
    return calls


def test_group_change_reissues_tickets_on_every_node_with_a_pod(etc, api, farm):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])
    r = api.post("/users/alice/groups", json={"groups": ["teamx"]})
    assert r.status_code == 200
    assert farm == ["refresh alice", "refresh alice"]        # farm6, farm9
    assert r.get_json()["krb5_refreshed_nodes"] == ["farm6", "farm9"]


def test_reissue_failure_does_not_undo_the_group_change(etc, api, farm, monkeypatch):
    """티켓 재발급이 실패해도 그룹 변경은 이미 끝났다 — 되돌릴 방법이 없으면서
    호출자만 재시도하게 만들면 안 된다. 다음 갱신 주기나 Pod 재생성이 따라잡는다."""
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"],
         group=["alice:x:21000:", "teamx:x:70000:"])

    def boom(host, port, cmd, stdin_data=""):
        raise RuntimeError("farm SSH 실패")
    monkeypatch.setattr(main, "_farm_ssh", boom)
    r = api.post("/users/alice/groups", json={"groups": ["teamx"]})
    assert r.status_code == 200
    assert r.get_json()["krb5_refreshed_nodes"] == []
    with main.app.app_context():
        line = [l for l in main.read_group_lines() if l.startswith("teamx:")][0]
        assert main.parse_group_line(line)["members"] == ["alice"]


def test_new_group_with_members_reissues_their_tickets(etc, api, farm):
    seed, sent = etc
    seed(passwd=["alice:x:21000:21000::/home/alice:/bin/bash"])
    r = api.post("/groups", json={"name": "teamx", "gid": 70000, "members": ["alice"]})
    assert r.status_code == 201
    assert sent == ["group-create teamx 70000", "group-addmember teamx alice"]
    assert farm == ["refresh alice", "refresh alice"]


def test_step_reissues_after_syncing_groups(etc, farm, logs):
    seed, sent = etc
    ctx = {"request_id": "r1", "name": "alice", "supp_groups": [{"name": "teamx", "gid": 70000}]}
    with main.app.app_context():
        provision.step_sync_ad_groups(ctx)
    assert sent == ["group-create teamx 70000", "group-addmember teamx alice"]
    assert farm == ["refresh alice", "refresh alice"]
