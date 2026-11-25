import pytest

import main
import utils


def test_cluster_id_is_required_and_validated():
    with pytest.raises(SystemExit, match="required"):
        main._resolve_cluster_id({})
    with pytest.raises(SystemExit, match="lowercase DNS label"):
        main._resolve_cluster_id({"CLUSTER_ID": "LAB_1"})
    with pytest.raises(SystemExit, match="farm or lab"):
        main._resolve_cluster_id({"CLUSTER_ID": "test"})
    assert main._resolve_cluster_id({"CLUSTER_ID": "lab"}) == "LAB"


def test_generic_environment_wins_and_farm_name_is_fallback_only_for_farm(monkeypatch):
    monkeypatch.setattr(main, "CLUSTER_ID", "FARM")
    monkeypatch.setenv("FARM_SSH_USER", "legacy")
    monkeypatch.delenv("INFRA_SSH_USER", raising=False)
    assert main._env_with_farm_fallback("INFRA_SSH_USER", "FARM_SSH_USER") == "legacy"

    monkeypatch.setenv("INFRA_SSH_USER", "generic")
    assert main._env_with_farm_fallback("INFRA_SSH_USER", "FARM_SSH_USER") == "generic"


def test_lab_never_reads_farm_environment_or_runtime_fallback(monkeypatch):
    monkeypatch.setattr(main, "CLUSTER_ID", "LAB")
    monkeypatch.setenv("FARM_SSH_USER", "farm-secret-user")
    monkeypatch.delenv("INFRA_SSH_USER", raising=False)
    assert main._env_with_farm_fallback(
        "INFRA_SSH_USER", "FARM_SSH_USER", "lab-default"
    ) == "lab-default"

    monkeypatch.setitem(main.app.config, "INFRA_SSH_USER", "")
    monkeypatch.setitem(main.app.config, "FARM_SSH_USER", "farm-secret-user")
    assert main._infra_setting("SSH_USER") is None

    monkeypatch.setenv("INFRA_SSH_USER", "lab-user")
    assert main._env_with_farm_fallback("INFRA_SSH_USER", "FARM_SSH_USER") == "lab-user"


def test_home_mount_template_supports_each_cluster_without_farm_name_assumption():
    assert utils.resolve_home_mount_root("lab8", "/srv/{node}/home") == "/srv/lab8/home"
    assert utils.resolve_home_mount_root("lab8", "/srv/lab-{index}/home") == "/srv/lab-8/home"


def test_legacy_farm_home_mount_rule_remains_as_fallback():
    assert utils.resolve_home_mount_root("farm6", "") == "/home/tako6/share/user"
    with pytest.raises(ValueError, match="HOME_MOUNT_ROOT_TEMPLATE is required"):
        utils.resolve_home_mount_root("lab6", "")


def test_health_exposes_config_identity(api):
    body = api.get("/health").get_json()
    assert body["cluster_id"] == main.CLUSTER_ID
    assert body["status"] == "OK"
