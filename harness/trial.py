"""trial manifest 를 쓰고 읽는 유일한 자리.

trial 의 식별자는 대상 시스템 바깥에서 붙인다. 계측을 하네스 쪽에 두면 비교군마다 계측
지점이 달라져서 지표가 오염되고, 반대로 trial 개념을 대상 시스템 안에 넣으면 baseline 이
"배포된 그대로" 라는 전제가 깨진다. 그래서 trial_manifest 는 대상 시스템이 건드리지 않는
별도 테이블이고, 쓰는 쪽은 이 모듈뿐이다.

어떤 operation_log 행이 어느 trial 에 속하는지는 신청 번호와 시간창으로 정한다. request_id
가 같고 created_at 이 started_at 이상이며, ended_at 이 있으면 그 이하인 행을 그 trial 의
이벤트로 본다. baseline 은 동기 경로여서 job_id 가 NULL 이라 작업 경계를 job_id 로 정할 수
없고, 시간창이 유일하게 세 비교군에 똑같이 적용되는 규칙이기 때문이다.

연결 객체는 부르는 쪽이 넘겨 준다. 이 모듈은 연결을 만들지 않으므로 MySQL 접속 설정을
갖지 않고, 테스트는 같은 DDL 을 sqlite 위에 올려서 그대로 검증할 수 있다.

시각은 전부 데이터베이스 시계로 채운다. operation_log 의 duration_ms 가 이미 데이터베이스
시계로 계산되므로, 파이썬 시계로 시간창을 만들면 두 테이블이 어긋난다.
"""
import json

_COLUMNS = ("trial_id", "scenario_id", "method", "server_group", "operation",
            "request_id", "horizon_sec", "repetition", "revisions", "started_at", "ended_at")


class TrialStateError(Exception):
    """trial 행이 없거나, 이미 정해진 값을 다시 정하려고 했다."""


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _one(conn, trial_id):
    with conn.cursor() as cur:
        cur.execute("SELECT request_id, started_at, ended_at FROM trial_manifest WHERE trial_id = %s",
                    (trial_id,))
        rows = _rows(cur)
    if not rows:
        raise TrialStateError(f"trial 행이 없다: {trial_id}")
    return rows[0]


def open_trial(conn, *, trial_id, method, server_group, operation,
               horizon_sec, repetition, revisions, scenario_id=None):
    """trial 행을 하나 넣는다. started_at 은 데이터베이스 시계로 채운다."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trial_manifest"
            " (trial_id, scenario_id, method, server_group, operation,"
            "  horizon_sec, repetition, revisions, started_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP(3))",
            (trial_id, scenario_id, method, server_group, operation,
             horizon_sec, repetition, json.dumps(revisions, sort_keys=True)))
    conn.commit()


def bind_request(conn, trial_id, request_id):
    """신청 번호가 정해진 뒤 그 값을 채운다.

    이미 다른 값이 들어 있으면 덮어쓰지 않고 예외를 올린다. 한 trial 이 두 신청을 가리키면
    시간창 조인이 어느 쪽 이벤트를 모은 것인지 말할 수 없게 되기 때문이다.
    """
    current = _one(conn, trial_id)["request_id"]
    if current is not None and current != request_id:
        raise TrialStateError(
            f"trial {trial_id} 는 이미 신청 {current} 에 묶여 있다. 새 값 {request_id} 로 덮어쓰지 않는다.")
    with conn.cursor() as cur:
        cur.execute("UPDATE trial_manifest SET request_id = %s WHERE trial_id = %s",
                    (request_id, trial_id))
    conn.commit()


def close_trial(conn, trial_id):
    """ended_at 을 데이터베이스 시계로 채운다. 이미 닫힌 trial 이면 예외를 올린다."""
    if _one(conn, trial_id)["ended_at"] is not None:
        raise TrialStateError(f"trial {trial_id} 는 이미 닫혀 있다. 시간창을 다시 정하지 않는다.")
    with conn.cursor() as cur:
        cur.execute("UPDATE trial_manifest SET ended_at = CURRENT_TIMESTAMP(3) WHERE trial_id = %s",
                    (trial_id,))
    conn.commit()


def events_of(conn, trial_id):
    """trial 의 시간창에 드는 operation_log 행을 created_at 오름차순으로 돌려준다.

    신청 번호가 아직 비어 있으면 빈 목록이다. 조회나 연결이 실패하면 예외를 그대로 올린다.
    이벤트가 없는 것과 조회하지 못한 것은 다른 사실이고, 뒤쪽을 빈 목록으로 삼키면 지표가
    조용히 0 이 된다.
    """
    row = _one(conn, trial_id)
    if row["request_id"] is None:
        return []
    sql = ("SELECT * FROM operation_log"
           " WHERE request_id = %s AND created_at >= %s")
    params = [row["request_id"], row["started_at"]]
    if row["ended_at"] is not None:
        sql += " AND created_at <= %s"
        params.append(row["ended_at"])
    sql += " ORDER BY created_at ASC, id ASC"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return _rows(cur)
