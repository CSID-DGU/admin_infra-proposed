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

    def __init__(self, *, declare_after=None, rounds=(), step=200.0, checks=len(evaluator.CREATION_CHECKS)):
        self.now = 1000.0
        self.step = step
        self.checks = checks
        self.declare_after = declare_after  # 이 횟수만큼 advance 한 뒤부터 선언이 보인다
        self.rounds = list(rounds)          # 평가 회차별 검사 결과
        self.advance_calls = 0
        self.eval_times = []                # 평가가 시작된 시각
        self.in_round = 0
        self.current = evaluator.PASS
        self.saved = []                     # save 가 받은 기록

    def save(self, record):
        self.saved.append(record)

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

    def collect(self, name, target):
        if self.in_round == 0:
            self.current = self.rounds.pop(0) if self.rounds else evaluator.PASS
            self.eval_times.append(self.now)
        self.in_round = (self.in_round + 1) % self.checks
        # 관계 판정이 회차 결과를 흐리지 않도록 식별자는 늘 서로 맞게 돌려준다.
        return self.current, {"check": name, "pod_uid": "pod-1", "backend_pod_uid": "pod-1",
                              "mount_source": "nas:/share" + target["expected"]["home_suffix"]}


def _run(conn, ports, **kw):
    kw.setdefault("trial_id", "trial-0001")
    kw.setdefault("method", "full")
    kw.setdefault("server_group", "A")
    kw.setdefault("operation", "CREATE")
    kw.setdefault("horizon_sec", 500)
    kw.setdefault("repetition", 1)
    kw.setdefault("revisions", {"config-server": "abc1234"})
    kw.setdefault("username", "exp-user")
    kw.setdefault("save", ports.save)
    kw.setdefault("environment", lambda: (trial_runner.CLEAN, {}))
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


def test_save_is_called_once_with_the_returned_record(conn):
    ports = Ports(declare_after=1)
    result = _run(conn, ports)
    assert len(ports.saved) == 1
    assert ports.saved[0] is result


def test_save_runs_after_the_manifest_row_is_closed(conn):
    ended = []

    def save(record):
        ended.append(_row(conn, record["trial_id"])["ended_at"])

    _run(conn, Ports(declare_after=1), save=save)
    assert len(ended) == 1 and ended[0]


def test_a_failing_save_is_not_swallowed(conn):
    def save(record):
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        _run(conn, Ports(declare_after=1), save=save)


def test_save_is_required(conn):
    ports = Ports(declare_after=1)
    with pytest.raises(TypeError, match="save"):
        trial_runner.run_trial(
            conn, trial_id="trial-0001", method="full", server_group="A", operation="CREATE",
            horizon_sec=500, repetition=1, revisions={}, username="exp-user",
            submit=ports.submit, advance=ports.advance, declaration=ports.declaration,
            collect=ports.collect, clock=ports.clock,
            environment=lambda: (trial_runner.CLEAN, {}))


def test_environment_is_judged_once_before_the_trial_is_opened(conn):
    seen = []

    def environment():
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM trial_manifest WHERE trial_id = %s", ("trial-0001",))
            seen.append(cur.fetchone()[0])
        return trial_runner.CLEAN, {}

    _run(conn, Ports(declare_after=1), environment=environment)
    assert seen == [0]


@pytest.mark.parametrize("verdict", [trial_runner.CLEAN, trial_runner.DIRTY, evaluator.UNKNOWN])
def test_every_environment_verdict_is_recorded_and_the_trial_still_runs(conn, verdict):
    ports = Ports(declare_after=1)
    result = _run(conn, ports, environment=lambda: (verdict, {"leftover_pods": []}))
    assert result["environment"] == {"verdict": verdict, "evidence": {"leftover_pods": []}}
    assert ports.saved == [result]


def test_a_broken_environment_judge_is_unknown_and_the_trial_still_runs(conn):
    def environment():
        raise ConnectionError("kube api unreachable")

    ports = Ports(declare_after=1)
    result = _run(conn, ports, environment=environment)
    assert result["environment"]["verdict"] == evaluator.UNKNOWN
    assert result["environment"]["evidence"]["collector_error"] == "ConnectionError"
    assert ports.saved == [result]


def test_an_unexpected_environment_value_is_unknown(conn):
    result = _run(conn, Ports(declare_after=1), environment=lambda: ("OK", {"x": 1}))
    assert result["environment"]["verdict"] == evaluator.UNKNOWN
    assert result["environment"]["evidence"] == {"unexpected_result": "'OK'", "detail": {"x": 1}}


def test_environment_is_required(conn):
    ports = Ports(declare_after=1)
    with pytest.raises(TypeError, match="environment"):
        trial_runner.run_trial(
            conn, trial_id="trial-0001", method="full", server_group="A", operation="CREATE",
            horizon_sec=500, repetition=1, revisions={}, username="exp-user",
            submit=ports.submit, advance=ports.advance, declaration=ports.declaration,
            collect=ports.collect, clock=ports.clock, save=ports.save)


def _pair(conn, create_ports, revoke_ports, **kw):
    kw.setdefault("revoke_submit", lambda request_id: request_id)
    kw.setdefault("save", create_ports.save)
    return trial_runner.run_pair(
        conn, create_trial_id="trial-c", revoke_trial_id="trial-r", method="full",
        server_group="A", horizon_sec=500, repetition=1, revisions={"config-server": "abc1234"},
        username="exp-user", create_submit=create_ports.submit,
        create_declaration=create_ports.declaration,
        revoke_declaration=revoke_ports.declaration, advance=create_ports.advance,
        collect=create_ports.collect, clock=create_ports.clock,
        environment=lambda: (trial_runner.CLEAN, {}), **kw)


def _manifest(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT trial_id, operation, request_id FROM trial_manifest ORDER BY rowid")
        return cur.fetchall()


def test_pair_opens_create_then_revoke_on_the_same_request(conn):
    ports = Ports(declare_after=1)
    _pair(conn, ports, ports)
    assert _manifest(conn) == [("trial-c", "CREATE", "req-1"), ("trial-r", "REVOKE", "req-1")]


@pytest.mark.parametrize("verdict", [evaluator.PASS, evaluator.FAIL])
def test_revoke_start_state_carries_the_creation_horizon_verdict(conn, verdict):
    # 생성 trial 의 두 평가(선언 시점, H 끝) 뒤에 회수 trial 의 평가가 이어진다.
    ports = Ports(declare_after=1, rounds=[evaluator.PASS, verdict])
    created, revoked = _pair(conn, ports, ports)
    assert created["independent_verdict"]["at_horizon"]["verdict"] == verdict
    assert revoked["start_state"] == {"creation_trial_id": "trial-c",
                                      "creation_verdict_at_horizon": verdict}
    assert created["start_state"] is None


def test_revoke_runs_even_without_a_creation_declaration(conn):
    create_ports = Ports(declare_after=None)
    revoke_ports = Ports(declare_after=1)
    created, revoked = _pair(conn, create_ports, revoke_ports)
    assert created["system_declaration"]["value"] is None
    assert revoked["operation"] == "REVOKE"
    assert [r[0] for r in _manifest(conn)] == ["trial-c", "trial-r"]


def test_revoke_submit_returning_another_request_is_rejected(conn):
    ports = Ports(declare_after=1)
    with pytest.raises(trial.TrialStateError, match="req-2"):
        _pair(conn, ports, ports, revoke_submit=lambda request_id: "req-2")


def test_a_failing_creation_save_stops_the_pair(conn):
    def save(record):
        raise OSError("disk full")

    ports = Ports(declare_after=1)
    with pytest.raises(OSError, match="disk full"):
        _pair(conn, ports, ports, save=save)
    assert [r[0] for r in _manifest(conn)] == ["trial-c"]


def test_start_state_on_a_create_trial_is_rejected_before_opening(conn):
    with pytest.raises(ValueError, match="start_state"):
        _run(conn, Ports(declare_after=1), start_state={"creation_verdict_at_horizon": "PASS"})
    assert _manifest(conn) == []


def test_an_unpaired_revoke_trial_keeps_start_state_none(conn):
    result = _run(conn, Ports(declare_after=1), operation="REVOKE")
    assert result["start_state"] is None


class FakeInjector:
    """주입 포트 흉내. 호출 순서를 trial 모듈 호출과 함께 한 목록에 적는다."""

    def __init__(self, log):
        self.log = log

    def arm(self):
        self.log.append("arm")

    def poll(self):
        self.log.append("poll")

    def report(self):
        self.log.append("report")
        return {"kind": "code_hook", "armed_at": "t0", "fired_at": "t1", "verified": True}

    def disarm(self):
        self.log.append("disarm")


def _logged(monkeypatch, log, ports):
    for name in ("bind_request", "close_trial"):
        original = getattr(trial, name)
        monkeypatch.setattr(trial, name, lambda *a, _n=name, _o=original, **kw: (log.append(_n), _o(*a, **kw))[1])
    submit = ports.submit
    ports.submit = lambda: (log.append("submit"), submit())[1]


def test_injector_is_armed_before_submit_polled_per_advance_and_reported_after_close(conn, monkeypatch):
    log = []
    ports = Ports(declare_after=1)
    _logged(monkeypatch, log, ports)
    result = _run(conn, ports, injector=FakeInjector(log))
    assert log == ["arm", "submit", "bind_request", "poll", "poll", "poll", "close_trial", "report", "disarm"]
    assert result["injection"]["fired_at"] == "t1"
    assert "fault" not in result


def test_without_an_injector_the_record_has_none(conn):
    assert _run(conn, Ports(declare_after=1))["injection"] is None


def test_injector_is_disarmed_when_submit_raises(conn):
    log = []
    ports = Ports(declare_after=1)

    def submit():
        raise RuntimeError("be down")
    ports.submit = submit
    with pytest.raises(RuntimeError, match="be down"):
        _run(conn, ports, injector=FakeInjector(log))
    assert log == ["arm", "disarm"]


def test_pair_passes_each_injector_only_to_its_trial(conn):
    create_log, revoke_log = [], []
    ports = Ports(declare_after=1)
    created, revoked = _pair(conn, ports, ports, create_injector=FakeInjector(create_log),
                             revoke_injector=FakeInjector(revoke_log))
    assert create_log == revoke_log == ["arm", "poll", "poll", "poll", "report", "disarm"]
    assert created["injection"] is not None and revoked["injection"] is not None


def test_pair_without_a_create_injector_records_none(conn):
    ports = Ports(declare_after=1)
    created, revoked = _pair(conn, ports, ports, revoke_injector=FakeInjector([]))
    assert created["injection"] is None and revoked["injection"] is not None


# 기록 스키마 v2 의 보조 칸

HERE = Path(__file__).resolve().parent


def test_v2_fields_are_empty_without_their_arguments(conn):
    result = _run(conn, Ports(declare_after=1))
    assert result["scenario"] is None
    assert result["snapshots"] is None
    assert result["protection"] is None
    assert result["independent_verdict"]["samples"] == []
    assert result["evaluator_version"] == evaluator.VERSION


def test_scenario_records_only_id_aliases_and_hash(conn):
    import scenario_spec
    spec = scenario_spec.load(HERE / "scenarios" / "A3-KRB5.yaml")
    result = _run(conn, Ports(declare_after=1), scenario=spec, scenario_id="A3-KRB5")
    assert result["scenario"] == {"id": "A3-KRB5", "aliases": spec.run_view()["aliases"],
                                  "spec_hash": spec.spec_hash}
    assert "analysis" not in json.dumps(result)


def test_a_scenario_id_that_disagrees_with_the_spec_is_rejected(conn):
    import scenario_spec
    spec = scenario_spec.load(HERE / "scenarios" / "A3-KRB5.yaml")
    with pytest.raises(ValueError, match="scenario_id"):
        _run(conn, Ports(declare_after=1), scenario=spec, scenario_id="N1")


def test_samples_accumulate_only_after_the_declaration(conn):
    ports = Ports(declare_after=2, step=100.0)
    result = _run(conn, ports, horizon_sec=500, sample_every=2)
    samples = result["independent_verdict"]["samples"]
    # 1100, 1200(선언), 1300, 1400(표본), 1500
    assert [s["at"] for s in samples] == [1400.0]
    assert all(s["at"] > result["timestamps"]["declared"] for s in samples)
    assert samples[0]["verdict"]["verdict"] == evaluator.PASS


def test_no_samples_without_a_declaration(conn):
    result = _run(conn, Ports(declare_after=None, step=100.0), sample_every=1)
    assert result["independent_verdict"]["samples"] == []


def test_snapshot_is_taken_before_open_and_after_close(conn):
    seen = []

    def snapshot():
        row = _row(conn, "trial-0001") if seen else None
        seen.append(row)
        return {"pods": [len(seen)]}

    result = _run(conn, Ports(declare_after=1), snapshot=snapshot)
    assert seen[0] is None                  # open_trial 전이라 manifest 행이 없다
    assert seen[1]["ended_at"]              # close_trial 뒤다
    assert result["snapshots"] == {"before": {"pods": [1]}, "after": {"pods": [2]}}


def test_a_failing_snapshot_is_written_into_its_field_and_the_trial_finishes(conn):
    def snapshot():
        raise ConnectionError("kube api down")

    ports = Ports(declare_after=1)
    result = _run(conn, ports, snapshot=snapshot)
    err = {"error": "ConnectionError", "message": "kube api down"}
    assert result["snapshots"] == {"before": err, "after": err}
    assert ports.saved == [result]
    assert result["independent_verdict"]["at_horizon"] is not None


def test_protection_is_judged_before_and_after(conn):
    bystander = {"username": "exp-bystander", "expected": {"home_suffix": "/exp-bystander"}}
    result = _run(conn, Ports(declare_after=1), bystanders=[bystander])
    for when in ("before", "after"):
        assert result["protection"][when]["protection_verdict"] == evaluator.PASS
        assert "exp-bystander" in result["protection"][when]["bystanders"]


def test_a_failing_protection_is_written_into_its_field_and_the_trial_finishes(conn, monkeypatch):
    def broken(collect, *, bystanders):
        raise RuntimeError("bystander lookup failed")

    monkeypatch.setattr(evaluator, "evaluate_protection", broken)
    ports = Ports(declare_after=1)
    result = _run(conn, ports, bystanders=[{"username": "b"}])
    err = {"error": "RuntimeError", "message": "bystander lookup failed"}
    assert result["protection"] == {"before": err, "after": err}
    assert ports.saved == [result]
