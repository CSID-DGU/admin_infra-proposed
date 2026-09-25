"""원장(passwd/group/shadow) 잠금 — 겹쳐 잡기, 못 잡으면 실패, Pod 사이 직렬화(MySQL 이름 잠금)."""
import threading

import pytest

import main
import utils


@pytest.fixture
def etc(tmp_path, monkeypatch):
    """계정 원장을 임시 파일로 돌린다."""
    base = str(tmp_path / "etc")
    for k, val in list(main.app.config.items()):
        if isinstance(val, str) and val.startswith(main.BASE_ETC_DIR):
            monkeypatch.setitem(main.app.config, k, base + val[len(main.BASE_ETC_DIR):])
    with main.app.app_context():
        main.ensure_etc_layout()
        yield


class FakeConn:
    def __init__(self, got=1):
        self.got, self.sql, self.closed = got, [], False

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, args):
                conn.sql.append((sql, args))

            def fetchone(self):
                return (conn.got,)
        return Cur()

    def close(self):
        self.closed = True


@pytest.fixture
def mysql_backend(monkeypatch):
    """DB 백엔드로 돌리고 만든 가짜 연결 목록을 돌려준다."""
    conns = []
    monkeypatch.setenv("LEDGER_LOCK_BACKEND", "mysql")

    def connect():
        conns.append(FakeConn(got=conns_got[0]))
        return conns[-1]
    conns_got = [1]
    monkeypatch.setattr(utils, "_ledger_lock_connection", connect)
    return conns, conns_got


def test_nested_acquire_takes_the_db_lock_once(mysql_backend):
    conns, _ = mysql_backend
    with utils.ledger_lock():
        with utils.ledger_lock():
            pass
        assert not conns[0].closed
    assert len(conns) == 1
    assert [sql.split("(")[0] for sql, _ in conns[0].sql] == ["SELECT GET_LOCK", "SELECT RELEASE_LOCK"]
    assert conns[0].sql[0][1] == (utils.LEDGER_LOCK_NAME, utils.LEDGER_LOCK_TIMEOUT_SEC)
    assert conns[0].closed


def test_timeout_raises_and_does_not_enter(mysql_backend):
    conns, got = mysql_backend
    got[0] = 0
    entered = []
    with pytest.raises(utils.LedgerLockTimeout):
        with utils.ledger_lock():
            entered.append(True)
    assert entered == [] and conns[0].closed
    # 실패 뒤에도 겹쳐 잡기 카운터가 남지 않아 다음 잠금이 다시 DB 잠금을 잡는다.
    got[0] = 1
    with utils.ledger_lock():
        pass
    assert len(conns) == 2


def test_lock_released_when_body_raises(mysql_backend):
    conns, _ = mysql_backend
    with pytest.raises(ValueError):
        with utils.ledger_lock():
            raise ValueError("boom")
    assert conns[0].sql[-1][0].startswith("SELECT RELEASE_LOCK") and conns[0].closed


def test_concurrent_read_modify_write_keeps_every_line(etc):
    """잠금 없이 읽고 쓰기를 나누면 줄이 사라진다 — 모든 스레드의 줄이 남아야 한다."""
    n = 20
    barrier = threading.Barrier(n)

    def add(i):
        with main.app.app_context():
            barrier.wait()
            with utils.ledger_lock():
                lines = main.read_shadow_lines()
                main.write_shadow_lines(lines + [main.format_shadow_entry({"name": f"u{i}", "passwd": "x", "lastchg": 1})])

    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with main.app.app_context():
        names = {r["name"] for l in main.read_shadow_lines() if (r := main.parse_shadow_line(l))}
    assert {f"u{i}" for i in range(n)} <= names


def test_group_membership_rereads_under_lock(etc):
    """AD 호출 동안 다른 쪽이 group 파일을 고쳐도, 멤버 반영이 그 줄을 덮어쓰지 않는다."""
    with main.app.app_context():
        main.write_group_lines(main.read_group_lines() + ["team:x:30001:", "other:x:30002:"])
        main._set_group_membership(["team"], "alice", member=True)
        # 다른 Pod가 그 사이 새 그룹을 더했다고 치고, 제거가 그 줄을 지우지 않는지 본다.
        main.write_group_lines(main.read_group_lines() + ["late:x:30003:bob"])
        main._set_group_membership(["team"], "alice", member=False)
        lines = main.read_group_lines()
    assert "team:x:30001:" in lines and "late:x:30003:bob" in lines and "other:x:30002:" in lines
