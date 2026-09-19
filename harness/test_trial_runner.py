"""trial_runner.py 계약 검증.

대상 시스템에 붙이지 않고 가짜 포트로만 돌린다. 표는 손으로 베껴 쓰지 않고 test_trial.py 가
이미 가진 DDL 변환기를 그대로 불러서 만든다. 베껴 쓰면 스키마가 두 곳으로 갈라져서 컬럼이
어긋나도 시험은 계속 통과한다.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluator  # noqa: E402
import test_trial  # noqa: E402
import trial  # noqa: E402
import trial_runner  # noqa: E402


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.execute(test_trial._sqlite_ddl("trial_manifest.sql", "trial_manifest"))
    return test_trial.Sql(db)


class Ports:
    """가짜 포트 묶음. 시계는 advance 가 밀어 준다."""

    def __init__(self, *, declare_after=None, rounds=(), step=200.0, checks=5):
        self.now = 1000.0
        self.step = step
        self.checks = checks
        self.declare_after = declare_after  # 이 횟수만큼 advance 한 뒤부터 선언이 보인다
        self.rounds = list(rounds)          # 평가 회차별 검사 결과
        self.advance_calls = 0
        self.eval_times = []                # 평가가 시작된 시각
        self.in_round = 0
        self.current = evaluator.PASS

    def clock(self):
        return self.now

    def submit(self):
        return "req-1"

    def advance(self):
        self.advance_calls += 1
        self.now += self.step

    def declaration(self):
        if self.declare_after is None or self.advance_calls < self.declare_after:
            return None
        return "FULFILLED"

    def collect(self, name, username):
        if self.in_round == 0:
            self.current = self.rounds.pop(0) if self.rounds else evaluator.PASS
            self.eval_times.append(self.now)
        self.in_round = (self.in_round + 1) % self.checks
        return self.current, {"check": name}


def _run(conn, ports, **kw):
    kw.setdefault("trial_id", "trial-0001")
    kw.setdefault("method", "full")
    kw.setdefault("server_group", "A")
    kw.setdefault("operation", "CREATE")
    kw.setdefault("horizon_sec", 500)
    kw.setdefault("repetition", 1)
    kw.setdefault("revisions", {"config-server": "abc1234"})
    kw.setdefault("username", "exp-user")
    return trial_runner.run_trial(
        conn, submit=ports.submit, advance=ports.advance, declaration=ports.declaration,
        collect=ports.collect, clock=ports.clock, **kw)


def _row(conn, trial_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM trial_manifest WHERE trial_id = %s", (trial_id,))
        return dict(zip([d[0] for d in cur.description], cur.fetchone()))


def test_manifest_is_opened_bound_and_closed_once_each(conn, monkeypatch):
    calls = {}
    for name in ("open_trial", "bind_request", "close_trial"):
        original = getattr(trial, name)
        calls[name] = 0

        def counted(*a, _name=name, _original=original, **kw):
            calls[_name] += 1
            return _original(*a, **kw)
        monkeypatch.setattr(trial, name, counted)

    _run(conn, Ports(declare_after=1))
    assert calls == {"open_trial": 1, "bind_request": 1, "close_trial": 1}
    row = _row(conn, "trial-0001")
    assert row["request_id"] == "req-1"
    assert row["ended_at"]


def test_declaration_triggers_two_evaluations(conn):
    ports = Ports(declare_after=1)
    result = _run(conn, ports)
    assert len(ports.eval_times) == 2
    assert result["independent_verdict"]["at_declaration"] is not None
    assert result["independent_verdict"]["at_horizon"] is not None
    assert result["system_declaration"] == {"value": "FULFILLED", "at": result["timestamps"]["declared"]}


def test_without_a_declaration_only_the_horizon_evaluation_runs(conn):
    ports = Ports(declare_after=None)
    result = _run(conn, ports)
    assert len(ports.eval_times) == 1
    assert result["timestamps"]["declared"] is None
    assert result["independent_verdict"]["at_declaration"] is None
    assert result["independent_verdict"]["at_horizon"]["verdict"] == evaluator.PASS
    assert result["timestamps"]["verified"] is None  # 선언이 없으면 검증 시각도 없다


def test_verified_is_the_declaration_time_when_that_verdict_passes(conn):
    ports = Ports(declare_after=1, rounds=[evaluator.PASS, evaluator.PASS])
    result = _run(conn, ports)
    ts = result["timestamps"]
    assert ts["verified"] == ts["declared"]
    assert ts["verified"] < ts["horizon_end"]


def test_a_later_recovery_does_not_erase_the_wrong_declaration(conn):
    ports = Ports(declare_after=1, rounds=[evaluator.FAIL, evaluator.PASS])
    result = _run(conn, ports)
    ts = result["timestamps"]
    assert ts["verified"] == ts["horizon_end"]
    assert result["independent_verdict"]["at_declaration"]["verdict"] == evaluator.FAIL
    assert result["independent_verdict"]["at_horizon"]["verdict"] == evaluator.PASS


def test_verified_is_none_when_no_evaluation_passes(conn):
    ports = Ports(declare_after=1, rounds=[evaluator.FAIL, evaluator.UNKNOWN])
    result = _run(conn, ports)
    assert result["timestamps"]["verified"] is None


def test_advance_stops_at_the_horizon(conn):
    ports = Ports(declare_after=None, step=200.0)
    result = _run(conn, ports, horizon_sec=500)
    assert ports.advance_calls == 3  # 1000 -> 1200 -> 1400 -> 1600 에서 멈춘다
    assert result["timestamps"]["horizon_end"] - result["timestamps"]["submitted"] >= 500


def test_result_is_json_serialisable(conn):
    result = _run(conn, Ports(declare_after=1))
    assert json.loads(json.dumps(result))["trial_id"] == "trial-0001"


def test_an_unknown_operation_raises(conn):
    with pytest.raises(ValueError, match="operation"):
        _run(conn, Ports(), operation="MIGRATE")
