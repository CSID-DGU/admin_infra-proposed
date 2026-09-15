"""이슈 #55 — RUN_MODE 검증, /health 계측 노출, 작업 이력 유실 카운터."""
import pytest

import main
from adapters import operation_log
from adapters.operation_log import Action, Phase


class FakeRedis:
    def __init__(self, broken=False):
        self.n, self.broken = 0, broken

    def incr(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        self.n += 1
        return self.n

    def get(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        return str(self.n)


# ---------- RUN_MODE 검증 ----------

def test_run_mode_defaults_to_noprobe():
    assert main._resolve_run_mode({}) == "noprobe"


def test_run_mode_alias_and_precedence():
    assert main._resolve_run_mode({"VERIFY_MODE": "full"}) == "full"
    # 새 이름이 옛 이름보다 우선한다
    assert main._resolve_run_mode({"RUN_MODE": "noprobe", "VERIFY_MODE": "full"}) == "noprobe"


def test_run_mode_rejects_unknown_value_loudly():
    """대문자 FULL·오타가 조용히 noprobe 로 돌면 ablation 이 0으로 나온다 — 기동 시점에 죽어야 한다."""
    with pytest.raises(SystemExit, match="noprobe"):
        main._resolve_run_mode({"VERIFY_MODE": "FULL"})
    with pytest.raises(SystemExit, match="got 'BASELINE'"):
        main._resolve_run_mode({"RUN_MODE": "BASELINE"})


def test_run_mode_accepts_three_modes_only():
    for mode in ("baseline", "noprobe", "full"):
        assert main._resolve_run_mode({"RUN_MODE": mode}) == mode


# ---------- 유실 카운터 ----------

def _fail_log_db(monkeypatch):
    def boom():
        raise ConnectionError("log db down")
    monkeypatch.setattr(operation_log, "get_log_db_connection", boom)


def test_write_failure_increments_counter_and_flow_continues(monkeypatch):
    _fail_log_db(monkeypatch)
    fake = FakeRedis()
    monkeypatch.setattr(operation_log, "_redis", fake)
    with main.app.app_context():
        row_id = operation_log.log_operation(
            request_id=1, username="exp-np-t", action=Action.PROVISION, phase=Phase.START)
    assert row_id is None      # 삼키는 동작은 그대로다
    assert fake.n == 1         # 잃은 건수는 남는다


def test_raise_errors_path_still_raises_and_counts(monkeypatch):
    _fail_log_db(monkeypatch)
    fake = FakeRedis()
    monkeypatch.setattr(operation_log, "_redis", fake)
    with main.app.app_context(), pytest.raises(ConnectionError):
        operation_log.log_operation(
            request_id=1, username="exp-np-t", action=Action.PROVISION, phase=Phase.START,
            raise_errors=True)
    assert fake.n == 1


def test_counter_failure_never_breaks_the_flow(monkeypatch):
    """로그 DB 와 Redis 가 동시에 죽어도 생성 흐름은 계속된다. 최후 백업은 app.logger 행이다."""
    _fail_log_db(monkeypatch)
    monkeypatch.setattr(operation_log, "_redis", FakeRedis(broken=True))
    with main.app.app_context():
        assert operation_log.log_operation(
            request_id=1, username="exp-np-t", action=Action.PROVISION, phase=Phase.START) is None


# ---------- /health 노출 ----------

def test_health_reports_mode_and_loss_counter(monkeypatch):
    fake = FakeRedis(); fake.n = 3
    monkeypatch.setattr(operation_log, "_redis", fake)
    body = main.app.test_client().get("/health").get_json()
    assert body["status"] == "OK"
    assert body["run_mode"] == main.VERIFY_MODE
    assert body["oplog_write_failures"] == 3


def test_health_reports_null_when_counter_unreadable(monkeypatch):
    """조회 불능을 0 으로 접으면 '유실 없음' 으로 오독된다 — null 로 구분한다."""
    monkeypatch.setattr(operation_log, "_redis", FakeRedis(broken=True))
    body = main.app.test_client().get("/health").get_json()
    assert body["oplog_write_failures"] is None
