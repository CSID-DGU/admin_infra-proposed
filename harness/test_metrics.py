"""metrics.py 계약 검증. 손으로 만든 기록 묶음의 지표를 손계산 값과 비교한다."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics  # noqa: E402

USER = "tm01"
OK = {"kind": "code_hook", "verified": True}


def _v(value):
    return {"access_verdict": value, "verdict": value}


def _rec(tid, *, op="CREATE", sid="A3-KRB5", method="full", declared=True, decl=None, horizon="PASS",
         env="CLEAN", injection=OK, pods=1, snapshot=True, protection=None, samples=(), start=None,
         verified_at=None):
    return {
        "schema_version": 2, "trial_id": tid, "method": method, "operation": op, "scenario_id": sid,
        "scenario": {"id": sid, "aliases": [], "spec_hash": "h"},
        "timestamps": {"submitted": 0, "declared": 1 if declared else None, "verified": verified_at},
        "system_declaration": {"value": metrics.DECLARATIONS[op] if declared else None},
        "independent_verdict": {"at_declaration": _v(decl) if declared else None, "at_horizon": _v(horizon),
                                "samples": [{"at": 2, "verdict": _v(s)} for s in samples]},
        "environment": {"verdict": env, "evidence": {"username": USER}},
        "start_state": start, "injection": injection,
        "snapshots": {"before": None, "after": {"by_user": {USER: {"pod": ["p"] * pods}}, "errors": {}}}
        if snapshot else None,
        "protection": None if protection is None else {"before": None, "after": {"protection_verdict": protection}},
    }


CREATE = [
    _rec("c1", decl="PASS", protection="PASS", verified_at=10),
    _rec("c2", decl="FAIL", verified_at=20),
    _rec("c3", decl="UNKNOWN", env="DIRTY", verified_at=30),
    _rec("c4", declared=False, horizon="FAIL", protection="UNKNOWN"),
    _rec("c5", decl="PASS", pods=2, verified_at=40),
    _rec("c6", decl="PASS", protection="FAIL", verified_at=50),
    _rec("c7", decl="FAIL", injection={"kind": "code_hook", "verified": False}),
    _rec("c8", decl="FAIL", injection={"kind": "code_hook"}),
    _rec("c9", decl="PASS", horizon="UNKNOWN", env="UNKNOWN"),
    _rec("c10", decl="PASS", snapshot=False, verified_at=60),
]
REVOKE = [
    _rec("r1", op="REVOKE", sid="R", decl="PASS", samples=["PASS"], start={"creation_verdict_at_horizon": "PASS"}),
    _rec("r2", op="REVOKE", sid="R", decl="PASS", samples=["FAIL", "PASS"],
         start={"creation_verdict_at_horizon": "PASS"}),
    _rec("r3", op="REVOKE", sid="R", decl="PASS", horizon="UNKNOWN", start={"creation_verdict_at_horizon": "FAIL"}),
    _rec("r4", op="REVOKE", sid="R", decl="FAIL", horizon="FAIL", start=None),
    _rec("r5", op="REVOKE", sid="R", declared=False, horizon="FAIL", start={"creation_verdict_at_horizon": "PASS"}),
]
V1 = [
    {"schema_version": 1, "trial_id": "v1a", "method": "baseline", "operation": "CREATE", "scenario_id": "V",
     "fault": None, "timestamps": {"submitted": 0, "verified": None},
     "system_declaration": {"value": "FULFILLED"},
     "independent_verdict": {"at_declaration": {"verdict": "FAIL"}, "at_horizon": {"verdict": "PASS"}},
     "environment": {"verdict": "CLEAN"}},
    {"schema_version": 1, "trial_id": "v1b", "method": "baseline", "operation": "CREATE", "scenario_id": "V",
     "fault": {"scenario": "C06", "fired_at": "t"}, "timestamps": {"submitted": 0},
     "system_declaration": {"value": None},
     "independent_verdict": {"at_declaration": None, "at_horizon": {"verdict": "FAIL"}}},
]
SPECS = {"A3-KRB5": {"spec_hash": "h", "analysis": {"recoverable": True}},
         "R": {"spec_hash": "r", "analysis": {"recoverable": False}}}


def _k(m):
    return (m["k"], m["n"], m["unknown"])


@pytest.fixture(scope="module")
def result():
    return metrics.analyze(CREATE + REVOKE + V1, SPECS, missing=[("A3-KRB5", "full")])


def test_counts_before_metrics(result):
    g = result["A3-KRB5|full"]
    assert (g["total"], g["missing"], g["invalid_injection"]) == (11, 1, 2)
    assert g["environment"] == {"CLEAN": 8, "DIRTY": 1, "UNKNOWN": 1}


def test_creation_metrics_match_hand_count(result):
    m = result["A3-KRB5|full"]["metrics"]
    assert _k(m["incorrect_completion"]) == (1, 7, 1)
    assert _k(m["unknown_at_declaration"]) == (1, 7, 0)
    assert _k(m["verified_completion"]) == (6, 8, 1)
    assert _k(m["safe_automatic_recovery"]) == (2, 8, 2)
    assert _k(m["duplicate_resource"]) == (1, 8, 1)
    assert _k(m["protection_violation"]) == (1, 3, 1)
    assert m["residual_access"]["rate"] is None
    assert m["time_to_verified_completion"] == {"n": 6, "median": 35.0, "q1": 22.5, "q3": 47.5}


def test_strata_by_environment(result):
    by_env = result["A3-KRB5|full"]["by_environment"]
    assert _k(by_env["DIRTY"]["unknown_at_declaration"]) == (1, 1, 0)
    assert _k(by_env["CLEAN"]["incorrect_completion"]) == (1, 5, 0)


def test_revocation_residual_access_and_start_state(result):
    g = result["R|full"]
    assert g["start_state"] == {"PASS": 3, "FAIL": 1, "UNPAIRED": 1}
    assert _k(g["metrics"]["residual_access"]) == (2, 4, 1)
    assert g["metrics"]["safe_automatic_recovery"] is None
    assert _k(g["by_start_state"]["PASS"]["residual_access"]) == (1, 2, 0)


def test_v1_records_are_read(result):
    g = result["V|baseline"]
    assert g["invalid_injection"] == 1
    assert _k(g["metrics"]["incorrect_completion"]) == (1, 1, 0)


def test_wilson_known_values():
    lo, hi = metrics.wilson(0, 20)
    assert lo == 0.0 and hi == pytest.approx(0.1611, abs=1e-4)
    assert metrics.wilson(10, 20) == pytest.approx((0.2993, 0.7007), abs=1e-4)
    assert metrics.wilson(0, 0) is None


def test_cli_prints_table_and_writes_json(tmp_path, capsys):
    for rec in CREATE[:2]:
        (tmp_path / f"{rec['trial_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    out = tmp_path / "out" / "m.json"
    out.parent.mkdir()
    specs = Path(__file__).resolve().parent / "scenarios"
    assert metrics.main([str(tmp_path), "--specs", str(specs), "--json", str(out)]) == 0
    assert "A3-KRB5|full incorrect_completion 1/2" in capsys.readouterr().out
    assert json.loads(out.read_text())["A3-KRB5|full"]["spec_hash"] is not None
