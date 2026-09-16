import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import main  # noqa: E402


@pytest.fixture
def logs(monkeypatch):
    """log_operation 호출을 DB 대신 목록에 모은다."""
    records = []
    monkeypatch.setattr(main, "log_operation", lambda **kw: records.append(kw))
    return records


@pytest.fixture
def pod_status(monkeypatch):
    """Redis 진행 상황 기록 대역. set_pod_creation_status 호출 인자를 모은다."""
    calls = []
    monkeypatch.setattr(main, "set_pod_creation_status", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(main, "get_pod_creation_status", lambda *a, **k: None)
    return calls


@pytest.fixture
def api():
    return main.app.test_client()


@pytest.fixture(autouse=True)
def lease_env(monkeypatch):
    """재시도 지연 제거 + lease 저장소 메모리 대역. 실제 SQL은 test_job_control_sql.py에서 검증한다.
    반환 dict: job_id -> {"owner","alive","done","ctx"}. alive=False면 만료된 lease로 취급한다."""
    from adapters import job_control
    monkeypatch.setattr(main, "RETRY_DELAY_SEC", 0)
    # macOS에서 *.svc.cluster.local이 mDNS로 풀려 실제 Redis 접속이 수십 초 멈춘다 — 부가 저장은 무시
    monkeypatch.setattr(main, "save_job_result", lambda a, r, d: None)
    monkeypatch.setattr(main, "load_job_result", lambda a, r: None)
    # 끝 행 조회는 기본적으로 "없음" — 재선택 판정을 보는 시험만 대역을 바꿔 끼운다.
    monkeypatch.setattr(main, "job_end_exists", lambda a, r, j: False)
    rows = {}

    def claim(job_id, request_id, action, owner=None, ttl_sec=None):
        owner = owner or job_control.OWNER
        row = rows.get(job_id)
        if row is not None and row["owner"] != owner and row.get("alive", True):
            return None
        if row is None:
            row = rows[job_id] = {"owner": owner, "alive": True, "done": [], "ctx": {}}
        else:
            row.update(owner=owner, alive=True)
        return list(row["done"]), dict(row["ctx"])

    def record_step(job_id, done_steps, saved_ctx, owner=None):
        row = rows.get(job_id)
        if row is None or row["owner"] != (owner or job_control.OWNER):
            raise job_control.LeaseLost(str(job_id))
        row.update(done=list(done_steps), ctx=dict(saved_ctx))

    monkeypatch.setattr(job_control, "claim", claim)
    monkeypatch.setattr(job_control, "record_step", record_step)
    monkeypatch.setattr(job_control, "release", lambda job_id, owner=None: rows.pop(job_id, None))
    monkeypatch.setattr(job_control, "renew", lambda ids, owner=None, ttl_sec=None: len(ids))
    return rows
