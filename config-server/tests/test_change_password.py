"""계정 로그인 비밀번호 교체 — 원장(shadow)·계정 Secret·떠 있는 Pod 를 같은 해시로 맞춘다."""
import types

import pytest

import main
import utils

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


def test_updates_ledger_secrets_and_pods(etc, api, pod_password_sync):
    etc("alice", "bob")
    r = api.put("/users/alice/password", json={"passwd_hash": HASH})
    assert r.status_code == 200 and r.get_json()["status"] == "updated"
    assert _shadow("alice")["passwd"] == HASH
    assert _shadow("bob")["passwd"] == OTHER_HASH
    assert pod_password_sync == [("secrets", "alice", HASH), ("pods", "alice", HASH)]


def test_unknown_user_touches_nothing(etc, api, pod_password_sync):
    etc("bob")
    r = api.put("/users/alice/password", json={"passwd_hash": HASH})
    assert r.status_code == 404 and r.get_json()["error"] == "USER_NOT_FOUND"
    assert pod_password_sync == []


@pytest.mark.parametrize("body", [{}, {"passwd_hash": "plain-text"}, {"passwd_hash": HASH + "\nroot:x"}])
def test_rejects_non_sha512_crypt(etc, api, body, pod_password_sync):
    etc("alice")
    assert api.put("/users/alice/password", json=body).status_code == 400
    assert _shadow("alice")["passwd"] == OTHER_HASH
    assert pod_password_sync == []


def test_rejects_invalid_username(etc, api):
    assert api.put("/users/Bad;Name/password", json={"passwd_hash": HASH}).status_code == 400


def test_secret_failure_is_500_and_skips_pods(etc, api, monkeypatch, pod_password_sync):
    etc("alice")

    def boom(u, h):
        raise RuntimeError("API 서버 응답 없음")
    monkeypatch.setattr(main, "update_account_secrets", boom)
    r = api.put("/users/alice/password", json={"passwd_hash": HASH})
    assert r.status_code == 500 and r.get_json()["error"] == "SECRET_UPDATE_FAILED"
    assert pod_password_sync == []


@pytest.mark.parametrize("pods", [
    {"synced": [], "failed": ["p1"]},
    {"synced": [], "failed": [], "error": "POD_LIST_FAILED"},
])
def test_pod_failure_is_500_with_detail(etc, api, monkeypatch, pods):
    etc("alice")
    monkeypatch.setattr(main, "sync_running_pod_password", lambda u, h: pods)
    r = api.put("/users/alice/password", json={"passwd_hash": HASH})
    body = r.get_json()
    assert r.status_code == 500 and body["error"] == "POD_PASSWORD_SYNC_FAILED"
    assert body["pods"] == pods


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

def test_pod_failure_restores_previous_hash_everywhere(etc, api, monkeypatch, pod_password_sync):
    etc("alice")
    calls = []
    monkeypatch.setattr(main, "update_account_secrets", lambda u, h: calls.append(("secrets", h)) or [])

    def pods(u, h):
        calls.append(("pods", h))
        return {"synced": [], "failed": ["p1"]} if h == HASH else {"synced": ["p1"], "failed": []}
    monkeypatch.setattr(main, "sync_running_pod_password", pods)

    r = api.put("/users/alice/password", json={"passwd_hash": HASH})

    assert r.status_code == 500 and r.get_json()["rolled_back"] is True
    assert calls == [("secrets", HASH), ("pods", HASH), ("secrets", OTHER_HASH), ("pods", OTHER_HASH)]
    assert _shadow("alice")["passwd"] == OTHER_HASH


def test_secret_failure_restores_ledger(etc, api, monkeypatch):
    etc("alice")
    restored = []

    def secrets(u, h):
        if h == HASH:
            raise RuntimeError("API 서버 응답 없음")
        restored.append(h)
        return []
    monkeypatch.setattr(main, "update_account_secrets", secrets)

    r = api.put("/users/alice/password", json={"passwd_hash": HASH})

    assert r.status_code == 500 and r.get_json()["rolled_back"] is True
    assert restored == [OTHER_HASH] and _shadow("alice")["passwd"] == OTHER_HASH


def test_non_sha512_old_hash_only_restores_ledger(etc, api, monkeypatch, pod_password_sync):
    """잠긴 계정("!")처럼 Secret 에 넣을 수 없는 옛 값은 원장만 되돌리고 되돌리기 실패로 알린다."""
    with main.app.app_context():
        main.write_shadow_lines(main.read_shadow_lines() + [
            main.format_shadow_entry({"name": "alice", "passwd": "!", "lastchg": 1})])
    monkeypatch.setattr(main, "sync_running_pod_password", lambda u, h: {"synced": [], "failed": ["p1"]})

    r = api.put("/users/alice/password", json={"passwd_hash": HASH})

    assert r.status_code == 500 and r.get_json()["rolled_back"] is False
    assert _shadow("alice")["passwd"] == "!"
