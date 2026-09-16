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


def _named_steps(calls, *names):
    steps = []
    for n in names:
        def step(ctx, n=n):
            calls.append(n)
        step.__name__ = n
        steps.append(step)
    return steps


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
    assert r.get_json()["request_id"] == "7" and r.get_json()["status"] == "accepted"
    job = store[("PROVISION", "7")]["job"]
    assert job["account"]["passwd_hash"].startswith("$6$")
    assert job["account"]["pg_name"] == "exp-np-001"
    assert "s3cret" not in json.dumps(job) and PW not in json.dumps(job)
    assert len(logs) == 1 and logs[0]["action"] == Action.PROVISION and logs[0]["phase"] == Phase.START
    assert logs[0]["start_job"] is True and logs[0]["raise_errors"] is True
    target = json.loads(logs[0]["target_state"])
    assert target["username"] == "exp-np-001" and target["account"]["pg_name"] == "exp-np-001"
    assert "passwd_hash" not in json.dumps(target)
    assert "job_id" in r.get_json()


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
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: _named_steps(calls, "a", "b", "c"))
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001")

    assert calls == ["a", "b", "c"]
    assert logs[-1]["action"] == Action.PROVISION and logs[-1]["phase"] == Phase.SUCCESS
    assert ("PROVISION", "9") not in store


def test_run_job_retries_5xx_then_degrades(logs, store, monkeypatch):
    calls = []
    failing = _raise(StepFailed(infra_error("CREATE_POD", "POD_CREATE_FAILED", "quota"), 500))
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [
        lambda ctx: calls.append("a"), failing, lambda ctx: calls.append("c")])
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001")

    assert calls == ["a"]                                     # 실패한 단계 뒤로는 가지 않는다
    retries = [l for l in logs if l["phase"] == Phase.RETRY]
    assert [r["attempt"] for r in retries] == [2, 3]          # 재시도 기록 + 시도 번호
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "DEGRADED"
    assert "POD_CREATE_FAILED" in logs[-1]["error_detail"]
    assert ("PROVISION", "9") not in store


def test_failed_provision_closes_progress_status(logs, store, pod_status, monkeypatch):
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [
        _raise(StepFailed({"error": "user already exists"}, 409))])
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001")

    assert pod_status[-1] == ("9", "failed", "user already exists")
    assert not [l for l in logs if l["phase"] == Phase.RETRY]   # 4xx는 다시 돌려도 같다 — 재시도 없음


def test_unknown_without_observer_hands_off_as_degraded(logs, store, monkeypatch):
    """실행 여부를 알 수 없고 관찰자도 재실행 안전 표시도 없는 단계 — 임의로 재실행하지 않고 이관한다."""
    step = _raise(StepFailed({"error": "NAS_SSH_FAILED"}, 500, cause=subprocess.TimeoutExpired("ssh", 60)))
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [step])
    _queued(store, "REVOKE", "4", {"username": "exp-np-001", "delete_account": True})

    main.run_job("revoke", "4", "exp-np-001")

    assert logs[-1]["action"] == Action.REVOKE and logs[-1]["phase"] == Phase.UNKNOWN
    assert logs[-1]["error_code"] == "DEGRADED"
    assert not [l for l in logs if l["phase"] == Phase.RETRY]


def test_run_job_retries_unexpected_errors_then_degrades(logs, store, monkeypatch):
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [_raise(RuntimeError("boom"))])
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001")

    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "DEGRADED"
    assert "boom" in logs[-1]["error_detail"]


def test_run_job_without_input_fails(logs, store):
    main.run_job("provision", "404", "exp-np-001")
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "JOB_INPUT_MISSING"


def test_finished_job_picked_up_again_is_skipped(logs, store, monkeypatch):
    """제어기가 미완료 목록을 읽은 뒤 그 작업이 끝나면 같은 작업을 한 번 더 집는다. 입력도 lease도
    이미 지워져 있지만, 끝 행이 있으므로 실패 행을 남기지 않고 물러나야 한다."""
    monkeypatch.setattr(main, "job_end_exists", lambda a, r, j: True)

    main.run_job("migrate", "17", "exp-fu-001", job_id=1925)

    assert logs == []


def test_restart_resumes_from_last_done_step(logs, store, lease_env, monkeypatch):
    """제어기 재시작: lease를 인수하고, 저널에 남은 단계는 건너뛰고 이어서 실행한다."""
    calls = []
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: _named_steps(calls, "s_a", "s_b", "s_c"))
    store[("PROVISION", "9")] = {"state": "running", "job": {"username": "exp-np-001"}}
    lease_env[11] = {"owner": "dead-controller", "alive": False,
                     "done": ["s_a"], "ctx": {"pod_name": "ailab-exp-np-001-old1"}}

    main.run_job("provision", "9", "exp-np-001", job_id=11)

    assert calls == ["s_b", "s_c"]                            # s_a는 다시 돌리지 않는다
    assert logs[-1]["phase"] == Phase.SUCCESS
    assert ("PROVISION", "9") not in store and 11 not in lease_env


def test_claim_denied_by_live_owner_leaves_job_untouched(logs, store, lease_env, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: _named_steps(calls, "s_a"))
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})
    lease_env[11] = {"owner": "other-controller", "alive": True, "done": [], "ctx": {}}

    main.run_job("provision", "9", "exp-np-001", job_id=11)

    assert calls == [] and logs == []                         # 남의 작업 — 손대지 않는다
    assert store[("PROVISION", "9")]["state"] == "queued"


def test_lease_lost_mid_job_stops_without_finishing(logs, store, lease_env, monkeypatch):
    """실행 중 소유권이 넘어가면(지연된 옛 제어기) 끝 행을 쓰지 않고 물러난다 — 새 소유자가 기록한다."""
    calls = []

    def steal_after_first(ctx):
        calls.append("s_a")
        lease_env[11]["owner"] = "new-controller"
    steal_after_first.__name__ = "s_a"
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [steal_after_first])
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001", job_id=11)

    assert calls == ["s_a"]
    assert not [l for l in logs if l["phase"] in (Phase.SUCCESS, Phase.FAIL, Phase.UNKNOWN)]
    assert store[("PROVISION", "9")]["state"] == "running"    # 입력도 남긴다


def test_unknown_with_observer_confirming_effect_continues(logs, store, monkeypatch):
    """observe-before-retry: UNKNOWN이어도 실제 효과가 확인되면 재실행 없이 다음 단계로 간다."""
    calls = []
    flaky = _raise(StepFailed({"error": "TIMEOUT"}, 500, cause=subprocess.TimeoutExpired("kubectl", 5)))
    flaky.__name__ = "s_make"
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [flaky] + _named_steps(calls, "s_next"))
    monkeypatch.setitem(main.STEP_OBSERVERS, "s_make", lambda ctx: True)
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001")

    assert calls == ["s_next"]
    assert logs[-1]["phase"] == Phase.SUCCESS
    assert not [l for l in logs if l["phase"] == Phase.RETRY]


def test_unknown_with_observer_denying_effect_retries(logs, store, monkeypatch):
    """관찰 결과 효과가 없으면 같은 단계를 다시 실행하고 RETRY 행을 남긴다."""
    tries = []

    def flaky(ctx):
        tries.append(1)
        if len(tries) == 1:
            raise StepFailed({"error": "TIMEOUT"}, 500, cause=subprocess.TimeoutExpired("kubectl", 5))
    flaky.__name__ = "s_make"
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [flaky])
    monkeypatch.setitem(main.STEP_OBSERVERS, "s_make", lambda ctx: False)
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})

    main.run_job("provision", "9", "exp-np-001")

    assert len(tries) == 2
    retries = [l for l in logs if l["phase"] == Phase.RETRY]
    assert len(retries) == 1 and retries[0]["attempt"] == 2 and retries[0]["resource_type"] == "s_make"
    assert logs[-1]["phase"] == Phase.SUCCESS


# ---------- baseline 모드 ----------

@pytest.fixture
def baseline(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_MODE", "baseline")


@pytest.fixture
def account_revoke(monkeypatch):
    """계정 되돌리기 단계 대역. 받은 ctx를 모으고, node_name이 없으면 운영과 같이 보류(409)한다."""
    seen = []

    def check(ctx):
        seen.append(dict(ctx))
        if not ctx.get("node_name"):
            raise StepFailed({"error": "ACCOUNT_NODE_UNKNOWN"}, 409)
    monkeypatch.setattr(main, "step_check_account_revocable", check)
    monkeypatch.setattr(main, "step_delete_account", lambda ctx: seen.append("deleted"))
    monkeypatch.setattr(main, "step_remove_krb5", lambda ctx: seen.append("krb5_removed"))
    return seen


def test_baseline_fails_once_without_retry_and_compensates(baseline, logs, store, account_revoke, monkeypatch):
    calls = []
    failing = _raise(StepFailed(infra_error("CREATE_POD", "POD_CREATE_FAILED", "quota"), 500))
    failing.__name__ = "step_create_pod_k8s"
    steps = _named_steps(calls, "step_create_account", "step_create_krb5_principal") + [failing]
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: steps)
    _queued(store, "PROVISION", "9", {"username": "exp-bl-001", "account": {"passwd_hash": "x"}})

    def select_node(ctx):
        ctx["node"] = "farm1"
    steps[1] = _with_side_effect(steps[1], select_node)

    main.run_job("provision", "9", "exp-bl-001")

    assert not [l for l in logs if l["phase"] == Phase.RETRY]             # 재시도 없음
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "POD_CREATE_FAILED"
    assert "DEGRADED" not in json.dumps(logs[-1])
    assert json.loads(logs[-1]["error_detail"])["compensation"] == "account_removed"
    assert "deleted" in account_revoke and "krb5_removed" in account_revoke


def _with_side_effect(step, effect):
    def wrapped(ctx):
        step(ctx)
        effect(ctx)
    wrapped.__name__ = step.__name__
    return wrapped


def test_baseline_unknown_is_fail_without_observing(baseline, logs, store, monkeypatch):
    """운영 admin_be는 타임아웃을 실패로 보고 뒷정리한다. baseline도 결과를 조회하지 않고 FAIL로 끝낸다."""
    observed = []
    flaky = _raise(StepFailed({"error": "TIMEOUT"}, 500, cause=subprocess.TimeoutExpired("kubectl", 5)))
    flaky.__name__ = "s_make"
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [flaky])
    monkeypatch.setitem(main.STEP_OBSERVERS, "s_make", lambda ctx: observed.append(1) or True)
    _queued(store, "PROVISION", "9", {"username": "exp-bl-001"})

    main.run_job("provision", "9", "exp-bl-001")

    assert observed == []
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "TIMEOUT"
    assert json.loads(logs[-1]["error_detail"])["unknown"] is True


def test_baseline_unexpected_error_fails_once(baseline, logs, store, monkeypatch):
    tries = []

    def boom(ctx):
        tries.append(1)
        raise RuntimeError("boom")
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [boom])
    _queued(store, "REVOKE", "4", {"username": "exp-bl-001", "pod_name": "ailab-exp-bl-001-a"})

    main.run_job("revoke", "4", "exp-bl-001")

    assert tries == [1]
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "UNEXPECTED_ERROR"


def test_baseline_restart_does_not_resume_and_holds_account(baseline, logs, store, lease_env,
                                                            account_revoke, monkeypatch):
    """운영에서 config-server가 처리 중 죽으면 admin_be는 노드를 모른 채 뒷정리해 계정 삭제를 보류한다."""
    calls = []
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: _named_steps(
        calls, "step_create_account", "step_create_krb5_principal", "step_create_pod_k8s"))
    store[("PROVISION", "9")] = {"state": "running",
                                 "job": {"username": "exp-bl-001", "account": {"passwd_hash": "x"}}}
    lease_env[11] = {"owner": "dead-controller", "alive": False,
                     "done": ["step_create_account", "step_create_krb5_principal"],
                     "ctx": {"node": "farm1", "pod_name": "ailab-exp-bl-001-old1"}}

    main.run_job("provision", "9", "exp-bl-001", job_id=11)

    assert calls == []                                                    # 이어서 실행하지 않는다
    assert logs[-1]["phase"] == Phase.FAIL and logs[-1]["error_code"] == "INTERRUPTED"
    detail = json.loads(logs[-1]["error_detail"])
    assert detail["interrupted_after"] == "step_create_krb5_principal"
    assert detail["compensation"] == "held:ACCOUNT_NODE_UNKNOWN"
    assert account_revoke[0].get("node_name") is None
    assert ("PROVISION", "9") not in store and 11 not in lease_env


def test_baseline_first_run_of_claimed_job_executes_normally(baseline, logs, store, lease_env, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: _named_steps(calls, "s_a", "s_b"))
    _queued(store, "PROVISION", "9", {"username": "exp-bl-001"})

    main.run_job("provision", "9", "exp-bl-001", job_id=12)

    assert calls == ["s_a", "s_b"]
    assert logs[-1]["phase"] == Phase.SUCCESS


def test_baseline_has_no_access_probes(baseline):
    job = {"username": "u", "account": {"passwd_hash": "x"}}
    assert main._job_steps("provision", job) == main.ACCOUNT_CREATE_STEPS + main.POD_CREATE_STEPS


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


def test_run_job_tags_every_row_with_job_number(logs, store, monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_job_steps", lambda kind, job: [lambda ctx: seen.append(main.current_job_id.get())])
    _queued(store, "PROVISION", "9", {"username": "exp-np-001"})
    main.run_job("provision", "9", "exp-np-001", job_id=77)
    assert seen == [77]
    assert main.current_job_id.get() is None                  # 작업이 끝나면 비워진다


def test_log_operation_writes_job_number_and_returns_row_id(monkeypatch):
    from adapters import operation_log
    executed = []

    class Cur:
        lastrowid = 501

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, sql, params):
            executed.append((sql, params))

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            executed.append(("COMMIT", None))

        def close(self):
            pass
    monkeypatch.setattr(operation_log, "get_log_db_connection", lambda: Conn())
    with main.app.app_context():
        rid = operation_log.log_operation(request_id=9, username="u", action=Action.PROVISION,
                                          phase=Phase.START, target_state="{}", start_job=True)
        token = operation_log.current_job_id.set(501)
        operation_log.log_operation(request_id=9, username="u", action=Action.CREATE_ACCOUNT, phase=Phase.START)
        operation_log.current_job_id.reset(token)
    assert rid == 501
    insert, update = executed[0], executed[1]
    assert insert[1][0] is None and insert[1][-1] == "{}"       # 시작 행: job_id는 곧바로 자기 id로 채움
    assert update == ("UPDATE operation_log SET job_id=%s WHERE id=%s", (501, 501))
    step_insert = [e for e in executed if e[0].startswith("INSERT")][1]
    assert step_insert[1][0] == 501                             # 실행 중 작업의 단계 행에 작업 번호
