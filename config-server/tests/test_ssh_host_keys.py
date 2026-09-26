"""farm·AD·NAS SSH 호스트 키 확인: 배포 때 넣은 known_hosts가 있으면 확인을 켜고, 없으면 예전처럼 둔다."""
import types

import main
import utils


def test_host_key_checking_on_when_known_hosts_present(tmp_path, monkeypatch):
    kh = tmp_path / "known_hosts"
    kh.write_text("[farm2]:8082 ssh-ed25519 AAAA\n")
    monkeypatch.setattr(main, "SSH_KNOWN_HOSTS_FILE", str(kh))
    assert main._ssh_host_key_options() == ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={kh}"]


def test_host_key_checking_off_when_missing_or_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SSH_KNOWN_HOSTS_FILE", str(tmp_path / "none"))
    assert main._ssh_host_key_options() == ["-o", "StrictHostKeyChecking=no"]
    empty = tmp_path / "empty"
    empty.write_text("")
    monkeypatch.setattr(main, "SSH_KNOWN_HOSTS_FILE", str(empty))
    assert main._ssh_host_key_options() == ["-o", "StrictHostKeyChecking=no"]


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


def test_nas_client_bounds_only_the_tcp_connect(monkeypatch):
    """연결에만 한도를 건다 — banner·auth·channel 한도나 명령 실행 한도는 두지 않는다(홈 작업은 오래 걸릴 수 있다)."""
    import paramiko
    monkeypatch.setenv("NAS_SSH_HOST", "nas")
    monkeypatch.setenv("NAS_SSH_USER", "u")
    monkeypatch.setenv("NAS_SSH_KEY_PATH", "/k")
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
