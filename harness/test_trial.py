"""trial.py 계약 검증.

표는 손으로 베껴 쓰지 않고 infra-sql 의 DDL 파일을 읽어서 만든다. 베껴 쓰면 스키마가 두
곳으로 갈라져서 컬럼이 어긋나도 테스트는 계속 통과한다. sqlite 가 받지 못하는 MySQL 전용
구문(테이블 안의 INDEX 선언, ENGINE 절, AUTO_INCREMENT, CURRENT_TIMESTAMP(3))만 기계적으로
바꾸고 컬럼 목록 자체는 파일에 있는 그대로 쓴다.
"""
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trial  # noqa: E402

INFRA_SQL = Path(__file__).resolve().parent.parent / "infra-sql"
TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _sqlite_ddl(filename, table):
    """DDL 파일에서 해당 표의 정의만 뽑아 sqlite 방언으로 바꾼다."""
    sql = (INFRA_SQL / filename).read_text()
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = sql.replace("CURRENT_TIMESTAMP(3)", "CURRENT_TIMESTAMP")
    for stmt in sql.split(";"):
        head = stmt.strip().lower()
        if not head.startswith("create table") or f" {table} " not in head:
            continue
        stmt = stmt.split(" ENGINE=")[0]
        stmt = "\n".join(l for l in stmt.splitlines() if not l.strip().lower().startswith("index "))
        stmt = re.sub(r",\s*\)\s*$", ")", stmt.strip())
        return stmt.replace("BIGINT AUTO_INCREMENT PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    raise AssertionError(f"{filename} 에서 {table} 정의를 찾지 못했다")


class Sql:
    """pymysql 연결 흉내. %s 와 CURRENT_TIMESTAMP(3) 만 sqlite 방언으로 바꾼다."""

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
                self.c.execute(sql.replace("%s", "?").replace("CURRENT_TIMESTAMP(3)", "CURRENT_TIMESTAMP"),
                               params)

            @property
            def description(self):
                return self.c.description

            def fetchall(self):
                return self.c.fetchall()

            def fetchone(self):
                return self.c.fetchone()
        return Cur()

    def commit(self):
        self.db.commit()


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.execute(_sqlite_ddl("trial_manifest.sql", "trial_manifest"))
    db.execute(_sqlite_ddl("operation_log.sql", "operation_log"))
    return Sql(db)


def _open(conn, trial_id="trial-0001", **kw):
    kw.setdefault("method", "full")
    kw.setdefault("server_group", "A")
    kw.setdefault("operation", "CREATE")
    kw.setdefault("horizon_sec", 600)
    kw.setdefault("repetition", 1)
    kw.setdefault("revisions", {"config-server": "abc1234"})
    trial.open_trial(conn, trial_id=trial_id, **kw)
    return trial_id


def _row(conn, trial_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM trial_manifest WHERE trial_id = %s", (trial_id,))
        return dict(zip([d[0] for d in cur.description], cur.fetchone()))


def _log(conn, request_id, created_at, action="PROVISION"):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO operation_log (request_id, username, action, phase, created_at)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (request_id, "exp-user", action, "START", created_at))
    conn.commit()


def test_open_trial_writes_one_row_with_db_clock(conn):
    _open(conn)
    row = _row(conn, "trial-0001")
    assert row["method"] == "full"
    assert row["revisions"] == '{"config-server": "abc1234"}'
    assert row["started_at"]
    assert row["ended_at"] is None
    assert row["request_id"] is None


def test_bind_request_refuses_a_second_different_request(conn):
    _open(conn)
    trial.bind_request(conn, "trial-0001", "req-1")
    trial.bind_request(conn, "trial-0001", "req-1")  # 같은 값은 그대로 둔다
    with pytest.raises(trial.TrialStateError, match="req-1"):
        trial.bind_request(conn, "trial-0001", "req-2")
    assert _row(conn, "trial-0001")["request_id"] == "req-1"


def test_close_trial_fills_ended_at_and_refuses_a_second_close(conn):
    _open(conn)
    trial.close_trial(conn, "trial-0001")
    assert _row(conn, "trial-0001")["ended_at"]
    with pytest.raises(trial.TrialStateError, match="이미 닫혀"):
        trial.close_trial(conn, "trial-0001")


def test_events_of_keeps_the_boundary_rows_and_drops_the_rest(conn):
    _open(conn)
    trial.bind_request(conn, "trial-0001", "req-1")
    started = _row(conn, "trial-0001")["started_at"]
    before = (datetime.strptime(started, TIME_FMT) - timedelta(seconds=1)).strftime(TIME_FMT)
    _log(conn, "req-1", before, action="TOO_EARLY")
    _log(conn, "req-1", started, action="ON_START")

    trial.close_trial(conn, "trial-0001")
    ended = _row(conn, "trial-0001")["ended_at"]
    after = (datetime.strptime(ended, TIME_FMT) + timedelta(seconds=1)).strftime(TIME_FMT)
    _log(conn, "req-1", ended, action="ON_END")
    _log(conn, "req-1", after, action="TOO_LATE")
    _log(conn, "req-2", started, action="OTHER_REQUEST")

    assert [e["action"] for e in trial.events_of(conn, "trial-0001")] == ["ON_START", "ON_END"]


def test_events_of_is_empty_while_the_request_is_unbound(conn):
    _open(conn)
    _log(conn, "req-1", "2026-09-19 00:00:00")
    assert trial.events_of(conn, "trial-0001") == []
