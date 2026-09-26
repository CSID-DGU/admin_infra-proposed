"""keytab 배포 전에 노드가 새 사용자를 조회할 수 있는지 먼저 기다린다. 기다림이 실패하면 배포하지 않는다."""
import types

import pytest

import main


@pytest.fixture
def farm(monkeypatch):
    calls = []
    secret = types.SimpleNamespace(data={"krb5.keytab": "a2V5dGFi"})
    api = types.SimpleNamespace(read_namespaced_secret=lambda **kw: secret)
    monkeypatch.setattr(main.client, "CoreV1Api", lambda: api)
    monkeypatch.setattr(main, "_get_farm_node_info", lambda name: {"host": "h", "port": "22"})
    monkeypatch.setattr(main, "_clear_krb5_cleanup_pending", lambda *a: None)
    return calls


def test_waits_for_identity_before_deploy(farm, monkeypatch):
    monkeypatch.setattr(main, "_farm_ssh", lambda host, port, cmd, stdin_data="": farm.append((cmd, stdin_data)))
    with main.app.app_context():
        main._deploy_krb5_to_farm("exp-fu-a", 55123, "farm8")
    assert farm == [("wait-identity exp-fu-a 55123", ""), ("deploy exp-fu-a 55123", "a2V5dGFi")]


def test_no_deploy_when_identity_never_resolves(farm, monkeypatch):
    def fake_ssh(host, port, cmd, stdin_data=""):
        farm.append(cmd)
        if cmd.startswith("wait-identity"):
            raise RuntimeError("identity not resolvable on this node")

    monkeypatch.setattr(main, "_farm_ssh", fake_ssh)
    with main.app.app_context(), pytest.raises(RuntimeError):
        main._deploy_krb5_to_farm("exp-fu-a", 55123, "farm8")
    assert farm == ["wait-identity exp-fu-a 55123"]
