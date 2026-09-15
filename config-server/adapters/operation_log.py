"""
v1.0: operation_log(log-mysql/operation_state_db)에 기록

main.py의 실제 계정/Pod 생성 로직은 변경되지 않으며,
그 로직 사이사이에 log_operation() 호출만 끼워 넣는 방식으로 사용해 로그를 기록
"""
from contextvars import ContextVar
from enum import Enum

from flask import current_app as app

from utils import get_log_db_connection
from adapters.bg_img_redis import r as _redis

# 기록 실패(유실) 카운터 — 스택 자기 Redis 에 쌓이고 stack-down 때 함께 사라진다.
# gunicorn 워커 4개 + 제어기가 별도 프로세스라 메모리 카운터로는 합산이 안 된다.
_WRITE_FAILURES_KEY = "oplog:write_failures"


def _count_write_failure():
    """이력 기록 실패를 센다. 카운터 자신이 흐름을 막으면 안 되므로 실패는 무시한다 —
    이중 실패(로그 DB·Redis 동시 다운)의 최후 백업은 호출부의 app.logger 행이다."""
    try:
        _redis.incr(_WRITE_FAILURES_KEY)
    except Exception:
        pass


def write_failure_count():
    """/health 노출용 누적 유실 건수. 조회 불능은 0(유실 없음)과 구분해 None."""
    try:
        return int(_redis.get(_WRITE_FAILURES_KEY) or 0)
    except Exception:
        return None


# 제어기가 지금 실행 중인 작업의 번호(작업 시작 행의 id). 제어기가 작업을 실행하는 동안 여기에 두면
# 그 사이의 모든 기록에 job_id가 붙어, 단계 함수가 작업 번호를 몰라도 된다. 동기 엔드포인트는 비어 있다.
current_job_id = ContextVar("current_job_id", default=None)
# 실행 중 단계의 시도 번호(v2.1). 재시도 엔진이 단계를 다시 돌릴 때 올려 두면, 단계 안의 모든
# log_operation 호출이 attempt 인자 없이도 그 시도 번호로 기록된다.
current_attempt = ContextVar("current_attempt", default=1)
# 제어기가 실행 중인 작업의 사용자. 진행 상황 기록이 작업 이력에 사용자를 채우는 데 쓴다.
current_username = ContextVar("current_username", default=None)


class Action(str, Enum):
    """
    action 속성에 들어갈 값 전체 목록
    """

    CREATE_ACCOUNT = "CREATE_ACCOUNT"
    # 계정 생성 단계에서 계정 다음에 이어지는 두 단계. 계정과 action을
    # 나눠야 단계별 소요시간이 따로 잡히고, 어느 단계에서 실패했는지가 action만으로 드러난다.
    CREATE_HOME = "CREATE_HOME"
    CREATE_KRB5_PRINCIPAL = "CREATE_KRB5_PRINCIPAL"
    FETCH_USER_CONFIG = "FETCH_USER_CONFIG"  # WAS에서 사용자 설정 조회
    SELECT_NODE = "SELECT_NODE"              # Prometheus 기반 노드 선택
    ALLOCATE_NODEPORT = "ALLOCATE_NODEPORT"
    CREATE_POD_K8S = "CREATE_POD_K8S"
    WAIT_READY = "WAIT_READY"
    DEPLOY_KRB5 = "DEPLOY_KRB5"
    CREATE_SERVICE = "CREATE_SERVICE"
    DELETE_SERVICE = "DELETE_SERVICE"
    RELEASE_NODEPORT = "RELEASE_NODEPORT"
    DELETE_POD_K8S = "DELETE_POD_K8S"
    # 회수 경로. Pod/Service/NodePort 제거만 기록하면 계정과 인증 정보가 실제로 지워졌는지를
    # 이력에서 확인할 수 없다 — 회수 완료로 기록됐는데 접근 경로가 남는 경우를 판별하려면
    # 이 세 단계가 함께 남아야 한다.
    DELETE_ACCOUNT = "DELETE_ACCOUNT"
    DELETE_HOME = "DELETE_HOME"
    REMOVE_KRB5 = "REMOVE_KRB5"
    # 접근 검증(VERIFY_MODE=full, v3.0). resource_type이 시험 이름, error_detail이 관찰 근거다.
    VERIFY_ACCESS = "VERIFY_ACCESS"
    VERIFY_REVOKED = "VERIFY_REVOKED"
    # v2.0 작업 단위 행. 작업 등록 시 START, 제어기가 작업을 끝내면 SUCCESS/FAIL/UNKNOWN.
    # 제어기는 START만 있고 끝이 없는 행으로 아직 끝나지 않은 작업을 찾는다.
    PROVISION = "PROVISION"
    REVOKE = "REVOKE"
    MIGRATE = "MIGRATE"
    # 진행 상황 변화(이미지 다운로드 중 → 컨테이너 시작 중 등). 제어기가 작업을 실행하는 동안 단계가 바뀔 때마다
    # 남긴다. resource_type이 진행 단계 이름, error_detail이 {"stage","message"}다.
    PROGRESS = "PROGRESS"


class Phase(str, Enum):
    START = "START"
    SUCCESS = "SUCCESS"
    FAIL = "FAIL"
    RETRY = "RETRY"
    # 요청은 나갔으나 응답을 못 받아 실제로 실행됐는지 알 수 없음(timeout). FAIL과 구분한다 (v2.0)
    UNKNOWN = "UNKNOWN"
    # 결과가 아니라 기록용 행(진행 상황 변화)
    INFO = "INFO"


def _lookup_elapsed_ms(conn, request_id, action, attempt):
    """
    같은 (request_id, action, attempt)의 가장 최근 START 행으로부터 지금까지 걸린 시간(ms)
    duration_ms 계산에 사용

    START 행의 created_at은 MySQL 서버 시계로 찍힌다. 끝 시각을 파이썬 쪽 datetime.now()로
    잡으면 config-server Pod와 MySQL Pod의 시계나 시간대 설정이 다를 때 값이 통째로
    어긋나므로(TZ가 한쪽에만 설정되면 9시간), 양쪽 끝을 모두 DB 시계로 잰다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TIMESTAMPDIFF(MICROSECOND, created_at, NOW(3)) DIV 1000 "
            "FROM operation_log "
            "WHERE request_id=%s AND action=%s AND attempt=%s AND phase=%s "
            "ORDER BY id DESC LIMIT 1",
            (request_id, action, attempt, Phase.START.value),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None


def log_operation(
    *,
    request_id,
    username,
    action,
    phase,
    pod_name=None,
    node_name=None,
    resource_type=None,
    attempt=1,
    duration_ms=None,
    error_code=None,
    error_detail=None,
    raise_errors=False,
    job_id=None,
    target_state=None,
    start_job=False,
):
    """
    operation_log에 한 줄 기록. 절대 예외를 밖으로 던지지 않음
    로깅 실패가 실제 계정/Pod 생성 흐름을 막으면 안 되므로 실패하면 app.logger에만 남김

    duration_ms를 안 넘기고 phase가 SUCCESS/FAIL이면,
    같은 (request_id, action, attempt)의 START로부터 걸린 시간을 DB 시계로 계산해 채움

    job_id를 안 넘기면 current_job_id(제어기가 실행 중인 작업)를 쓴다. start_job=True면 이 행이 작업의
    시작 행이라 job_id를 자기 id로 채운다. 기록한 행의 id를 돌려준다(실패하면 None).

    raise_errors=True면 기록 실패를 호출자에게 다시 던진다. 작업 등록처럼 이 행 자체가
    이후 처리의 근거인 경우에만 쓴다.
    """
    action_value = action.value if isinstance(action, Action) else action
    phase_value = phase.value if isinstance(phase, Phase) else phase
    # admin_be는 신청 PK를 숫자로 보낸다. 컬럼이 VARCHAR라 숫자 그대로 비교하면 MySQL이
    # 컬럼 쪽을 숫자로 변환해 인덱스를 못 타고, 숫자로 시작하는 다른 키와도 같다고 판정할
    # 수 있으므로 기록·조회 모두 문자열로 맞춘다.
    request_id = str(request_id)
    if job_id is None:
        job_id = current_job_id.get()
    if attempt == 1:
        attempt = current_attempt.get()

    conn = None
    try:
        conn = get_log_db_connection()

        if duration_ms is None and phase_value in (Phase.SUCCESS.value, Phase.FAIL.value, Phase.UNKNOWN.value):
            duration_ms = _lookup_elapsed_ms(conn, request_id, action_value, attempt)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO operation_log "
                "(job_id, request_id, username, pod_name, node_name, resource_type, "
                " action, phase, attempt, duration_ms, error_code, error_detail, target_state) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    job_id, request_id, username, pod_name, node_name, resource_type,
                    action_value, phase_value, attempt, duration_ms,
                    error_code, error_detail, target_state,
                ),
            )
            row_id = cur.lastrowid
            if start_job:
                cur.execute("UPDATE operation_log SET job_id=%s WHERE id=%s", (row_id, row_id))
        conn.commit()
        return row_id

    except Exception:
        app.logger.exception(
            f"[OPERATION LOG] insert failed request_id={request_id} "
            f"action={action_value} phase={phase_value}"
        )
        _count_write_failure()
        if raise_errors:
            raise
    finally:
        if conn is not None:
            conn.close()
