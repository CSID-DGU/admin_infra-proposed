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
    monkeypatch.setattr(main, "_get_farm_node_info", lambda name: {"name": name, "host": "h", "port": "22"})
    monkeypatch.setattr(main, "_clear_krb5_cleanup_pending", lambda *a: None)
    return calls


def _node_answers(farm, states):
    """check-identity 에 차례로 states 를 돌려주는 가짜 SSH. deploy 호출도 기록한다."""
    answers = iter(states)

    def fake_ssh(host, port, cmd, stdin_data=""):
        farm.append(cmd)
        if cmd.startswith("check-identity"):
            return f"identity_state={next(answers)}\n"
        return ""
    return fake_ssh


def test_deploys_once_node_sees_identity(farm, monkeypatch):
    monkeypatch.setattr(main, "_farm_ssh", _node_answers(farm, ["ready"]))
    with main.app.app_context():
        main._deploy_krb5_to_farm("exp-fu-a", 55123, "farm8")
    assert farm == ["check-identity exp-fu-a 55123", "deploy exp-fu-a 55123"]


def test_keeps_checking_until_caches_expire(farm, monkeypatch):
    monkeypatch.setattr(main, "_farm_ssh", _node_answers(
        farm, ["name_pending got=none", "home_pending owner=65534", "ready"]))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    with main.app.app_context():
        main._deploy_krb5_to_farm("exp-fu-a", 55123, "farm8")
    assert farm == ["check-identity exp-fu-a 55123"] * 3 + ["deploy exp-fu-a 55123"]


def test_no_deploy_when_identity_never_ready(farm, monkeypatch):
    monkeypatch.setattr(main, "_farm_ssh", _node_answers(farm, ["home_pending owner=65534"] * 3))
    monkeypatch.setattr(main, "NODE_IDENTITY_WAIT_SEC", 0)
    with main.app.app_context(), pytest.raises(main.NodeIdentityTimeout, match="home_pending"):
        main._deploy_krb5_to_farm("exp-fu-a", 55123, "farm8")
    assert farm == ["check-identity exp-fu-a 55123"]


def test_unreadable_answer_is_an_error_not_a_wait(farm, monkeypatch):
    monkeypatch.setattr(main, "_farm_ssh", lambda host, port, cmd, stdin_data="": farm.append(cmd) or "unknown action")
    with main.app.app_context(), pytest.raises(RuntimeError, match="해석하지 못함"):
        main._deploy_krb5_to_farm("exp-fu-a", 55123, "farm8")
    assert farm == ["check-identity exp-fu-a 55123"]


def test_ad_replication_wait_uses_longer_ssh_timeout(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "_farm_ad_ssh",
                        lambda cmd, stdin_data="", timeout=30: seen.update(cmd=cmd, timeout=timeout))
    main._await_ad_replicated("exp-fu-a", 55123)
    assert seen == {"cmd": "await-replicated exp-fu-a 55123", "timeout": main.AD_REPLICATION_SSH_TIMEOUT_SEC}
