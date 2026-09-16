"""부가 저장소(Redis)가 멈춰도 서버는 제때 응답해야 한다.

제한이 없으면 저장소가 사라졌을 때 연결 시도가 20초 넘게 매달린다. /health는 이 저장소에서 값 하나를
읽는데, 조회 실패를 삼키도록 만들어 두었어도 시간 제한이 없으면 그 처리에 닿지 못한다. 그 사이 준비
상태 점검(5초)이 계속 실패해 배포가 10분을 기다린 뒤 중단됐다.
"""
from adapters import bg_img_redis, pod_status

# 준비 상태 점검 제한보다 넉넉히 짧아야 의미가 있다.
READINESS_TIMEOUT_SEC = 5


def _limits(client):
    kwargs = client.connection_pool.connection_kwargs
    return kwargs.get("socket_connect_timeout"), kwargs.get("socket_timeout")


def test_redis_clients_stop_waiting_before_readiness_probe():
    for module in (pod_status, bg_img_redis):
        connect_limit, command_limit = _limits(module.r)
        assert connect_limit is not None and command_limit is not None, f"{module.__name__}: 제한 없음"
        assert connect_limit < READINESS_TIMEOUT_SEC
        assert command_limit < READINESS_TIMEOUT_SEC
