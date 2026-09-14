"""작업 소유권(lease)과 진행 기록 (v2.1).

operation_log는 append-only 저널이라 "지금 누가 이 작업을 잡고 있나", "어느 단계까지 끝냈나" 같은
가변 상태를 담지 않는다. 그 둘을 job_control 한 행(작업당 1행)에 두고, 작업이 끝나면 지운다.

- lease: 제어기 프로세스 하나가 작업의 한시적 소유권을 갖는다. 갱신이 끊기면(프로세스 사망)
  lease_until이 지나 다른 제어기가 인수한다. 소유자가 아닌 프로세스의 기록은 거부되어,
  늦게 깨어난 옛 소유자가 새 소유자의 진행을 덮어쓰지 못한다(fencing).
- done_steps / saved_ctx: 단계가 끝날 때마다 기록해, 재시작한 제어기가 같은 pod_name·uid·포트로
  중단된 단계부터 이어간다.

ponytail: lease 시각은 DB 시계가 아니라 파이썬 epoch 초다 — 제어기들이 같은 클러스터 NTP를 쓰므로
드리프트는 TTL(30초)보다 훨씬 작다. 사이트를 넘는 배포가 생기면 DB 시계로 바꾼다.
"""
import json
import logging
import os
import time
import uuid

from utils import get_log_db_connection

logger = logging.getLogger(__name__)

# 프로세스 식별자. import 시점에 정해져 프로세스 안에서는 불변이다.
OWNER = f"{os.getenv('HOSTNAME', 'local')}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
LEASE_TTL_SEC = float(os.getenv("JOB_LEASE_TTL_SEC", "30"))


class LeaseLost(Exception):
    """이 프로세스가 더 이상 작업의 소유자가 아니다. 새 소유자가 이어가므로 조용히 물러난다."""


def claim(job_id, request_id, action, owner=None, ttl_sec=None):
    """작업 소유권 선점. 성공하면 (done_steps, saved_ctx), 다른 살아있는 소유자가 있으면 None.
    같은 소유자의 재선점과 만료된 lease 인수는 성공한다."""
    owner = owner or OWNER
    ttl = ttl_sec or LEASE_TTL_SEC
    now = time.time()
    conn = get_log_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT owner, lease_until, done_steps, saved_ctx "
                        "FROM job_control WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO job_control (job_id, request_id, action, owner, lease_until,"
                    " done_steps, saved_ctx) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (job_id, str(request_id), action, owner, now + ttl, "[]", "{}"))
                conn.commit()
                return [], {}
            held_by, until, done, saved = row
            if held_by != owner and until is not None and float(until) > now:
                conn.commit()
                return None
            # 만료 lease 인수 경쟁: WHERE에 직전 소유자·시각을 걸어 한 프로세스만 이긴다.
            cur.execute("UPDATE job_control SET owner=%s, lease_until=%s "
                        "WHERE job_id=%s AND owner=%s AND lease_until=%s",
                        (owner, now + ttl, job_id, held_by, until))
            won = cur.rowcount == 1
            conn.commit()
            if not won:
                return None
            return (json.loads(done) if done else []), (json.loads(saved) if saved else {})
    finally:
        conn.close()


def record_step(job_id, done_steps, saved_ctx, owner=None):
    """끝난 단계 목록과 이어하기 컨텍스트를 기록한다. 소유권이 넘어갔으면 LeaseLost."""
    conn = get_log_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE job_control SET done_steps=%s, saved_ctx=%s "
                        "WHERE job_id=%s AND owner=%s",
                        (json.dumps(done_steps), json.dumps(saved_ctx, default=str),
                         job_id, owner or OWNER))
            lost = cur.rowcount == 0
            conn.commit()
        if lost:
            raise LeaseLost(f"job {job_id}")
    finally:
        conn.close()


def renew(job_ids, owner=None, ttl_sec=None):
    """실행 중 작업들의 lease 갱신. 갱신된 건수를 돌려준다."""
    if not job_ids:
        return 0
    conn = get_log_db_connection()
    try:
        with conn.cursor() as cur:
            marks = ",".join(["%s"] * len(job_ids))
            cur.execute(f"UPDATE job_control SET lease_until=%s "
                        f"WHERE owner=%s AND job_id IN ({marks})",
                        [time.time() + (ttl_sec or LEASE_TTL_SEC), owner or OWNER, *job_ids])
            n = cur.rowcount
            conn.commit()
            return n
    finally:
        conn.close()


def release(job_id, owner=None):
    """작업 종료 후 행 제거. 소유자가 아니면 아무것도 지우지 않는다."""
    conn = get_log_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM job_control WHERE job_id=%s AND owner=%s",
                        (job_id, owner or OWNER))
            conn.commit()
    finally:
        conn.close()
