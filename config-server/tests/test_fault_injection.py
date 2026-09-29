"""fault_injection 의 장전 조회와 발동 표시를 sqlite 로 검증한다.
표는 infra-sql/operation_log.sql 의 fault_arming DDL 을 sqlite 문법으로만 바꿔서 세운다."""
import pathlib
import re
import sqlite3

import pytest

from adapters import fault_injection

DDL_FILE = pathlib.Path(__file__).resolve().parents[2] / "infra-sql" / "operation_log.sql"


def _sqlite(sql):
    return sql.replace("%s", "?").replace("CURRENT_TIMESTAMP(3)", "CURRENT_TIMESTAMP")


def fault_arming_ddl():
    """MySQL 전용 부분(엔진 절, BIGINT AUTO_INCREMENT, DATETIME(3), 주석)만 sqlite 에 맞게 바꾼다."""
    text = DDL_FILE.read_text()
    ddl = re.search(r"CREATE TABLE IF NOT EXISTS fault_arming \(.*?\)\s*ENGINE=[^;]*;", text, re.S).group(0)
    ddl = re.sub(r"--[^\n]*", "", ddl)
    ddl = re.sub(r"\)\s*ENGINE=[^;]*;", ")", ddl)
    ddl = ddl.replace("BIGINT AUTO_INCREMENT PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    return _sqlite(ddl)


class Sql:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        outer = self

        class Cur:
            def __enter__(self):
                self.c = outer.db.cursor()
                return self

            def __exit__(self, *a):
                pass

            def execute(self, sql, params=()):
                self.c.execute(_sqlite(sql), params)

            def fetchone(self):
                return self.c.fetchone()

            @property
            def rowcount(self):
                return self.c.rowcount
        return Cur()

    def commit(self):
        self.db.commit()

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(fault_arming_ddl())
    monkeypatch.setattr(fault_injection, "get_log_db_connection", lambda **kw: Sql(conn))
    monkeypatch.setenv("FAULT_INJECTION", "1")
    return conn


@pytest.fixture
def connect_calls(db, monkeypatch):
    calls = []
    real = fault_injection.get_log_db_connection
    monkeypatch.setattr(fault_injection, "get_log_db_connection", lambda **kw: calls.append(kw) or real(**kw))
    return calls


@pytest.fixture(autouse=True)
def fresh_counts(monkeypatch):
    monkeypatch.setattr(fault_injection, "_seen", {})


# 옛 시나리오 ID -> (boundary, action). 확인하는 동작은 v1 시험과 같다.
C06, C08, C12 = ("X4", "response_loss"), ("X6", "sigkill_before_journal"), ("X2", "fail_persistent")


def arm(db, point, username="u1", step="step_x", occurrence=1):
    cur = db.execute("INSERT INTO fault_arming (username, boundary, step_name, action, occurrence)"
                     " VALUES (?,?,?,?,?)", (username, point[0], step, point[1], occurrence))
    db.commit()
    return cur.lastrowid


@pytest.mark.parametrize("value", [None, "0", "true", "yes", " 1"])
def test_switch_off_never_touches_db(db, connect_calls, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("FAULT_INJECTION")
    else:
        monkeypatch.setenv("FAULT_INJECTION", value)
    row_id = arm(db, C06)
    assert fault_injection.armed("u1", "step_x", *C06) is None
    assert fault_injection.mark_fired(row_id) is False
    assert connect_calls == []


@pytest.mark.parametrize("point", [C06, C08])
def test_one_shot_actions_disarm_after_firing(db, point):
    row_id = arm(db, point)
    row = fault_injection.armed("u1", "step_x", *point)
    assert row["id"] == row_id and row["fired_at"] is None
    assert fault_injection.mark_fired(row_id) is True
    assert fault_injection.armed("u1", "step_x", *point) is None


def test_fail_persistent_stays_armed_after_firing(db):
    row_id = arm(db, C12, step="step_remove_krb5")
    assert fault_injection.mark_fired(row_id) is True
    row = fault_injection.armed("u1", "step_remove_krb5", *C12)
    assert row["id"] == row_id and row["fired_at"] is not None


def test_arming_is_scoped_to_user_step_boundary_and_action(db):
    arm(db, C06)
    assert fault_injection.armed("u2", "step_x", *C06) is None
    assert fault_injection.armed("u1", "step_y", *C06) is None
    assert fault_injection.armed("u1", "step_x", *C08) is None
    assert fault_injection.armed("u1", "step_x", "X6", "response_loss") is None
    assert fault_injection.armed("u1", "step_x", "X4", "sigkill_before_journal") is None


def test_occurrence_2_lets_the_first_event_pass_and_fires_on_the_second(db):
    row_id = arm(db, C06, occurrence=2)
    assert fault_injection.armed("u1", "step_x", *C06) is None
    row = fault_injection.armed("u1", "step_x", *C06)
    assert row["id"] == row_id
    assert fault_injection.mark_fired(row_id) is True
    assert fault_injection.armed("u1", "step_x", *C06) is None


def test_fail_persistent_with_occurrence_2_keeps_failing_from_the_second_event(db):
    row_id = arm(db, C12, occurrence=2)
    assert fault_injection.armed("u1", "step_x", *C12) is None
    assert fault_injection.armed("u1", "step_x", *C12)["id"] == row_id
    fault_injection.mark_fired(row_id)
    assert fault_injection.armed("u1", "step_x", *C12)["id"] == row_id


def test_mark_fired_twice_only_first_wins(db):
    row_id = arm(db, C08)
    assert fault_injection.mark_fired(row_id) is True
    assert fault_injection.mark_fired(row_id) is False


def test_lookup_failure_means_not_armed(db, monkeypatch):
    arm(db, C06)

    def boom(**kw):
        raise RuntimeError("log db down")

    monkeypatch.setattr(fault_injection, "get_log_db_connection", boom)
    assert fault_injection.armed("u1", "step_x", *C06) is None
    assert fault_injection.mark_fired(1) is False


def test_kill_process_exits_137_only_when_switch_on(monkeypatch):
    codes = []
    monkeypatch.setattr(fault_injection.os, "_exit", codes.append)
    monkeypatch.delenv("FAULT_INJECTION", raising=False)
    fault_injection.kill_process()
    monkeypatch.setenv("FAULT_INJECTION", "1")
    fault_injection.kill_process()
    assert codes == [137]


def test_ddl_applies_twice():
    conn = sqlite3.connect(":memory:")
    conn.execute(fault_arming_ddl())
    conn.execute(fault_arming_ddl())
    cols = [r[1] for r in conn.execute("PRAGMA table_info(fault_arming)")]
    assert cols == ["id", "username", "boundary", "step_name", "action", "occurrence", "params_json",
                    "created_at", "fired_at"]
