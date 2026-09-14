"""system.py 계약 검증. 실제 server-state 대신 같은 자리에 가짜 실행 파일을 둔다."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import system  # noqa: E402


def _fake_cli(tmp_path, body):
    path = tmp_path / "server-state" / "bin" / "server-state"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return tmp_path


def test_audit_failure_is_data_not_exception(tmp_path, monkeypatch):
    """감사 실패는 판정 자료다. 종료코드를 그대로 돌려주고 예외로 바꾸지 않는다."""
    root = _fake_cli(tmp_path, "echo '[{\"host\":\"farm8\",\"status\":\"FAIL\"}]'\nexit 2\n")
    monkeypatch.setenv(system.ENV_VAR, str(root))
    rows, rc = system.audit("farm8", "user-access")
    assert rows == [{"host": "farm8", "status": "FAIL"}]
    assert rc == 2


def test_show_command_does_not_contact_servers(tmp_path, monkeypatch):
    """재현성 기록용. --show-command 가 인자에 실려야 한다."""
    root = _fake_cli(tmp_path, 'printf "[{\\"argv\\": \\"%s\\"}]" "$*"\n')
    monkeypatch.setenv(system.ENV_VAR, str(root))
    rows, _ = system.audit("farm8", "user-access", show_command=True)
    assert "--show-command" in rows[0]["argv"]


def test_missing_env_var_names_what_is_missing(monkeypatch):
    monkeypatch.delenv(system.ENV_VAR, raising=False)
    with pytest.raises(system.SystemCallFailed, match=system.ENV_VAR):
        system.list_hosts()


def test_non_json_output_is_reported_with_the_output(tmp_path, monkeypatch):
    """출력이 깨졌을 때 무엇이 왔는지 보여 준다. 빈 목록으로 삼키면 원인을 못 찾는다."""
    root = _fake_cli(tmp_path, "echo 'ansible: command not found' >&2\necho 'not json'\nexit 127\n")
    monkeypatch.setenv(system.ENV_VAR, str(root))
    with pytest.raises(system.SystemCallFailed, match="not json"):
        system.describe()
