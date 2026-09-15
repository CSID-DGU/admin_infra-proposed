"""내부 API 토큰: admin_be만 config-server를 부르도록 공유 토큰을 요구한다."""
import pytest

import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "s3cret")
    return main.app.test_client()


def test_request_without_token_is_rejected_before_validation(client):
    r = client.post("/operations/provision", json={})
    assert r.status_code == 401 and r.get_json()["error"] == "UNAUTHORIZED"


def test_wrong_token_is_rejected(client):
    r = client.delete("/pods/ailab-u-1", headers={"X-Internal-Token": "nope"})
    assert r.status_code == 401


def test_right_token_reaches_the_route(client):
    r = client.post("/operations/provision", json={}, headers={"X-Internal-Token": "s3cret"})
    assert r.status_code == 400   # 토큰은 통과하고 본문 검증에서 걸린다


def test_health_and_api_docs_stay_open(client):
    assert client.get("/health").status_code == 200
    assert client.get("/apispec_1.json").status_code == 200


def test_status_route_is_open_only_for_get(client):
    assert client.post("/requests/1/status").status_code == 401


def test_token_check_is_off_when_not_configured(monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "")
    assert main.app.test_client().post("/operations/provision", json={}).status_code == 400
