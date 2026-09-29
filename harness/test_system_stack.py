"""stack 명령 네 개가 만드는 CLI 인자와 표준입력을 본다. subprocess.run 을 가로챈다."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import system  # noqa: E402


@pytest.fixture
def calls(monkeypatch):
    """subprocess.run 호출을 기록하고, 테스트가 정한 행과 종료코드를 돌려준다."""
    seen = {"rows": [{"rc": 0}], "rc": 0, "calls": []}

    def fake_run(cmd, **kw):
        seen["calls"].append((cmd, kw))
        return subprocess.CompletedProcess(cmd, seen["rc"], json.dumps(seen["rows"]), "")

    monkeypatch.setattr(system, "cli_path", lambda: Path("/x/server-state"))
    monkeypatch.setattr(system.subprocess, "run", fake_run)
    return seen


def _last(calls):
    cmd, kw = calls["calls"][-1]
    return cmd[1:], kw["input"]


def test_kube_args_and_stdin(calls):
    system.stack_kube("farm1", "ailab-full", ["create", "-f", "-"], stdin="apiVersion: v1")
    argv, stdin = _last(calls)
    assert argv == ["--format", "json", "stack", "kube", "--host", "farm1",
                    "--namespace", "ailab-full", "--", "create", "-f", "-"]
    assert stdin == "apiVersion: v1"


def test_call_without_stdin_passes_empty_input(calls):
    """부모의 표준입력을 물려주면 stack kube 가 그것을 읽으려다 멈출 수 있다."""
    system.stack_kube("farm1", "ailab-full", ["get", "pods"])
    assert _last(calls)[1] == ""
    system.stack_secret("farm1", "ailab-full", "jwt_secret")
    assert _last(calls)[1] == ""


def test_sql_statement_only_on_stdin(calls):
    stmt = "SELECT id FROM users WHERE username='x'"
    system.stack_sql("farm1", "ailab-noprobe", "web_admin", stmt)
    argv, stdin = _last(calls)
    assert argv == ["--format", "json", "stack", "sql", "--host", "farm1",
                    "--namespace", "ailab-noprobe", "--database", "web_admin"]
    assert stmt not in " ".join(argv) and stdin == stmt


def test_secret_args(calls):
    system.stack_secret("farm1", "ailab-baseline", "jwt_secret")
    assert _last(calls)[0] == ["--format", "json", "stack", "secret", "--host", "farm1",
                               "--namespace", "ailab-baseline", "--key", "jwt_secret"]


def test_http_token_and_body_only_on_stdin(calls):
    token, body = "tok-SECRET", {"password": "pw-SECRET"}
    system.stack_http("farm1", "ailab-full", "POST", "/api/requests", token=token, body=body)
    argv, stdin = _last(calls)
    assert argv == ["--format", "json", "stack", "http", "--host", "farm1",
                    "--namespace", "ailab-full", "--method", "POST", "--path", "/api/requests"]
    assert "SECRET" not in " ".join(argv)
    assert json.loads(stdin) == {"token": token, "body": body}


def test_remote_failure_is_returned_not_raised(calls):
    calls["rows"], calls["rc"] = [{"rc": 1, "stderr": "Error from server (NotFound)"}], 1
    assert system.stack_kube("farm1", "ailab-full", ["get", "cm", "x"])["rc"] == 1


@pytest.mark.parametrize("rows", [[], [{"rc": 0}, {"rc": 0}]])
def test_not_exactly_one_row_raises(calls, rows):
    calls["rows"], calls["rc"] = rows, 3
    with pytest.raises(system.SystemCallFailed):
        system.stack_secret("farm1", "ailab-full", "jwt_secret")


def test_probe_password_only_on_stdin(calls):
    system.stack_probe("local", "10.0.0.2", 10022, "exp-fu-m1", password="pw-SECRET")
    argv, stdin = _last(calls)
    assert argv == ["--format", "json", "stack", "probe", "--host", "local", "--address", "10.0.0.2",
                    "--port", "10022", "--user", "exp-fu-m1"]
    assert json.loads(stdin) == {"password": "pw-SECRET"}


def test_mutate_args(calls):
    system.stack_mutate("local", "ailab-full", "apply", template="deny-ingress-user",
                        target_user="exp-fu-m1", run="abc123")
    assert _last(calls)[0] == ["--format", "json", "stack", "mutate", "--host", "local",
                               "--namespace", "ailab-full", "--action", "apply", "--run", "abc123",
                               "--template", "deny-ingress-user", "--target-user", "exp-fu-m1"]
    system.stack_mutate("local", "ailab-full", "clear", run="all")
    assert _last(calls)[0][-2:] == ["--run", "all"]
