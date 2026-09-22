import os
import uuid
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

REDIS_HOST = os.getenv("REDIS_HOST", "redis-bg-master.ailab-infra.svc.cluster.local")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

# 제한을 두는 이유는 pod_status.py/bg_img_redis.py와 같다 — 저장소가 멈춰도 서버는 응답해야 한다.
REDIS_TIMEOUT_SEC = float(os.getenv("REDIS_TIMEOUT_SEC", "2"))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True,
                socket_connect_timeout=REDIS_TIMEOUT_SEC, socket_timeout=REDIS_TIMEOUT_SEC,
                retry=Retry(NoBackoff(), 0))

_LOCK_KEY = "nas_gss_flush:inflight"

# GET+DEL을 원자적으로 묶어야 한다 — 따로 하면 "값 확인 후 지우기" 사이에 락이 만료되고
# 다른 워커가 새로 잡아버린 락을 지워버릴 수 있다(TOCTOU).
_RELEASE_IF_OWNER_SCRIPT = r.register_script("""
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
""")


def try_acquire_flush_lock(ttl_sec: int) -> str | None:
    """온디맨드 재시도 루프가 gunicorn 워커 여러 개에 걸쳐 중복으로 돌지 않도록 하는 락.

    gunicorn이 워커 4개라 워커 하나의 in-memory 상태로는 다른 워커를 못 막는다 — 그래서
    프로세스 공유 저장소인 Redis(이미 job 등록에 쓰던 것과 같은 인스턴스)를 쓴다.

    TTL을 락의 유일한 해제 수단으로 둔다(명시적 해제도 하지만, 그게 실패해도 TTL이 결국
    풀어준다) — 워커가 죽어도 락이 영원히 안 풀리는 사고를 막기 위함.

    성공하면 이번에 잡은 소유자 토큰을 반환한다(실패하면 None) — release_flush_lock에
    그대로 넘겨야 한다. 값을 상수로 두지 않고 매번 새 토큰으로 두는 이유: TTL이 만료된
    직후 다른 워커가 새로 락을 잡았는데, 그 순간 예전 소유자가 뒤늦게 무조건 delete를
    호출하면 남의 락을 지워버린다 — 토큰이 일치할 때만 지우게 해서 막는다."""
    token = uuid.uuid4().hex
    return token if r.set(_LOCK_KEY, token, nx=True, ex=ttl_sec) else None


def release_flush_lock(owner_token: str) -> None:
    try:
        _RELEASE_IF_OWNER_SCRIPT(keys=[_LOCK_KEY], args=[owner_token])
    except Exception:
        pass  # TTL이 결국 풀어준다 — 해제 실패가 치명적이지 않다.
