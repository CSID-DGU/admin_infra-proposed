"""measure_ports 를 가짜 스택 위에서 확인한다. system.stack_sql 과 system.stack_http 를 바꿔 끼운다."""
import base64
import hashlib
import hmac
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure_ports as mp  # noqa: E402
import system  # noqa: E402
import trial  # noqa: E402
import trial_runner  # noqa: E402

HOST, NS = "farm1", "ailab-full"
PASSWORD = "p'w\\\"d-평문"


class FakeStack:
    """SQL 문장과 HTTP 호출을 기록하고, 문장 모양에 따라 정한 결과를 돌려준다."""

    def __init__(self):
        self.statements = []
        self.http = []
        self.rc = 0
        self.http_status = {}
        self.request_status = "PENDING"
        self.manifest = {}

    def sql(self, host, namespace, database, statement):
        self.statements.append((database, statement))
        if self.rc:
            return {"rc": self.rc, "columns": [], "rows": [], "stderr": "ERROR 1146"}
        s = statement
        if s.startswith("SELECT user_id FROM users"):
            return self._rows(["user_id"], [["7"]])
        if "FROM resource_groups" in s:
            return self._rows(["rsgroup_id"], [["3"]])
        if "FROM container_image" in s:
            return self._rows(["image_id"], [["5"]])
        if s.startswith("SELECT status FROM requests"):
            return self._rows(["status"], [[self.request_status]])
        if s.startswith("INSERT INTO trial_manifest"):
            tid = re.search(r"VALUES \('([^']+)'", s).group(1)
            self.manifest[tid] = {"request_id": None, "started_at": "2026-09-29 10:00:00.000", "ended_at": None}
        elif s.startswith("UPDATE trial_manifest SET request_id"):
            rid, tid = re.search(r"request_id = '([^']+)' WHERE trial_id = '([^']+)'", s).groups()
            self.manifest[tid]["request_id"] = rid
        elif s.startswith("UPDATE trial_manifest SET ended_at"):
            tid = re.search(r"trial_id = '([^']+)'", s).group(1)
            self.manifest[tid]["ended_at"] = "2026-09-29 10:10:00.000"
        elif s.startswith("SELECT request_id, started_at, ended_at FROM trial_manifest"):
            tid = re.search(r"trial_id = '([^']+)'", s).group(1)
            m = self.manifest.get(tid)
            return self._rows(["request_id", "started_at", "ended_at"],
                              [[m["request_id"], m["started_at"], m["ended_at"]]] if m else [])
        elif s.startswith("SELECT * FROM operation_log"):
            return self._rows(["id", "action", "phase"], [["1", "PROVISION", "START"]])
        return self._rows([], [])

    @staticmethod
    def _rows(columns, rows):
        return {"rc": 0, "columns": columns, "rows": rows, "stderr": ""}

    def http_call(self, host, namespace, method, path, *, token, body):
        self.http.append((method, path, token, body))
        if method == "POST" and path == "/api/requests":
            return {"rc": 0, "status": self.http_status.get(path, 201),
                    "body": {"data": {"requestId": 812}}, "stderr": ""}
        return {"rc": 0, "status": self.http_status.get(path, 202), "body": {}, "stderr": ""}


@pytest.fixture
def stack(monkeypatch):
    fake = FakeStack()
    monkeypatch.setattr(system, "stack_sql", fake.sql)
    monkeypatch.setattr(system, "stack_http", fake.http_call)
    monkeypatch.setattr(system, "stack_kube", lambda *a, **k: {"rc": 1, "stdout": "", "stderr": "fake"})
    return fake


def test_execute_escapes_quotes_and_backslashes(stack):
    conn = mp.StackSql(HOST, NS, "web_admin")
    with conn.cursor() as cur:
        cur.execute("SELECT %s, %s, %s", ("a'b\\c", 3, None))
    assert stack.statements[-1] == ("web_admin", "SELECT 'a\\'b\\\\c', 3, NULL")


@pytest.mark.parametrize("bad", [b"x", True, [1], {"a": 1}])
def test_execute_rejects_other_types(stack, bad):
    with pytest.raises(TypeError):
        mp.StackSql(HOST, NS, "web_admin").query("SELECT %s", (bad,))
    assert stack.statements == []


def test_trial_functions_run_on_stack_sql(stack):
    conn = mp.StackSql(HOST, NS, "operation_state_db")
    trial.open_trial(conn, trial_id="t1", method="full", server_group="A", operation="CREATE",
                     horizon_sec=600, repetition=1, revisions={"a": "b"})
    trial.bind_request(conn, "t1", "812")
    trial.close_trial(conn, "t1")
    events = trial.events_of(conn, "t1")
    assert events == [{"id": "1", "action": "PROVISION", "phase": "START"}]
    assert "created_at <= '2026-09-29 10:10:00.000'" in stack.statements[-1][1]


def test_remote_failure_raises(stack):
    stack.rc = 1
    with pytest.raises(mp.StackSqlError):
        trial.events_of(mp.StackSql(HOST, NS, "operation_state_db"), "t1")


def _decode(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def test_token_has_access_type_and_valid_signature():
    token = mp.BeClient(HOST, NS, "s3cret", clock=lambda: 1000).token(7)
    head, claims, sig = token.split(".")
    assert _decode(head)["alg"] == "HS256"
    assert _decode(claims) == {"sub": "7", "iat": 1000, "exp": 1300, "token_type": "access"}
    expected = base64.urlsafe_b64encode(
        hmac.new(b"s3cret", f"{head}.{claims}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert sig == expected


def test_create_user_keeps_plaintext_out_of_sql(stack):
    uid = mp.create_user(mp.StackSql(HOST, NS, "web_admin"), username="fullm01", email="u@example.com",
                         password=PASSWORD, role="USER")
    assert uid == 7
    insert = stack.statements[0][1]
    assert insert.startswith("INSERT INTO users")
    assert re.search(r"'\$6\$[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}'", insert)
    for _, statement in stack.statements:
        assert PASSWORD not in statement
        assert "평문" not in statement


def _ports(stack, clock_box):
    def sleep(sec):
        clock_box[0] += sec
        # 첫 잠자기 뒤에 생성이 끝나고, 회수 제출 뒤에 삭제가 끝난 것처럼 흉내 낸다.
        if any(p.endswith("/ubuntu-account") for _, p, _, _ in stack.http):
            stack.request_status = "DELETED"
        else:
            stack.request_status = "FULFILLED"

    be = mp.BeClient(HOST, NS, "s3cret", clock=lambda: 1000)
    return mp.real_ports(be=be, web_sql=mp.StackSql(HOST, NS, "web_admin"), user_id=7, admin_id=1,
                         prefix="exp-fu-", username="exp-fu-m01", expires_at="2026-10-02T00:00:00", sleep=sleep, clock=lambda: clock_box[0],
                         poll_sec=10)


def test_run_pair_on_fake_stack(stack):
    saved = []
    created, revoked = trial_runner.run_pair(
        mp.StackSql(HOST, NS, "operation_state_db"), create_trial_id="full-r-1-c",
        revoke_trial_id="full-r-1-r", method="full", server_group="A", horizon_sec=30,
        repetition=1, revisions={}, username="fullm01", save=saved.append,
        **_ports(stack, [0.0]))
    assert saved == [created, revoked]
    assert created["request_id"] == revoked["request_id"] == "812"
    assert created["system_declaration"]["value"] == "FULFILLED"
    assert revoked["system_declaration"]["value"] == "DELETED"
    for rec in (created, revoked):
        assert rec["independent_verdict"]["at_declaration"]["verdict"] == "UNKNOWN"
        assert rec["independent_verdict"]["at_horizon"]["verdict"] == "UNKNOWN"
        assert rec["environment"]["verdict"] == "UNKNOWN"
    calls = [(m, p) for m, p, _, _ in stack.http]
    assert calls == [("POST", "/api/requests"), ("POST", "/api/admin/requests/812/approval"),
                     ("DELETE", "/api/admin/users/7/ubuntu-account")]


def test_rejected_submit_raises(stack):
    stack.http_status["/api/requests"] = 400
    ports = _ports(stack, [0.0])
    with pytest.raises(mp.MeasureStepFailed) as e:
        ports["create_submit"]()
    assert e.value.status == 400
