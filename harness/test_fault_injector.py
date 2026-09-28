"""fault_injector.Fault 의 계약. 장전 표는 infra-sql 의 DDL 정의로 sqlite 에 세운다."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fault_injector  # noqa: E402
import test_trial  # noqa: E402


@pytest.fixture
def db():
    db = sqlite3.connect(":memory:")
    db.execute(test_trial._sqlite_ddl("operation_log.sql", "fault_arming"))
    return db


def _fault(db, scenario="C12"):
    return fault_injector.Fault(test_trial.Sql(db), scenario=scenario, username="exp-fu-m1")


def _rows(db):
    return db.execute("SELECT username, step_name, scenario, fired_at FROM fault_arming").fetchall()


def test_arm_inserts_the_scenario_row(db):
    _fault(db).arm()
    assert _rows(db) == [("exp-fu-m1", "step_remove_krb5", "C12", None)]


def test_report_without_firing_reads_none_and_deletes_the_row(db):
    f = _fault(db, "C06")
    f.arm()
    out = f.report()
    assert out["scenario"] == "C06" and out["step_name"] == "step_create_krb5_principal"
    assert out["armed_at"] is not None and out["fired_at"] is None
    assert _rows(db) == []


def test_report_reads_the_fired_mark(db):
    f = _fault(db)
    f.arm()
    db.execute("UPDATE fault_arming SET fired_at = '2026-09-29 01:00:00.000'")
    assert f.report()["fired_at"] == "2026-09-29 01:00:00.000"
    assert _rows(db) == []


def test_disarm_can_be_called_twice(db):
    f = _fault(db)
    f.arm()
    f.disarm()
    f.disarm()
    assert _rows(db) == []


def test_other_users_rows_are_left_alone(db):
    other = fault_injector.Fault(test_trial.Sql(db), scenario="C12", username="exp-fu-m2")
    other.arm()
    f = _fault(db)
    f.arm()
    f.report()
    assert _rows(db) == [("exp-fu-m2", "step_remove_krb5", "C12", None)]


def test_an_unknown_scenario_is_rejected(db):
    with pytest.raises(ValueError, match="C99"):
        _fault(db, "C99")
