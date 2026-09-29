"""장애 주입 장전 표(fault_arming)를 읽고 발동 표시를 남긴다. 실험 스택 전용.

운영 스택에서는 FAULT_INJECTION 이 설정되지 않아 모든 함수가 DB 도 보지 않고 곧바로 돌아간다.
스위치는 호출 시점에 읽는다. 정확히 "1" 일 때만 켜진다.

훅은 (boundary, action) 으로 분기한다. 논문팀 경계 X0~X11 에 주입을 더할 때 훅 위치만 늘리면 된다.
occurrence 는 같은 사용자·단계·경계·동작에서 몇 번째 사건(armed 호출)에 걸지를 뜻한다. 기본 1.

발동 표시(fired_at)는 장애를 일으키기 전에 먼저 커밋한다. X6 sigkill_before_journal 은 os._exit 로 제어기를 죽이므로
표시가 kill 뒤로 밀리면 기록되지 않고, 재시작한 제어기가 같은 행을 다시 보고 같은 지점에서 또 죽는다.
mark_fired 의 WHERE fired_at IS NULL 은 여러 제어기 중 한 프로세스만 발동하게 한다.

조회가 실패하면 발동하지 않는다. 주입 장치의 고장이 대상 시스템의 정상 동작을 바꾸면 안 되기 때문이다.
발동하지 않은 trial 은 하네스가 trial 뒤에 fired_at 을 읽어 따로 센다.
"""
import logging
import os

from adapters.job_control import CHECKPOINT_DB_TIMEOUTS
from utils import get_log_db_connection

logger = logging.getLogger(__name__)

# 발동 뒤에도 trial 이 끝날 때까지 유지하는 동작. 나머지는 한 번만 발동한다.
PERSISTENT = {"fail_persistent"}
_COLUMNS = ("id", "username", "boundary", "step_name", "action", "occurrence", "fired_at")
# 장전 행 id -> 지금까지 본 사건 수.
# ponytail: 프로세스 안에서만 센다. 제어기가 재시작하면 0 부터 다시 센다. 재시작을 넘어 세야 하면 표에 칸을 둔다.
_seen = {}


def enabled():
    return os.getenv("FAULT_INJECTION") == "1"


def armed(username, step_name, boundary, action):
    """이번 사건에 걸린 장전 행을 dict 로 돌려준다. 없거나, 아직 occurrence 번째 사건이 아니거나,
    스위치가 꺼졌거나, 조회가 실패하면 None. 부를 때마다 사건 하나로 센다."""
    if not enabled():
        return None
    sql = ("SELECT id, username, boundary, step_name, action, occurrence, fired_at FROM fault_arming"
           " WHERE username=%s AND step_name=%s AND boundary=%s AND action=%s")
    if action not in PERSISTENT:
        sql += " AND fired_at IS NULL"
    sql += " ORDER BY id LIMIT 1"
    try:
        conn = get_log_db_connection(**CHECKPOINT_DB_TIMEOUTS)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, (username, step_name, boundary, action))
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("fault_arming lookup failed, not firing: user=%s step=%s boundary=%s action=%s err=%s",
                       username, step_name, boundary, action, e)
        return None
    if not row:
        return None
    row = dict(zip(_COLUMNS, row))
    _seen[row["id"]] = seen = _seen.get(row["id"], 0) + 1
    if seen < (row["occurrence"] or 1):
        return None
    return row


def mark_fired(row_id):
    """발동 표시를 커밋한다. 이 프로세스가 처음 표시했으면 True, 이미 표시됐거나 실패하면 False(발동하지 않는다)."""
    if not enabled():
        return False
    try:
        conn = get_log_db_connection(**CHECKPOINT_DB_TIMEOUTS)
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE fault_arming SET fired_at = CURRENT_TIMESTAMP(3)"
                            " WHERE id = %s AND fired_at IS NULL", (row_id,))
                won = cur.rowcount == 1
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("fault_arming mark_fired failed, not firing: id=%s err=%s", row_id, e)
        return False
    return won


def kill_process():
    """X6 sigkill_before_journal: 제어기를 곧바로 끝낸다. 시험에서 바꿔 끼울 수 있게 모듈 수준에 둔다."""
    if not enabled():
        return
    os._exit(137)
