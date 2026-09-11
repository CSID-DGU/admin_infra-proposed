import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import main  # noqa: E402


@pytest.fixture
def logs(monkeypatch):
    """log_operation 호출을 DB 대신 목록에 모은다."""
    records = []
    monkeypatch.setattr(main, "log_operation", lambda **kw: records.append(kw))
    return records


@pytest.fixture
def pod_status(monkeypatch):
    """Redis 진행 상황 기록 대역. set_pod_creation_status 호출 인자를 모은다."""
    calls = []
    monkeypatch.setattr(main, "set_pod_creation_status", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(main, "get_pod_creation_status", lambda *a, **k: None)
    return calls


@pytest.fixture
def api():
    return main.app.test_client()
