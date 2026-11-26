"""LAB API-bootstrap의 HTTP 쓰기 차단 계약."""

import pytest

import main


def test_read_only_env_is_strict_and_defaults_to_existing_farm_behavior():
    assert main._resolve_api_read_only({}) is False
    assert main._resolve_api_read_only({"CONFIG_API_READ_ONLY": "true"}) is True
    assert main._resolve_api_read_only({"CONFIG_API_READ_ONLY": " FALSE "}) is False
    with pytest.raises(SystemExit, match="must be true or false"):
        main._resolve_api_read_only({"CONFIG_API_READ_ONLY": "tru"})


def test_maintenance_env_is_strict_and_defaults_to_disabled():
    assert main._resolve_api_maintenance({}) is False
    assert main._resolve_api_maintenance({"CONFIG_API_MAINTENANCE": "true"}) is True
    assert main._resolve_api_maintenance({"CONFIG_API_MAINTENANCE": " FALSE "}) is False
    with pytest.raises(SystemExit, match="must be true or false"):
        main._resolve_api_maintenance({"CONFIG_API_MAINTENANCE": "on"})


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_read_only_mode_rejects_every_mutating_http_method(monkeypatch, method):
    monkeypatch.setattr(main, "API_READ_ONLY", True)
    response = getattr(main.app.test_client(), method)("/operations/provision", json={})

    assert response.status_code == 403
    assert response.get_json()["error"] == "CONFIG_API_READ_ONLY"
    assert response.headers["X-Config-Read-Only"] == "true"


def test_read_only_rejection_runs_after_authentication(monkeypatch):
    monkeypatch.setattr(main, "API_READ_ONLY", True)
    monkeypatch.setattr(main, "API_AUTH_DISABLED", False)
    monkeypatch.setattr(main, "API_TOKEN", "secret")

    response = main.app.test_client().post("/operations/provision", json={})

    assert response.status_code == 401
    assert response.get_json()["error"] == "UNAUTHORIZED"


def test_read_only_mode_keeps_health_and_authenticated_reads_available(monkeypatch):
    monkeypatch.setattr(main, "API_READ_ONLY", True)
    client = main.app.test_client()

    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["read_only"] is True

    # 존재하지 않는 작업 조회라도 middleware가 GET을 차단하지 않았음은 403이 아닌 것으로 증명한다.
    status = client.get("/operations/provision/not-found")
    assert status.status_code != 403


def test_default_mode_preserves_existing_farm_write_route(monkeypatch):
    monkeypatch.setattr(main, "API_READ_ONLY", False)
    monkeypatch.setattr(main, "API_MAINTENANCE", False)
    response = main.app.test_client().post("/operations/provision", json={})
    assert response.status_code == 400


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_maintenance_mode_rejects_mutations_but_keeps_health(monkeypatch, method):
    monkeypatch.setattr(main, "API_READ_ONLY", False)
    monkeypatch.setattr(main, "API_MAINTENANCE", True)
    client = main.app.test_client()

    response = getattr(client, method)("/operations/provision", json={})
    assert response.status_code == 503
    assert response.get_json()["error"] == "CONFIG_API_MAINTENANCE"
    assert response.headers["X-Config-Maintenance"] == "true"
    assert response.headers["Retry-After"] == "30"

    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["maintenance"] is True


def test_maintenance_rejection_runs_after_authentication(monkeypatch):
    monkeypatch.setattr(main, "API_READ_ONLY", False)
    monkeypatch.setattr(main, "API_MAINTENANCE", True)
    monkeypatch.setattr(main, "API_AUTH_DISABLED", False)
    monkeypatch.setattr(main, "API_TOKEN", "secret")

    response = main.app.test_client().post("/operations/provision", json={})
    assert response.status_code == 401
    assert response.get_json()["error"] == "UNAUTHORIZED"
