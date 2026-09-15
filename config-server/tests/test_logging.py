"""config-server 로그는 한 번만 찍힌다(Flask 기본 출력과 사용자 정의 출력이 겹치지 않는다)."""
from flask.logging import default_handler

import main


def test_single_log_handler():
    assert default_handler not in main.app.logger.handlers
    assert len(main.app.logger.handlers) == 1
