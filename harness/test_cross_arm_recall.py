"""비교군이 달라도 trial_id 하나로 이벤트 전체를 회수할 수 있음을 증명한다.

docs/PLAN.md 2단계의 검증 방법이 이 파일이다. baseline 은 동기 경로여서 operation_log 의
job_id 가 NULL 이고, noprobe 는 제어기가 실행하므로 모든 행에 job_id 가 붙는다. 회수 코드가
job_id 를 조건에 넣는 순간 baseline 에서는 한 건도 걸리지 않으므로, events_of 는 신청 번호와
시간창만 본다. 여기서는 같은 신청 번호 아래에 두 벌의 저널을 넣고 양쪽이 같은 개수와 같은
순서로 돌아오는지를 확인한다.

표는 test_trial.py 와 같은 방식으로 infra-sql 의 DDL 파일을 읽어서 만든다. 베껴 쓰면 스키마가
두 곳으로 갈라진다.
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trial  # noqa: E402
from test_trial import Sql, _sqlite_ddl  # noqa: E402

BASE = datetime(2026, 9, 19, 9, 0, 0)
TIME_FMT = "%Y-%m-%d %H:%M:%S"

# 두 비교군이 똑같이 밟는 단계 순서. 회수된 이벤트의 모양이 비교군과 무관함을 보이려면
# 양쪽에 같은 순서를 넣고 같은 순서가 돌아오는지 보아야 한다.
SEQUENCE = [("CREATE_ACCOUNT", "START"), ("CREATE_ACCOUNT", "SUCCESS"),
            ("CREATE_POD_K8S", "START"), ("CREATE_POD_K8S", "SUCCESS"),
            ("WAIT_READY", "SUCCESS")]


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).strftime(TIME_FMT)


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.execute(_sqlite_ddl("trial_manifest.sql", "trial_manifest"))
    db.execute(_sqlite_ddl("operation_log.sql", "operation_log"))
    return Sql(db)


def _window(conn, trial_id, started, ended):
    """시간창을 정해진 시각으로 고정한다.

    sqlite 의 CURRENT_TIMESTAMP 는 1초 단위여서 한 초 안에 연 두 trial 이 같은 시각을 받고,
    그러면 두 창이 완전히 겹쳐 버려서 가르는 일 자체를 시험할 수 없다. 시각을 데이터베이스
    시계로 채운다는 계약은 test_trial.py 가 이미 검증하므로, 여기서는 창을 벌리기만 한다.
    창을 넓히지 않고 각각 10초로 좁게 잡는다.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE trial_manifest SET started_at = %s, ended_at = %s WHERE trial_id = %s",
                    (started, ended, trial_id))
    conn.commit()


def _open(conn, trial_id, *, method, operation, request_id, started, ended=None):
    trial.open_trial(conn, trial_id=trial_id, method=method, server_group="A",
                     operation=operation, horizon_sec=600, repetition=1,
                     revisions={"config-server": "abc1234"})
    trial.bind_request(conn, trial_id, request_id)
    if ended is not None:
        trial.close_trial(conn, trial_id)
    _window(conn, trial_id, started, ended)
    return trial_id


def _log(conn, request_id, created_at, action, phase, job_id):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO operation_log"
                    " (job_id, request_id, username, action, phase, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (job_id, request_id, "exp-user", action, phase, created_at))
    conn.commit()


def _journal(conn, request_id, first_second, job_id):
    for offset, (action, phase) in enumerate(SEQUENCE):
        _log(conn, request_id, at(first_second + offset), action, phase, job_id)


@pytest.fixture
def two_arms(conn):
    """같은 신청 번호 아래에 baseline 벌과 noprobe 벌을 서로 다른 시각으로 넣는다."""
    # baseline 벌: 동기 경로라서 job_id 가 없다. 첫 행이 started_at 과 정확히 같은 시각이다.
    _open(conn, "trial-base", method="baseline", operation="CREATE",
          request_id="req-1", started=at(0), ended=at(9))
    _journal(conn, "req-1", 0, None)
    # noprobe 벌: 제어기가 실행하므로 모든 행에 작업 번호가 붙는다.
    _open(conn, "trial-np", method="noprobe", operation="CREATE",
          request_id="req-1", started=at(20), ended=at(29))
    _journal(conn, "req-1", 20, 77)
    return conn


def test_each_trial_recalls_only_its_own_arm(two_arms):
    base = trial.events_of(two_arms, "trial-base")
    noprobe = trial.events_of(two_arms, "trial-np")

    assert len(base) == len(SEQUENCE)
    assert len(noprobe) == len(SEQUENCE)
    assert {e["created_at"] for e in base}.isdisjoint({e["created_at"] for e in noprobe})


def test_both_arms_recall_the_same_event_shape(two_arms):
    shape = lambda rows: [(e["action"], e["phase"]) for e in rows]

    assert shape(trial.events_of(two_arms, "trial-base")) == SEQUENCE
    assert shape(trial.events_of(two_arms, "trial-np")) == SEQUENCE


def test_recall_does_not_filter_on_job_id(two_arms):
    """baseline 벌은 job_id 가 NULL 인 채로 전부 돌아오고, noprobe 벌은 값을 단 채로 돌아온다."""
    assert [e["job_id"] for e in trial.events_of(two_arms, "trial-base")] == [None] * len(SEQUENCE)
    assert [e["job_id"] for e in trial.events_of(two_arms, "trial-np")] == [77] * len(SEQUENCE)


def test_the_row_at_started_at_is_included(two_arms):
    first = trial.events_of(two_arms, "trial-base")[0]

    assert first["created_at"] == at(0)


def test_create_and_revoke_trials_on_one_request_stay_apart(conn):
    """한 신청에 생성 trial 과 회수 trial 이 이어져 있어도 서로의 행을 가져오지 않는다."""
    _open(conn, "trial-c", method="full", operation="CREATE",
          request_id="req-9", started=at(0), ended=at(9))
    _log(conn, "req-9", at(1), "CREATE_ACCOUNT", "SUCCESS", 101)
    _open(conn, "trial-r", method="full", operation="REVOKE",
          request_id="req-9", started=at(30), ended=at(39))
    _log(conn, "req-9", at(31), "DELETE_ACCOUNT", "SUCCESS", 102)

    assert [e["action"] for e in trial.events_of(conn, "trial-c")] == ["CREATE_ACCOUNT"]
    assert [e["action"] for e in trial.events_of(conn, "trial-r")] == ["DELETE_ACCOUNT"]


def test_an_open_trial_keeps_recalling_rows_that_arrive_later(conn):
    """ended_at 이 아직 비어 있는 trial 은 뒤에 들어온 행까지 계속 회수한다."""
    _open(conn, "trial-open", method="noprobe", operation="CREATE",
          request_id="req-5", started=at(0))
    _log(conn, "req-5", at(1), "CREATE_ACCOUNT", "START", 5)
    assert len(trial.events_of(conn, "trial-open")) == 1

    _log(conn, "req-5", at(600), "WAIT_READY", "SUCCESS", 5)
    assert len(trial.events_of(conn, "trial-open")) == 2
