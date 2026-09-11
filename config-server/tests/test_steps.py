"""v2.0-①: 단계 분리 후에도 동기 엔드포인트의 응답이 그대로인지, 결과 불명(UNKNOWN) 구분."""
import subprocess

import requests
import urllib3

import main
from main import Phase, StepFailed


def _raise(exc):
    def step(ctx):
        raise exc
    return step


# ---------- 결과 불명(UNKNOWN) 구분 ----------

def test_read_timeouts_are_unknown_and_connection_failures_are_fail():
    assert main._is_unknown_result(requests.exceptions.ReadTimeout())
    assert main._is_unknown_result(subprocess.TimeoutExpired("ssh", 60))
    retry = urllib3.exceptions.MaxRetryError(None, "/", reason=urllib3.exceptions.ReadTimeoutError(None, "/", "t"))
    assert main._is_unknown_result(retry)

    assert not main._is_unknown_result(requests.exceptions.ConnectTimeout())
    assert not main._is_unknown_result(requests.exceptions.ConnectionError())
    assert not main._is_unknown_result(RuntimeError("farm SSH 실패"))
    assert not main._is_unknown_result(None)

    assert main._fail_phase(requests.exceptions.ReadTimeout()) == Phase.UNKNOWN
    assert main._fail_phase(RuntimeError()) == Phase.FAIL


def test_wrapped_timeout_is_unknown():
    try:
        try:
            raise subprocess.TimeoutExpired("ssh", 60)
        except subprocess.TimeoutExpired as e:
            raise main.PodSpecBuildError("krb5 deploy failed") from e
    except main.PodSpecBuildError as wrapped:
        assert main._is_unknown_result(wrapped)
        assert StepFailed({"error": "X"}, 500, cause=wrapped).unknown
    assert not StepFailed({"error": "X"}, 500).unknown


# ---------- 단계 순서 ----------

def test_step_order_matches_current_flow():
    assert [s.__name__ for s in main.ACCOUNT_CREATE_STEPS] == [
        "step_create_account", "step_create_home", "step_create_krb5_principal"]
    assert [s.__name__ for s in main.POD_CREATE_STEPS] == [
        "step_fetch_user_config", "step_prepare_pod", "step_select_node", "step_build_pod_spec",
        "step_create_pod_k8s", "step_wait_ready", "step_create_services"]
    assert [s.__name__ for s in main.POD_DELETE_STEPS] == [
        "step_delete_services", "step_release_nodeports", "step_delete_pod_k8s", "step_cleanup_pod_node_krb5"]
    assert [s.__name__ for s in main.ACCOUNT_DELETE_STEPS] == [
        "step_delete_account", "step_delete_home", "step_remove_krb5"]


# ---------- 동기 엔드포인트는 단계 결과를 기존 응답 그대로 돌려준다 ----------

def test_create_pod_returns_step_failure_response(api, pod_status, monkeypatch):
    monkeypatch.setattr(main, "POD_CREATE_STEPS", [_raise(StepFailed({"error": "POD_ALREADY_EXISTS"}, 409))])
    r = api.post("/create-pod", json={"username": "u", "request_id": "1"})
    assert r.status_code == 409 and r.get_json() == {"error": "POD_ALREADY_EXISTS"}


def test_create_pod_success_body(api, pod_status, monkeypatch):
    def done(ctx):
        ctx.update(node="farm2", pod_name="ailab-u-1", allocated_ports=[{"internal_port": 22}])
    monkeypatch.setattr(main, "POD_CREATE_STEPS", [done])
    r = api.post("/create-pod", json={"username": "u", "request_id": "1"})
    assert r.status_code == 201
    assert r.get_json() == {"status": "created", "node": "farm2", "pod_name": "ailab-u-1",
                            "ports": [{"internal_port": 22}]}


def test_create_pod_unexpected_error_is_500_and_marks_failed(api, pod_status, monkeypatch):
    monkeypatch.setattr(main, "POD_CREATE_STEPS", [_raise(RuntimeError("boom"))])
    r = api.post("/create-pod", json={"username": "u", "request_id": "1"})
    assert r.status_code == 500 and r.get_json()["error"] == "CREATE_POD_FAILED"
    assert pod_status[-1] == ("1", "failed", "예기치 않은 오류")


def test_create_pod_validation_unchanged(api, pod_status):
    assert api.post("/create-pod", json={"request_id": "1"}).status_code == 400
    assert api.post("/create-pod", json={"username": "u"}).status_code == 400


def test_delete_pod_already_absent_body(api, monkeypatch):
    def absent(ctx):
        ctx["rollback"]["podDeleted"] = True
        ctx["already_absent"] = True
    monkeypatch.setattr(main, "POD_DELETE_STEPS", [absent])
    r = api.post("/delete-pod", json={"pod_name": "ailab-u-abc", "request_id": "3"})
    assert r.status_code == 200
    assert r.get_json() == {"status": "deleted", "pod_name": "ailab-u-abc", "already_absent": True,
                            "progress": {"servicesDeleted": False, "nodeportsReleased": False,
                                         "podDeleteRequested": False, "podDeleted": True}}


def test_delete_pod_passes_username_from_pod_name(api, monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "POD_DELETE_STEPS", [lambda ctx: seen.update(ctx)])
    assert api.post("/delete-pod", json={"pod_name": "ailab-exp-np-001-7f3a9c21", "request_id": "3"}).status_code == 200
    assert seen["username"] == "exp-np-001" and seen["request_id"] == "3"


def test_create_user_passes_plaintext_to_account_step(api, monkeypatch):
    seen = {}

    def done(ctx):
        seen.update(ctx)
        ctx.update(entry={"name": ctx["name"]}, gid=50000, added_supp=[], s_path=None)
    monkeypatch.setattr(main, "ACCOUNT_CREATE_STEPS", [done])
    r = api.put("/accounts/users", json={"name": "exp-np-001", "passwd_base64": "cHc=", "request_id": "9"})
    assert r.status_code == 201
    assert r.get_json()["group"] == {"name": "exp-np-001", "gid": 50000}
    assert seen["plaintext_pw"] == "pw" and "passwd_hash" not in seen


def test_delete_user_returns_step_failure_response(api, monkeypatch):
    monkeypatch.setattr(main, "ACCOUNT_DELETE_STEPS", [_raise(StepFailed({"error": "user not found"}, 404))])
    r = api.delete("/accounts/users/u")
    assert r.status_code == 404 and r.get_json() == {"error": "user not found"}
