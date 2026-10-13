"""farm·AD·NAS SSH 호스트 키 확인: 배포 때 넣은 known_hosts만 믿고, 없으면 접속을 거절한다."""
import types

import main
import utils


def test_host_key_checking_on_when_known_hosts_present(tmp_path, monkeypatch):
    kh = tmp_path / "known_hosts"
    kh.write_text("[farm2]:8082 ssh-ed25519 AAAA\n")
    monkeypatch.setattr(main, "SSH_KNOWN_HOSTS_FILE", str(kh))
    assert main._ssh_host_key_options() == ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={kh}"]


def test_missing_or_empty_known_hosts_refuses_instead_of_skipping_the_check(tmp_path, monkeypatch):
    """파일이 없다고 확인을 끄면, Secret 이 빠진 채 배포된 스택은 아무 서버에나 keytab·계정 명령을 보낸다."""
    monkeypatch.delenv("SSH_HOST_KEY_CHECK_DISABLED", raising=False)
    empty = tmp_path / "empty"
    empty.write_text("")
    for path in (tmp_path / "none", empty):
        monkeypatch.setattr(main, "SSH_KNOWN_HOSTS_FILE", str(path))
        assert main._ssh_host_key_options() == ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={path}"]


def test_host_key_check_can_be_switched_off_explicitly(tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_HOST_KEY_CHECK_DISABLED", "true")
    monkeypatch.setattr(main, "SSH_KNOWN_HOSTS_FILE", str(tmp_path / "none"))
    assert main._ssh_host_key_options() == ["-o", "StrictHostKeyChecking=no"]


def test_switch_does_not_override_collected_host_keys(tmp_path, monkeypatch):
    kh = tmp_path / "known_hosts"
    kh.write_text("[farm2]:8082 ssh-ed25519 AAAA\n")
    monkeypatch.setenv("SSH_HOST_KEY_CHECK_DISABLED", "true")
    monkeypatch.setattr(main, "SSH_KNOWN_HOSTS_FILE", str(kh))
    assert "StrictHostKeyChecking=yes" in main._ssh_host_key_options()


def test_farm_ssh_command_uses_host_key_options(tmp_path, monkeypatch):
    kh = tmp_path / "known_hosts"
    kh.write_text("x ssh-ed25519 AAAA\n")
    monkeypatch.setattr(main, "SSH_KNOWN_HOSTS_FILE", str(kh))
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    with main.app.app_context():
        main._farm_ssh("farm2", "8082", "list")
    assert "StrictHostKeyChecking=yes" in seen["cmd"] and "StrictHostKeyChecking=no" not in seen["cmd"]


def test_nas_client_rejects_unknown_hosts_when_known_hosts_present(tmp_path, monkeypatch):
    import paramiko
    kh = tmp_path / "known_hosts"
    kh.write_text("nas ssh-ed25519 AAAA\n")
    monkeypatch.setenv("SSH_KNOWN_HOSTS_FILE", str(kh))
    monkeypatch.setenv("NAS_SSH_HOST", "nas")
    monkeypatch.setenv("NAS_SSH_USER", "u")
    monkeypatch.setenv("NAS_SSH_KEY_PATH", "/k")
    seen = {}

    class FakeClient:
        def load_host_keys(self, path):
            seen["loaded"] = path

        def set_missing_host_key_policy(self, policy):
            seen["policy"] = type(policy).__name__

        def connect(self, **kw):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", FakeClient)
    utils._nas_ssh_client()
    assert seen == {"loaded": str(kh), "policy": "RejectPolicy"}


def _nas_policy(monkeypatch, known_hosts, disabled):
    import paramiko
    monkeypatch.setenv("SSH_KNOWN_HOSTS_FILE", str(known_hosts))
    monkeypatch.setenv("NAS_SSH_HOST", "nas")
    monkeypatch.setenv("NAS_SSH_USER", "u")
    monkeypatch.setenv("NAS_SSH_KEY_PATH", "/k")
    if disabled:
        monkeypatch.setenv("SSH_HOST_KEY_CHECK_DISABLED", "true")
    else:
        monkeypatch.delenv("SSH_HOST_KEY_CHECK_DISABLED", raising=False)
    seen = {}

    class FakeClient:
        def load_host_keys(self, path):
            seen["loaded"] = path

        def set_missing_host_key_policy(self, policy):
            seen["policy"] = type(policy).__name__

        def connect(self, **kw):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", FakeClient)
    utils._nas_ssh_client()
    return seen


def test_nas_client_rejects_every_host_when_known_hosts_is_missing(tmp_path, monkeypatch):
    assert _nas_policy(monkeypatch, tmp_path / "none", disabled=False) == {"policy": "RejectPolicy"}


def test_nas_client_accepts_unknown_hosts_only_when_switched_off(tmp_path, monkeypatch):
    assert _nas_policy(monkeypatch, tmp_path / "none", disabled=True) == {"policy": "AutoAddPolicy"}


def test_nas_connect_timeout_bounds_only_the_tcp_connect(monkeypatch):
    """connect() 에는 TCP 연결 한도만 준다 — banner·auth·channel 한도는 두지 않는다. 명령 실행 한도는
    명령마다 따로 건다(test_home_owner_guard)."""
    import paramiko
    monkeypatch.setenv("NAS_SSH_HOST", "nas")
    monkeypatch.setenv("NAS_SSH_USER", "u")
    monkeypatch.setenv("NAS_SSH_KEY_PATH", "/k")
    monkeypatch.setenv("SSH_HOST_KEY_CHECK_DISABLED", "true")
    seen = {}

    class FakeClient:
        def load_host_keys(self, path):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(paramiko, "SSHClient", FakeClient)
    utils._nas_ssh_client()
    assert seen["timeout"] == utils.NAS_SSH_CONNECT_TIMEOUT_SEC == 10
    assert not {"banner_timeout", "auth_timeout", "channel_timeout"} & seen.keys()


def test_ssh_targets_reads_nas_farm_and_ad_hosts():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[2] / "ops" / "proposed-stack" / "ssh_targets.py"
    spec = importlib.util.spec_from_file_location("ssh_targets", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values = {"nas": {"ssh": {"host": "nas", "port": 22}},
              "farm": {"ssh": {"nodes": [{"name": "farm2", "host": "h2", "port": 8082}]},
                       "adSsh": {"nodes": [{"name": "dc1", "host": "dc", "port": ""}]}}}
    assert module.targets(values) == [("dc", "22"), ("h2", "8082"), ("nas", "22")]
