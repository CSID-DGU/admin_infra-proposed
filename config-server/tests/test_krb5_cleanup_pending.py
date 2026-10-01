"""정리 예약 재시도는 그 노드에서 다시 쓰이는 keytab을 지우지 않는다.

정리가 시간 초과로 실패해 예약을 다시 적는 사이 같은 노드에 배포가 성공하면, 살아 있는 keytab에
삭제 예약이 남는다. 다음 주기가 그대로 지우면 갱신 타이머까지 사라져 티켓 만료 뒤 홈 접근이 끊긴다.
"""
import pytest

import reconcile_krb5 as rec


class _Cursor:
    def __init__(self, pending):
        self.pending = pending
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if sql.startswith("DELETE"):
            self.rowcount = 1 if tuple(params) in self.pending else 0
            self.pending.discard(tuple(params))

    def fetchall(self):
        return sorted(self.pending)


class _Conn:
    def __init__(self, pending):
        self.pending = pending

    def cursor(self):
        return _Cursor(self.pending)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def farm(monkeypatch):
    """예약 표·노드 정리·예약 재기록을 가로챈다. 반환: (예약 집합, 정리 호출 목록, 노드별 사용 중 이름)"""
    pending, removed, in_use = {("alice", "farm2")}, [], {}
    monkeypatch.setattr(rec, "get_db_connection", lambda: _Conn(pending))
    monkeypatch.setattr(rec, "_remove_krb5_from_farm", lambda u, n: removed.append((u, n)))
    monkeypatch.setattr(rec, "_record_krb5_cleanup_pending", lambda u, n: pending.add((u, n)))
    monkeypatch.setattr(rec, "_get_expected_krb5_usernames_for_node", lambda n: in_use.get(n, set()))
    return pending, removed, in_use


def test_removes_keytab_nobody_uses(farm):
    pending, removed, _ = farm
    rec.reconcile_krb5_cleanup_pending()
    assert removed == [("alice", "farm2")]
    assert pending == set()


def test_keeps_keytab_in_use_on_that_node_and_drops_the_pending_row(farm):
    pending, removed, in_use = farm
    in_use["farm2"] = {"alice"}
    rec.reconcile_krb5_cleanup_pending()
    assert removed == []
    assert pending == set()


def test_use_on_another_node_does_not_block_cleanup(farm):
    pending, removed, in_use = farm
    in_use["farm3"] = {"alice"}
    rec.reconcile_krb5_cleanup_pending()
    assert removed == [("alice", "farm2")]


def test_keeps_keytab_and_pending_row_when_use_lookup_fails(farm, monkeypatch):
    pending, removed, _ = farm

    def boom(node_name):
        raise RuntimeError("db down")
    monkeypatch.setattr(rec, "_get_expected_krb5_usernames_for_node", boom)
    rec.reconcile_krb5_cleanup_pending()
    assert removed == []
    assert pending == {("alice", "farm2")}


def test_failed_cleanup_is_recorded_again(farm, monkeypatch):
    pending, _, _ = farm

    def boom(username, node_name):
        raise RuntimeError("ssh timeout")
    monkeypatch.setattr(rec, "_remove_krb5_from_farm", boom)
    rec.reconcile_krb5_cleanup_pending()
    assert pending == {("alice", "farm2")}
