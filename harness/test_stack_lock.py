"""stack_lock 계약 검증. system.stack_kube 와 stack_sql 을 가짜 스택으로 바꾼다."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stack_lock  # noqa: E402
import system  # noqa: E402

HOST, NS, NOW = "farm1", "ailab-full", "2026-09-29T00:30:00+0900"


class FakeStack:
    def __init__(self):
        self.lock = None
        self.admins = []
        self.policies = []
        self.create_error = None
        self.kube_calls = []

    def kube(self, host, namespace, args, *, stdin=None):
        self.kube_calls.append(args)
        verb, kind = args[0], args[1]
        if kind == "networkpolicy":
            return {"rc": 0, "stdout": "\n".join(self.policies), "stderr": ""}
        if verb == "create":
            if self.create_error:
                return {"rc": 1, "stdout": "", "stderr": self.create_error}
            if self.lock is not None:
                return {"rc": 1, "stdout": "",
                        "stderr": 'Error from server (AlreadyExists): configmaps "experiment-lock" already exists'}
            self.lock = dict(a.split("=", 1)[1].split("=", 1) for a in args[3:])
            return {"rc": 0, "stdout": "", "stderr": ""}
        if self.lock is None:
            return {"rc": 1, "stdout": "",
                    "stderr": 'Error from server (NotFound): configmaps "experiment-lock" not found'}
        if verb == "get":
            return {"rc": 0, "stdout": json.dumps({"data": self.lock}), "stderr": ""}
        if verb == "delete":
            self.lock = None
            return {"rc": 0, "stdout": "", "stderr": ""}
        raise AssertionError(args)

    def sql(self, host, namespace, database, statement):
        assert database == "web_admin"
        return {"rc": 0, "columns": ["email"], "rows": [[e] for e in self.admins], "stderr": ""}

    def verbs(self, verb):
        return [a for a in self.kube_calls if a[0] == verb and a[1] == "configmap"]


@pytest.fixture
def stack(monkeypatch):
    s = FakeStack()
    monkeypatch.setattr(system, "stack_kube", s.kube)
    monkeypatch.setattr(system, "stack_sql", s.sql)
    return s


def test_acquire_on_empty_stack_creates_once(stack):
    stack_lock.acquire(HOST, NS, owner="measure", run_id="r1", now=NOW)
    assert len(stack.verbs("create")) == 1
    assert stack.lock == {"owner": "measure", "run": "r1", "started_at": NOW}


def test_existing_lock_is_held_with_its_owner_and_run(stack):
    stack.lock = {"owner": "e2e", "run": "r0", "started_at": "t0"}
    with pytest.raises(stack_lock.LockHeld) as e:
        stack_lock.acquire(HOST, NS, owner="measure", run_id="r1", now=NOW)
    assert (e.value.owner, e.value.run, e.value.started_at) == ("e2e", "r0", "t0")


def test_other_create_failure_is_unknown_not_held(stack):
    # 잠금이 실제로 있어도, create 가 연결 오류로 실패했다면 이미 있다고 판단할 근거가 없다.
    stack.lock = {"owner": "e2e", "run": "r0", "started_at": "t0"}
    stack.create_error = "Unable to connect to the server: dial tcp: i/o timeout"
    with pytest.raises(stack_lock.LockUnknown):
        stack_lock.acquire(HOST, NS, owner="measure", run_id="r1", now=NOW)


def test_system_call_failure_is_unknown(stack, monkeypatch):
    def boom(*a, **k):
        raise system.SystemCallFailed("no cli")
    monkeypatch.setattr(system, "stack_sql", boom)
    with pytest.raises(stack_lock.LockUnknown):
        stack_lock.acquire(HOST, NS, owner="measure", run_id="r1", now=NOW)


@pytest.mark.parametrize("trace", ["admin", "policy"])
def test_e2e_trace_blocks_before_create(stack, trace):
    if trace == "admin":
        stack.admins = ["e2eabc-admin@e2e.local"]
    else:
        stack.policies = ["networkpolicy.networking.k8s.io/e2e-fault-x"]
    with pytest.raises(stack_lock.E2ETraceFound) as e:
        stack_lock.acquire(HOST, NS, owner="measure", run_id="r1", now=NOW)
    assert e.value.evidence
    assert stack.verbs("create") == []


def test_release_deletes_only_own_run(stack):
    stack.lock = {"owner": "e2e", "run": "r0", "started_at": "t0"}
    with pytest.raises(stack_lock.LockHeld):
        stack_lock.release(HOST, NS, run_id="r1")
    assert stack.verbs("delete") == []
    stack_lock.release(HOST, NS, run_id="r0")
    assert len(stack.verbs("delete")) == 1
    assert stack.lock is None


def test_force_release_deletes_without_checking(stack):
    stack.lock = {"owner": "e2e", "run": "r0", "started_at": "t0"}
    stack_lock.force_release(HOST, NS)
    assert stack.lock is None


def test_hold_releases_even_on_exception(stack):
    with pytest.raises(RuntimeError):
        with stack_lock.hold(HOST, NS, owner="measure", run_id="r1", now=NOW):
            assert stack.lock["run"] == "r1"
            raise RuntimeError("trial failed")
    assert len(stack.verbs("delete")) == 1
    assert stack.lock is None
