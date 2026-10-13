"""NodePort 정리가 생성 중인 Pod 의 배정 행을 지우지 않는지 본다.

배정 행은 Service 보다 먼저 들어간다(Pod 준비 대기 최대 500초). 예전 정리는 "Service 없는 행"을
전부 stale 로 봐, 겹쳐 도는 다른 작업의 행을 지워 그 작업이 Service 생성에서 실패했다.
"""
import types

import main
from lifecycle_steps import provision


class _Cursor:
    def __init__(self, old_pods):
        self.old_pods, self.executed, self.rowcount, self._rows = old_pods, [], 0, []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))
        if sql.lstrip().startswith("SELECT"):
            self._rows = [(p,) for p in self.old_pods]
        else:
            self.rowcount = 2

    def fetchall(self): return self._rows


class _Conn:
    def __init__(self, old_pods):
        self.cur, self.committed = _Cursor(old_pods), False

    def cursor(self): return self.cur
    def commit(self): self.committed = True
    def rollback(self): pass
    def close(self): pass


def _run(monkeypatch, old_pods, live_pods):
    """old_pods: 유예를 넘긴 행만 가진 Pod(DB 가 걸러 준 결과), live_pods: Service 가 있는 Pod"""
    conn = _Conn(old_pods)
    services = types.SimpleNamespace(items=[
        types.SimpleNamespace(metadata=types.SimpleNamespace(labels={"pod_name": p})) for p in live_pods
    ])
    monkeypatch.setattr(main, "_last_reconcile_ts", 0.0)
    monkeypatch.setattr(main, "load_k8s", lambda: None)
    monkeypatch.setattr(main, "get_db_connection", lambda: conn)
    monkeypatch.setattr(provision.client, "CoreV1Api", lambda: types.SimpleNamespace(
        list_namespaced_service=lambda namespace, label_selector: services))
    with main.app.app_context():
        deleted = provision.reconcile_nodeport_allocations(namespace="ailab-full")
    return deleted, conn


def test_candidates_are_limited_by_row_age(monkeypatch):
    """후보 조회가 DB 시계 기준 유예 시간을 조건으로 건다 — 방금 넣은 행은 후보에 오르지 않는다."""
    _, conn = _run(monkeypatch, old_pods=[], live_pods=[])
    sql, params = conn.cur.executed[0]
    assert "MAX(created_at) < NOW() - INTERVAL %s SECOND" in sql
    assert params == (main._NODEPORT_STALE_GRACE_SEC,)


def test_grace_outlasts_pod_ready_wait():
    """유예가 Pod 준비 대기보다 짧으면 생성 중인 행이 다시 지워진다."""
    assert main._NODEPORT_STALE_GRACE_SEC > main.app.config["POD_READY_MAX_WAIT_SEC"]


def test_nothing_deleted_when_no_pod_is_past_grace(monkeypatch):
    deleted, conn = _run(monkeypatch, old_pods=[], live_pods=[])
    assert deleted == 0
    assert not [e for e in conn.cur.executed if e[0].startswith("DELETE")]


def test_only_old_pods_without_service_are_deleted(monkeypatch):
    deleted, conn = _run(monkeypatch, old_pods=["gone", "alive"], live_pods=["alive"])
    deletes = [e for e in conn.cur.executed if e[0].startswith("DELETE")]
    assert deletes == [("DELETE FROM nodeport_allocations WHERE pod_name=%s", ("gone",))]
    assert deleted == 2 and conn.committed
