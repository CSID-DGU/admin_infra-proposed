"""계정 로그인 비밀번호 교체 작업 — 원장(shadow)·계정 Secret·떠 있는 Pod 를 같은 해시로 맞춘다."""
import json
import types

import pytest

import main
import utils
from main import Action, Phase

HASH = "$6$saltsalt$" + "a" * 86
OTHER_HASH = "$6$oldsalt$" + "b" * 86


@pytest.fixture
def etc(tmp_path, monkeypatch):
    """계정 원장을 임시 파일로 돌린다. 반환: shadow 에 줄을 더하는 함수"""
    base = str(tmp_path / "etc")
    for k, val in list(main.app.config.items()):
        if isinstance(val, str) and val.startswith(main.BASE_ETC_DIR):
            monkeypatch.setitem(main.app.config, k, base + val[len(main.BASE_ETC_DIR):])
    with main.app.app_context():
        main.ensure_etc_layout()

        def seed(*names):
            main.write_shadow_lines(main.read_shadow_lines() + [
                main.format_shadow_entry({"name": n, "passwd": OTHER_HASH, "lastchg": 1}) for n in names])
        yield seed


def _shadow(name):
    with main.app.app_context():
        return next(r for l in main.read_shadow_lines() if (r := main.parse_shadow_line(l)) and r["name"] == name)


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


KEY = "password-reset-12"


def _run(store, username="alice", passwd_hash=HASH):
    """등록된 것으로 두고 제어기가 하듯 작업을 끝까지 실행한다."""
    store[("CHANGE_PASSWORD", KEY)] = {"state": "queued", "job": {"username": username, "passwd_hash": passwd_hash}}
    with main.app.app_context():
        main.run_job("password", KEY, username)


# ---------- 작업 등록 ----------

def test_registration_returns_202_and_keeps_hash_out_of_the_log(api, logs, store):
    r = api.post("/operations/password", json={"request_id": 12, "username": "alice", "passwd_hash": HASH})

    assert r.status_code == 202
    assert r.get_json()["request_id"] == "12" and r.get_json()["status"] == "accepted" and "job_id" in r.get_json()
    # 컨테이너 신청 번호와 섞이지 않게 작업 기록의 키에는 접두어가 붙는다.
    assert store[("CHANGE_PASSWORD", KEY)]["job"] == {"username": "alice", "passwd_hash": HASH}
    assert len(logs) == 1 and logs[0]["request_id"] == KEY
    assert logs[0]["action"] == Action.CHANGE_PASSWORD and logs[0]["phase"] == Phase.START
    assert logs[0]["start_job"] is True
    assert json.loads(logs[0]["target_state"]) == {"username": "alice"}


def test_registration_changes_nothing_by_itself(etc, api, logs, store, pod_password_sync):
    etc("alice")
    assert api.post("/operations/password",
                    json={"request_id": 12, "username": "alice", "passwd_hash": HASH}).status_code == 202
    assert _shadow("alice")["passwd"] == OTHER_HASH
    assert pod_password_sync == []


def test_same_reset_twice_is_409(api, logs, store):
    body = {"request_id": 12, "username": "alice", "passwd_hash": HASH}
    assert api.post("/operations/password", json=body).status_code == 202
    assert api.post("/operations/password", json=body).status_code == 409
    assert len(logs) == 1


@pytest.mark.parametrize("body", [
    {"request_id": 12, "username": "alice"},
    {"request_id": 12, "username": "alice", "passwd_hash": "plain-text"},
    {"request_id": 12, "username": "alice", "passwd_hash": HASH + "\nroot:x"},
    {"request_id": 12, "username": "Bad;Name", "passwd_hash": HASH},
    {"request_id": "pw-1", "username": "alice", "passwd_hash": HASH},
    {"username": "alice", "passwd_hash": HASH},
])
def test_invalid_registration_is_400_and_not_stored(api, logs, store, body):
    assert api.post("/operations/password", json=body).status_code == 400
    assert store == {} and logs == []


def test_prefix_guard_covers_registration(api, logs, store, monkeypatch):
    monkeypatch.setattr(main, "ACCOUNT_PREFIX", "exp-np-")
    r = api.post("/operations/password", json={"request_id": 12, "username": "alice", "passwd_hash": HASH})
    assert r.status_code == 403
    assert store == {} and logs == []


def test_sync_route_is_gone(api):
    assert api.put("/accounts/users/alice/password", json={"passwd_hash": HASH}).status_code in (404, 405)


def test_result_is_looked_up_by_the_same_number(api, monkeypatch):
    """조회도 등록할 때 쓴 번호 그대로 받는다 — 접두어는 config-server 안에서만 붙는다."""
    seen = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            seen.append(params)

        def fetchone(self):
            return ("SUCCESS", None, "2026-10-01 00:00:00", 77)

    class Conn:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(main, "get_log_db_connection", lambda **kw: Conn())
    monkeypatch.setattr(main, "load_job_result", lambda a, r: {"key": r})

    body = api.get("/operations/password/12").get_json()

    assert seen == [(KEY, "CHANGE_PASSWORD")]
    assert body["phase"] == "SUCCESS" and body["job_id"] == 77 and body["request_id"] == "12"
    assert body["result"] == {"key": KEY}


# ---------- 작업 실행 ----------

def test_job_updates_ledger_secrets_and_pods(etc, logs, store, pod_password_sync):
    etc("alice", "bob")

    _run(store)

    assert _shadow("alice")["passwd"] == HASH
    assert _shadow("bob")["passwd"] == OTHER_HASH
    assert pod_password_sync == [("secrets", "alice", HASH), ("pods", "alice", HASH)]
    assert logs[-1]["action"] == Action.CHANGE_PASSWORD and logs[-1]["phase"] == Phase.SUCCESS
    assert logs[-1]["request_id"] == KEY
    assert ("CHANGE_PASSWORD", KEY) not in store


def test_job_result_carries_what_was_changed(etc, logs, store, monkeypatch):
    etc("alice")
    saved = {}
    monkeypatch.setattr(main, "update_account_secrets", lambda u, h: ["a-account"])
    monkeypatch.setattr(main, "sync_running_pod_password", lambda u, h: {"synced": ["a"], "failed": []})
    monkeypatch.setattr(main, "save_job_result", lambda a, r, d: saved.update({(a, r): d}))

    _run(store)

    assert saved == {("CHANGE_PASSWORD", KEY): {"secrets": ["a-account"], "pods": {"synced": ["a"], "failed": []}}}


def test_unknown_user_fails_without_retry_and_touches_nothing(etc, logs, store, pod_password_sync):
    etc("bob")

    _run(store)

    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "USER_NOT_FOUND"
    assert not [l for l in logs if l["phase"] == Phase.RETRY]
    assert pod_password_sync == []


def test_hash_is_never_written_to_the_log(etc, logs, store, monkeypatch):
    etc("alice")
    monkeypatch.setattr(main, "sync_running_pod_password", lambda u, h: {"synced": [], "failed": ["p1"]})

    _run(store)

    assert HASH not in json.dumps(logs, default=str) and OTHER_HASH not in json.dumps(logs, default=str)


# ---------- utils: Secret·Pod 반영 ----------

def _pod(name, phase="Running"):
    return types.SimpleNamespace(metadata=types.SimpleNamespace(name=name),
                                 status=types.SimpleNamespace(phase=phase))


@pytest.fixture
def k8s(monkeypatch):
    """Pod 목록·Secret patch·exec 를 대역으로 바꾼다. 반환: (Secret 없는 이름 집합, pods, patch 호출, exec 호출)"""
    missing, pods, patches, execs = set(), [], [], []

    class FakeV1:
        def patch_namespaced_secret(self, name, namespace, body):
            if name in missing:
                raise utils.client.exceptions.ApiException(status=404)
            patches.append((name, body))

        def list_namespaced_pod(self, namespace, label_selector):
            assert label_selector == "username=alice"
            return types.SimpleNamespace(items=pods)

        def connect_get_namespaced_pod_exec(self, *a, **k):
            raise AssertionError("stream 을 거쳐야 한다")

    class FakeWs:
        """stdin 을 쓰는 exec(_preload_content=False) 대역. 쓴 입력을 기록하고 __RC=0 을 한 번 돌려준다."""
        def __init__(self, name, command):
            self.name, self.command, self.stdin, self._open, self._out = name, command, "", True, "__RC=0\n"

        def write_stdin(self, data):
            self.stdin += data

        def is_open(self):
            return self._open

        def update(self, timeout=None):
            pass

        def peek_stdout(self):
            return bool(self._out)

        def read_stdout(self):
            out, self._out, self._open = self._out, "", False
            return out

        def peek_stderr(self):
            return False

        def close(self):
            self._open = False

    def fake_stream(fn, name, namespace, command, **kw):
        if kw.get("stdin"):
            ws = FakeWs(name, command)
            execs.append((name, command, ws))
            return ws
        execs.append((name, command, None))
        return "__RC=0"

    monkeypatch.setattr(utils, "load_k8s", lambda: None)
    monkeypatch.setattr(utils.client, "CoreV1Api", FakeV1)
    monkeypatch.setattr(utils, "stream", fake_stream)
    return missing, pods, patches, execs


def test_every_pod_secret_gets_the_new_hash_even_when_stopped(k8s):
    missing, pods, patches, execs = k8s
    pods += [_pod("b"), _pod("a", "Pending"), _pod("c")]
    missing.add("c-account")  # 만드는 중이라 Secret 이 아직 없다
    with main.app.app_context():
        assert utils.update_account_secrets("alice", HASH) == ["a-account", "b-account"]
    assert patches == [(n, {"stringData": {"USER_PW_HASH": HASH}}) for n in ("b-account", "a-account")]
    assert execs == []


def test_secret_error_other_than_missing_is_raised(k8s, monkeypatch):
    missing, pods, patches, execs = k8s
    pods.append(_pod("a"))

    def forbidden(*a, **k):
        raise utils.client.exceptions.ApiException(status=403)
    monkeypatch.setattr(utils.client.CoreV1Api, "patch_namespaced_secret", forbidden)
    with main.app.app_context(), pytest.raises(utils.client.exceptions.ApiException):
        utils.update_account_secrets("alice", HASH)


def test_running_pods_get_hash_through_stdin_not_argv(k8s):
    """인자로 넘기면 Pod 안의 프로세스 목록에 해시가 보인다."""
    missing, pods, patches, execs = k8s
    pods += [_pod("p1"), _pod("p2", "Pending")]
    with main.app.app_context():
        assert utils.sync_running_pod_password("alice", HASH) == {"synced": ["p1"], "failed": []}
    name, command, ws = execs[0]
    assert [e[0] for e in execs] == ["p1"]
    assert command[:2] == ["/bin/sh", "-c"] and "chpasswd -e" in command[2]
    assert command[4:] == ["alice"]
    assert all(HASH not in part for part in command)
    assert ws.stdin == HASH + "\n"


# ---------- 되돌리기 ----------

def test_pod_failure_restores_previous_hash_and_fails_cleanly(etc, logs, store, monkeypatch):
    """되돌리기까지 끝난 실패는 아무것도 바뀌지 않은 실패다 — 재시도하지 않고, 관리자에게 넘기는 DEGRADED 도 아니다."""
    etc("alice")
    calls = []
    monkeypatch.setattr(main, "update_account_secrets", lambda u, h: calls.append(("secrets", h)) or [])

    def pods(u, h):
        calls.append(("pods", h))
        return {"synced": [], "failed": ["p1"]} if h == HASH else {"synced": ["p1"], "failed": []}
    monkeypatch.setattr(main, "sync_running_pod_password", pods)

    _run(store)

    assert calls == [("secrets", HASH), ("pods", HASH), ("secrets", OTHER_HASH), ("pods", OTHER_HASH)]
    assert _shadow("alice")["passwd"] == OTHER_HASH
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "POD_PASSWORD_SYNC_FAILED"
    assert not [l for l in logs if l["phase"] == Phase.RETRY]
    detail = json.loads(logs[-1]["error_detail"])["error"]
    assert detail["rolled_back"] is True and detail["pods"] == {"synced": [], "failed": ["p1"]}


def test_pod_list_failure_is_a_failure(etc, logs, store, monkeypatch):
    etc("alice")
    monkeypatch.setattr(main, "sync_running_pod_password",
                        lambda u, h: {"synced": [], "failed": [], "error": "POD_LIST_FAILED"} if h == HASH
                        else {"synced": [], "failed": []})

    _run(store)

    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "POD_PASSWORD_SYNC_FAILED"
    assert _shadow("alice")["passwd"] == OTHER_HASH


def test_secret_failure_restores_ledger_and_skips_pods(etc, logs, store, monkeypatch):
    etc("alice")
    restored, pod_calls = [], []

    def secrets(u, h):
        if h == HASH:
            raise RuntimeError("API 서버 응답 없음")
        restored.append(h)
        return []
    monkeypatch.setattr(main, "update_account_secrets", secrets)
    monkeypatch.setattr(main, "sync_running_pod_password",
                        lambda u, h: pod_calls.append(h) or {"synced": [], "failed": []})

    _run(store)

    assert restored == [OTHER_HASH] and _shadow("alice")["passwd"] == OTHER_HASH
    assert pod_calls == [OTHER_HASH]                      # 새 해시는 Pod 에 한 번도 가지 않았다
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "SECRET_UPDATE_FAILED"
    assert json.loads(logs[-1]["error_detail"])["error"]["rolled_back"] is True


def test_restore_failure_retries_until_the_new_hash_is_everywhere(etc, logs, store, monkeypatch):
    """되돌리지 못했으면 일부만 바뀐 상태다. 같은 해시를 다시 써서 끝까지 맞춘다."""
    etc("alice")
    attempts = []

    def pods(u, h):
        attempts.append(h)
        return {"synced": [], "failed": ["p1"]} if len(attempts) <= 2 else {"synced": ["p1"], "failed": []}
    monkeypatch.setattr(main, "sync_running_pod_password", pods)

    _run(store)

    # 첫 시도 실패 → 되돌리기도 실패 → 재시도에서 새 해시로 성공
    assert attempts == [HASH, OTHER_HASH, HASH]
    assert _shadow("alice")["passwd"] == HASH
    assert [l["attempt"] for l in logs if l["phase"] == Phase.RETRY] == [2]
    assert logs[-1]["phase"] == Phase.SUCCESS


def test_partial_state_that_never_converges_is_handed_to_the_admin(etc, logs, store, monkeypatch):
    etc("alice")
    monkeypatch.setattr(main, "sync_running_pod_password", lambda u, h: {"synced": [], "failed": ["p1"]})

    _run(store)

    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "DEGRADED"
    assert "POD_PASSWORD_SYNC_FAILED" in logs[-1]["error_detail"]


def test_non_sha512_old_hash_only_restores_ledger(etc, logs, store, monkeypatch):
    """잠긴 계정("!")처럼 Secret 에 넣을 수 없는 옛 값은 원장만 되돌리고, 되돌리지 못한 실패로 다룬다."""
    with main.app.app_context():
        main.write_shadow_lines(main.read_shadow_lines() + [
            main.format_shadow_entry({"name": "alice", "passwd": "!", "lastchg": 1})])
    monkeypatch.setattr(main, "VERIFY_MODE", "baseline")
    monkeypatch.setattr(main, "sync_running_pod_password", lambda u, h: {"synced": [], "failed": ["p1"]})

    _run(store)

    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "POD_PASSWORD_SYNC_FAILED"
    assert json.loads(logs[-1]["error_detail"])["error"]["rolled_back"] is False
    assert _shadow("alice")["passwd"] == "!"


def test_resumed_attempt_cannot_restore_and_reports_it(etc, logs, store, monkeypatch):
    """중단됐다 이어받으면 원장에 이미 새 해시가 있어 옛 해시를 모른다. 되돌렸다고 알리면 안 된다."""
    with main.app.app_context():
        main.write_shadow_lines(main.read_shadow_lines() + [
            main.format_shadow_entry({"name": "alice", "passwd": HASH, "lastchg": 1})])
    monkeypatch.setattr(main, "VERIFY_MODE", "baseline")
    restores = []
    monkeypatch.setattr(main, "_restore_password", lambda u, h: restores.append(h) or True)
    monkeypatch.setattr(main, "sync_running_pod_password", lambda u, h: {"synced": [], "failed": ["p1"]})

    _run(store)

    assert restores == []
    assert json.loads(logs[-1]["error_detail"])["error"]["rolled_back"] is False


def test_baseline_fails_once_and_still_restores(etc, logs, store, monkeypatch):
    """운영 방식(baseline)은 재시도하지 않는다. 그래도 실패는 되돌린 뒤의 실패여야 한다."""
    etc("alice")
    monkeypatch.setattr(main, "VERIFY_MODE", "baseline")
    monkeypatch.setattr(main, "sync_running_pod_password",
                        lambda u, h: {"synced": [], "failed": ["p1"]} if h == HASH else {"synced": [], "failed": []})

    _run(store)

    assert _shadow("alice")["passwd"] == OTHER_HASH
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "POD_PASSWORD_SYNC_FAILED"
    assert not [l for l in logs if l["phase"] == Phase.RETRY]
