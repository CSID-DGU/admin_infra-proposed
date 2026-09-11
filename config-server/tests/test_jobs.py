"""v2.0-②: 작업 등록 API와 제어기 실행 경로."""
import base64
import json
import subprocess

import pytest

import main
from error import infra_error
from main import Action, Phase, StepFailed

PW = base64.b64encode(b"s3cret-pw").decode()


def _raise(exc):
    def step(ctx):
        raise exc
    return step


@pytest.fixture
def store(monkeypatch, pod_status):
    """Redis 작업 입력 저장소 대역. 키는 (action, request_id)."""
    data = {}

    def save(action, request_id, job):
        if (action, request_id) in data:
            return False
        data[(action, request_id)] = {"state": "queued", "job": job}
        return True

    def running(action, request_id):
        data[(action, request_id)]["state"] = "running"

    monkeypatch.setattr(main, "save_job_input", save)
    monkeypatch.setattr(main, "load_job_input", lambda a, r: data.get((a, r)))
    monkeypatch.setattr(main, "mark_job_running", running)
    monkeypatch.setattr(main, "delete_job_input", lambda a, r: data.pop((a, r), None))
    return data


def _queued(store, action, request_id, job):
    store[(action, request_id)] = {"state": "queued", "job": job}


# ---------- 작업 등록 ----------

def test_provision_returns_202_and_stores_only_password_hash(api, logs, store):
    r = api.post("/operations/provision", json={
        "request_id": 7, "username": "exp-np-001", "account": {"passwd_base64": PW, "gecos": "t"}})

    assert r.status_code == 202
    assert r.get_json() == {"request_id": "7", "status": "accepted"}
    job = store[("PROVISION", "7")]["job"]
    assert job["account"]["passwd_hash"].startswith("$6$")
    assert job["account"]["pg_name"] == "exp-np-001"
    assert "s3cret" not in json.dumps(job) and PW not in json.dumps(job)
    assert logs == [dict(request_id="7", username="exp-np-001", action=Action.PROVISION,
                         phase=Phase.START, raise_errors=True)]


def test_same_job_twice_is_409(api, logs, store):
    body = {"request_id": "8", "username": "exp-np-001"}
    assert api.post("/operations/provision", json=body).status_code == 202
    r = api.post("/operations/provision", json=body)
    assert r.status_code == 409
    assert r.get_json()["error"] == "JOB_ALREADY_REGISTERED"
    assert len(logs) == 1


@pytest.mark.parametrize("path,body", [
    ("/operations/provision", {"username": "exp-np-001"}),
    ("/operations/provision", {"request_id": "1"}),
    ("/operations/provision", {"request_id": "1", "username": "exp-np-001", "account": {}}),
    ("/operations/provision", {"request_id": "1", "username": "exp-np-001", "account": {"passwd_base64": "%%"}}),
    ("/operations/revoke", {"request_id": "1", "username": "exp-np-001"}),
    ("/operations/revoke", {"request_id": "1", "pod_name": "other-pod"}),
    ("/operations/provision", {"request_id": "smoke-1", "username": "exp-np-001"}),
    ("/operations/provision", {"request_id": 0, "username": "exp-np-001"}),
    ("/operations/revoke", {"request_id": "-3", "username": "exp-np-001", "delete_account": True}),
])
def test_invalid_registration_is_400_and_not_stored(api, logs, store, path, body):
    assert api.post(path, json=body).status_code == 400
    assert store == {} and logs == []


def test_revoke_takes_username_from_pod_name(api, logs, store):
    r = api.post("/operations/revoke", json={"request_id": "5", "pod_name": "ailab-exp-np-001-7f3a9c21"})
    assert r.status_code == 202
    assert store[("REVOKE", "5")]["job"] == {"username": "exp-np-001", "pod_name": "ailab-exp-np-001-7f3a9c21",
                                             "node_name": None, "delete_account": False}


def test_prefix_guard_covers_registration(api, logs, store, monkeypatch):
    monkeypatch.setattr(main, "ACCOUNT_PREFIX", "exp-np-")
    assert api.post("/operations/provision", json={"request_id": "1", "username": "yoon6yo"}).status_code == 403
    assert api.post("/operations/revoke", json={"request_id": "1", "pod_name": "ailab-yoon6yo-abc"}).status_code == 403
    assert store == {} and logs == []


def test_registration_fails_when_start_row_cannot_be_written(api, store, monkeypatch):
    def broken(**kw):
        raise RuntimeError("log db down")
    monkeypatch.setattr(main, "log_operation", broken)
    r = api.post("/operations/provision", json={"request_id": "3", "username": "exp-np-001"})
    assert r.status_code == 503
    assert store == {}


# ---------- 제어기 실행 ----------

def test_run_job_runs_steps_in_order_and_records_success(logs, store, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [lambda ctx, n=n: calls.append(n) for n in "abc"])
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001")

    assert calls == ["a", "b", "c"]
    assert logs[-1]["action"] == Action.PROVISION and logs[-1]["phase"] == Phase.SUCCESS
    assert ("PROVISION", "9") not in store


def test_run_job_stops_at_failed_step(logs, store, monkeypatch):
    calls = []
    failing = _raise(StepFailed(infra_error("CREATE_POD", "POD_CREATE_FAILED", "quota"), 500))
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [
        lambda ctx: calls.append("a"), failing, lambda ctx: calls.append("c")])
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001")

    assert calls == ["a"]
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "POD_CREATE_FAILED"
    assert ("PROVISION", "9") not in store


def test_failed_provision_closes_progress_status(logs, store, pod_status, monkeypatch):
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [
        _raise(StepFailed({"error": "user already exists"}, 409))])
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001")

    assert pod_status[-1] == ("9", "failed", "user already exists")


def test_run_job_records_unknown_for_timeouts(logs, store, monkeypatch):
    step = _raise(StepFailed({"error": "NAS_SSH_FAILED"}, 500, cause=subprocess.TimeoutExpired("ssh", 60)))
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [step])
    _queued(store, "REVOKE", "4", {"username": "exp-np-001", "delete_account": True})

    main.run_job("revoke", "4", "exp-np-001")

    assert logs[-1]["action"] == Action.REVOKE and logs[-1]["phase"] == Phase.UNKNOWN


def test_run_job_records_unexpected_errors(logs, store, monkeypatch):
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [_raise(RuntimeError("boom"))])
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001")

    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "UNEXPECTED_ERROR"


def test_run_job_without_input_fails(logs, store):
    main.run_job("provision", "404", "exp-np-001")
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "JOB_INPUT_MISSING"


def test_jobs_interrupted_by_restart_are_failed_not_rerun(logs, store, monkeypatch):
    monkeypatch.setattr(main, "find_unfinished_jobs", lambda limit=100: [
        ("provision", "1", "u"), ("revoke", "2", "u"), ("provision", "3", "u")])
    store[("PROVISION", "1")] = {"state": "running", "job": {"username": "u"}}
    _queued(store, "REVOKE", "2", {"username": "u"})

    main.mark_interrupted_jobs()

    assert {(l["request_id"], l["error_code"]) for l in logs} == {
        ("1", "CONTROLLER_RESTARTED"), ("3", "JOB_INPUT_MISSING")}
    assert ("REVOKE", "2") in store


# ---------- 작업별 단계 목록 ----------

def test_provision_steps():
    with_account = main._job_steps("provision", {"username": "u", "account": {"passwd_hash": "x"}})
    assert with_account == main.ACCOUNT_CREATE_STEPS + main.POD_CREATE_STEPS
    assert main._job_steps("provision", {"username": "u"}) == main.POD_CREATE_STEPS


def test_revoke_keeps_home_but_sync_account_delete_still_removes_it():
    steps = main._job_steps("revoke", {"pod_name": "ailab-u-x", "delete_account": True})
    assert steps == main.POD_DELETE_STEPS + [main.step_check_account_revocable, main.step_delete_account,
                                             main.step_remove_krb5]
    assert main.step_delete_home not in steps
    assert main._job_steps("revoke", {"pod_name": "ailab-u-x"}) == main.POD_DELETE_STEPS
    assert main.step_delete_home in main.ACCOUNT_DELETE_STEPS


def test_account_ctx_uses_stored_hash():
    ctx = main._job_ctx("provision", "1", {"username": "u", "account": {
        "pg_name": "u", "supp_groups": [], "gecos": "", "passwd_hash": "$6$x"}})
    assert ctx["name"] == "u" and ctx["passwd_hash"] == "$6$x" and "plaintext_pw" not in ctx
    assert ctx["config_by_request"] is True


def test_result_row_failure_keeps_input_done_and_retries_only_the_row(store, monkeypatch):
    rows, fail = [], {"on": True}

    def log(**kw):
        if fail["on"] and kw.get("raise_errors") and kw["phase"] != Phase.START:
            raise RuntimeError("log db down")
        rows.append(kw)
    monkeypatch.setattr(main, "log_operation", log)
    monkeypatch.setattr(main, "mark_job_done",
                        lambda a, r, result: store[(a, r)].update(state="done", result=result))
    calls = []
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [lambda ctx: calls.append("step")])
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001")
    assert store[("PROVISION", "9")]["state"] == "done" and rows == []

    fail["on"] = False
    main.run_job("provision", "9", "exp-np-001")
    assert calls == ["step"]                      # 단계는 다시 돌리지 않는다
    assert rows[-1]["phase"] == Phase.SUCCESS and ("PROVISION", "9") not in store


def test_request_id_is_normalized_to_integer_text(api, logs, store):
    assert api.post("/operations/provision", json={"request_id": "007", "username": "exp-np-001"}).status_code == 202
    assert ("PROVISION", "7") in store
