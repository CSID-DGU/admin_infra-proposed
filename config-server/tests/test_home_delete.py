"""보존 기간이 지난 홈 삭제 작업 — 지워도 되는 상태일 때만 NAS 의 홈을 지운다."""
import json
import types

import pytest

import main
from lifecycle_steps import home
from main import Action, Phase

KEY = "home-cleanup-7"
UID = 50001


@pytest.fixture
def store(monkeypatch):
    """Redis 작업 입력 저장소 대역. 키는 (action, request_id)."""
    data = {}

    def save(action, request_id, job):
        if (action, request_id) in data:
            return False
        data[(action, request_id)] = {"state": "queued", "job": job}
        return True

    monkeypatch.setattr(main, "save_job_input", save)
    monkeypatch.setattr(main, "load_job_input", lambda a, r: data.get((a, r)))
    monkeypatch.setattr(main, "mark_job_running", lambda a, r: data[(a, r)].update(state="running"))
    monkeypatch.setattr(main, "delete_job_input", lambda a, r: data.pop((a, r), None))
    return data


@pytest.fixture
def nas(monkeypatch):
    """NAS·클러스터·작업 목록 대역. owner 는 홈 소유자 uid(None 이면 홈 없음), deleted 는 지운 이름 목록."""
    state = types.SimpleNamespace(owner=UID, pods=[], unfinished=[], deleted=[], delete_error=None,
                                  root_reachable=True)

    class FakeV1:
        def list_namespaced_pod(self, namespace, label_selector):
            assert label_selector == "username=alice"
            return types.SimpleNamespace(items=state.pods)

    def delete(username):
        if state.delete_error:
            raise state.delete_error
        state.deleted.append(username)
        state.owner = None

    monkeypatch.setattr(main, "load_k8s", lambda: None)
    monkeypatch.setattr(home.client, "CoreV1Api", FakeV1)
    monkeypatch.setattr(main, "find_unfinished_jobs", lambda limit=100: list(state.unfinished))
    monkeypatch.setattr(main, "user_home_owner_uid", lambda username: state.owner)
    monkeypatch.setattr(main, "delete_user_home_directory", delete)
    monkeypatch.setattr(main, "home_root_is_reachable", lambda: state.root_reachable)
    return state


def _run(store, username="alice", expected_uid=UID):
    """등록된 것으로 두고 제어기가 하듯 작업을 끝까지 실행한다."""
    store[("PURGE_HOME", KEY)] = {"state": "queued", "job": {"username": username, "expected_uid": expected_uid}}
    with main.app.app_context():
        main.run_job("home", KEY, username)


def _home_steps(logs):
    return [(l["phase"], l.get("error_code")) for l in logs if l["action"] == Action.DELETE_HOME]


# ---------- 작업 등록 ----------

def test_registration_returns_202_with_prefixed_key(api, logs, store):
    r = api.post("/operations/home", json={"request_id": 7, "username": "alice", "expected_uid": UID})

    assert r.status_code == 202
    assert r.get_json()["request_id"] == "7" and r.get_json()["status"] == "accepted"
    assert store[("PURGE_HOME", KEY)]["job"] == {"username": "alice", "expected_uid": UID}
    assert len(logs) == 1 and logs[0]["request_id"] == KEY
    assert logs[0]["action"] == Action.PURGE_HOME and logs[0]["phase"] == Phase.START
    assert json.loads(logs[0]["target_state"]) == {"username": "alice", "expected_uid": UID}


def test_registration_deletes_nothing_by_itself(api, logs, store, nas):
    assert api.post("/operations/home",
                    json={"request_id": 7, "username": "alice", "expected_uid": UID}).status_code == 202
    assert nas.deleted == []


def test_same_cleanup_twice_is_409(api, logs, store):
    body = {"request_id": 7, "username": "alice", "expected_uid": UID}
    assert api.post("/operations/home", json=body).status_code == 202
    assert api.post("/operations/home", json=body).status_code == 409
    assert len(logs) == 1


@pytest.mark.parametrize("body", [
    {"request_id": 7, "username": "alice"},
    {"request_id": 7, "username": "alice", "expected_uid": 0},
    {"request_id": 7, "username": "alice", "expected_uid": "abc"},
    {"request_id": 7, "username": "../etc", "expected_uid": UID},
    {"request_id": 7, "username": "a b; rm -rf /", "expected_uid": UID},
    {"request_id": "home-1", "username": "alice", "expected_uid": UID},
    {"username": "alice", "expected_uid": UID},
])
def test_invalid_registration_is_400_and_not_stored(api, logs, store, body):
    assert api.post("/operations/home", json=body).status_code == 400
    assert store == {} and logs == []


def test_prefix_guard_covers_registration(api, logs, store, monkeypatch):
    monkeypatch.setattr(main, "ACCOUNT_PREFIX", "exp-np-")
    r = api.post("/operations/home", json={"request_id": 7, "username": "alice", "expected_uid": UID})
    assert r.status_code == 403
    assert store == {} and logs == []


# ---------- 작업 실행 ----------

def test_job_deletes_home_owned_by_expected_uid(logs, store, nas, monkeypatch):
    saved = {}
    monkeypatch.setattr(main, "save_job_result", lambda a, r, d: saved.update({(a, r): d}))

    _run(store)

    assert nas.deleted == ["alice"]
    assert _home_steps(logs) == [(Phase.START, None), (Phase.SUCCESS, None)]
    assert logs[-1]["action"] == Action.PURGE_HOME and logs[-1]["phase"] == Phase.SUCCESS
    assert saved == {("PURGE_HOME", KEY): {"deleted": True}}
    assert ("PURGE_HOME", KEY) not in store


def test_absent_home_is_success_without_deleting(logs, store, nas, monkeypatch):
    nas.owner = None
    saved = {}
    monkeypatch.setattr(main, "save_job_result", lambda a, r, d: saved.update({(a, r): d}))

    _run(store)

    assert nas.deleted == [] and _home_steps(logs) == []
    assert logs[-1]["phase"] == Phase.SUCCESS
    assert saved == {("PURGE_HOME", KEY): {"deleted": False}}


def test_home_owned_by_another_uid_is_not_deleted(logs, store, nas):
    nas.owner = UID + 1

    _run(store)

    assert nas.deleted == []
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "HOME_OWNER_MISMATCH"
    assert not [l for l in logs if l["phase"] == Phase.RETRY]


def test_home_is_kept_while_a_pod_of_the_user_exists(logs, store, nas):
    nas.pods = [types.SimpleNamespace(metadata=types.SimpleNamespace(name="ailab-alice-1"))]

    _run(store)

    assert nas.deleted == []
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "HOME_IN_USE"
    assert not [l for l in logs if l["phase"] == Phase.RETRY]


@pytest.mark.parametrize("kind", ["provision", "migrate"])
def test_home_is_kept_while_a_job_that_will_mount_it_is_running(logs, store, nas, kind):
    nas.unfinished = [(kind, "31", "alice", 900)]

    _run(store)

    assert nas.deleted == []
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "HOME_IN_USE"


def test_unfinished_jobs_of_other_users_or_kinds_do_not_block(logs, store, nas):
    nas.unfinished = [("provision", "31", "bob", 900), ("home", KEY, "alice", 901), ("password", "x", "alice", 902)]

    _run(store)

    assert nas.deleted == ["alice"]
    assert logs[-1]["phase"] == Phase.SUCCESS


def test_delete_failure_is_recorded_and_not_reported_as_success(logs, store, nas):
    nas.delete_error = RuntimeError("NAS SSH command failed")

    _run(store)

    assert (Phase.FAIL, "HOME_DELETE_FAILED") in _home_steps(logs)
    assert logs[-1]["action"] == Action.PURGE_HOME and logs[-1]["phase"] != Phase.SUCCESS


def test_missing_home_is_not_success_when_the_home_root_is_unreachable(logs, store, nas):
    nas.owner, nas.root_reachable = None, False

    _run(store)

    assert nas.deleted == []
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "HOME_ROOT_UNREACHABLE"


def test_home_is_kept_when_the_unfinished_job_list_is_truncated(logs, store, nas):
    nas.unfinished = [("password", str(i), "bob", i) for i in range(home._UNFINISHED_JOB_SCAN_LIMIT)]

    _run(store)

    assert nas.deleted == []
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "HOME_IN_USE"
