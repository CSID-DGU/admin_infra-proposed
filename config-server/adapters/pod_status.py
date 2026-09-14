import os
import json
import redis
from datetime import datetime, timezone

REDIS_HOST = os.getenv("REDIS_HOST", "redis-bg-master.ailab-infra.svc.cluster.local")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

STATUS_TTL_SEC = 3600  # 완료/실패 후에도 조회 가능하도록 1시간 유지, 이후 자동 만료


def set_pod_creation_status(key_value, stage: str, message: str = "") -> None:
    # key_value는 create-pod 경로에서는 request_id, migrate 경로에서는 아직 username이다
    # (한 사용자가 Pod를 여러 개 동시에 만들 수 있게 되면서, username 하나로는 서로 다른
    # 생성 시도의 진행 상황이 같은 키에서 덮어써져 구분이 안 됐다).
    key = f"pod_status:{key_value}"
    data = {
        "stage": stage,
        "message": message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        r.set(key, json.dumps(data), ex=STATUS_TTL_SEC)
    except Exception:
        pass  # 상태 조회는 부가 기능 — Redis 장애가 pod 생성 자체를 막으면 안 됨


def get_pod_creation_status(key_value):
    key = f"pod_status:{key_value}"
    raw = r.get(key)
    if raw is None:
        return None
    return json.loads(raw)


# ---------- v2.0 작업 입력 ----------
# 작업 등록 API가 저장하고 제어기가 꺼내 쓴다. 진행 상황과 달리 만료시키지 않고 작업이 끝나면
# 제어기가 지운다(실험 스택은 이 Redis에 영속화를 켜 두어 Redis가 재시작돼도 남는다).
# 진행 상황 저장과 달리 실패를 삼키지 않는다 — 저장이 안 되면 작업 등록 자체가 실패해야 한다.

def _job_key(action, request_id):
    return f"op_job:{action}:{request_id}"


def save_job_input(action, request_id, job) -> bool:
    """같은 작업이 이미 등록돼 있으면 덮어쓰지 않고 False."""
    return bool(r.set(_job_key(action, request_id), json.dumps({"state": "queued", "job": job}), nx=True))


def load_job_input(action, request_id):
    raw = r.get(_job_key(action, request_id))
    return json.loads(raw) if raw else None


def mark_job_running(action, request_id):
    key = _job_key(action, request_id)
    raw = r.get(key)
    if raw:
        stored = json.loads(raw)
        stored["state"] = "running"
        r.set(key, json.dumps(stored))


def mark_job_done(action, request_id, result):
    """작업은 끝났지만 결과 행 기록에 실패했을 때, 결과를 들고 "done"으로 남긴다(다음 바퀴에 기록만 재시도)."""
    key = _job_key(action, request_id)
    raw = r.get(key)
    stored = json.loads(raw) if raw else {}
    stored.update(state="done", result=result)
    r.set(key, json.dumps(stored))


def delete_job_input(action, request_id):
    r.delete(_job_key(action, request_id))


# 작업이 만든 자원. admin_be가 작업 결과를 받아 신청 기록에 반영하는 데 쓴다(계정 uid/gid, Pod 이름·
# 노드, 외부 포트). 작업 결과 행에는 남지 않는 값이라 따로 둔다. 신청 처리에 쓰고 나면 필요 없어
# 만료시킨다. 진행 상황과 같이 저장 실패는 삼킨다 — 부가 정보 저장이 작업 결과 기록을 막으면 안 된다.
JOB_RESULT_TTL_SEC = int(os.getenv("JOB_RESULT_TTL_SEC", str(24 * 3600)))


def _job_result_key(action, request_id):
    return f"op_job_result:{action}:{request_id}"


def save_job_result(action, request_id, detail) -> None:
    try:
        r.set(_job_result_key(action, request_id), json.dumps(detail), ex=JOB_RESULT_TTL_SEC)
    except Exception:
        pass


def load_job_result(action, request_id):
    try:
        raw = r.get(_job_result_key(action, request_id))
    except Exception:
        return None
    return json.loads(raw) if raw else None
