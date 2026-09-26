"""단계 함수의 결과 불명(UNKNOWN) 구분, 단계 순서, 남아 있는 동기 경로(DELETE /pods/<name>)의 응답."""
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
    # AD 그룹 반영은 반드시 krb5 principal(=AD 사용자 생성) 뒤에 온다 — 그 전에는
    # 멤버로 넣을 대상이 없다(#146). 복제 대기는 AD 사용자가 생긴 바로 뒤, 노드가 조회하기 전에 온다.
    assert [s.__name__ for s in main.ACCOUNT_CREATE_STEPS] == [
        "step_create_account", "step_create_home", "step_create_krb5_principal",
        "step_await_ad_replication", "step_sync_ad_groups"]
    assert [s.__name__ for s in main.SUPP_GROUPS_ONLY_STEPS] == [
        "step_add_user_groups", "step_sync_ad_groups", "step_trigger_nas_gss_flush"]
    assert [s.__name__ for s in main.POD_CREATE_STEPS] == [
        "step_fetch_user_config", "step_prepare_pod", "step_select_node", "step_build_pod_spec",
        "step_create_pod_k8s", "step_wait_ready", "step_create_services"]
    assert [s.__name__ for s in main.POD_DELETE_STEPS] == [
        "step_delete_services", "step_release_nodeports", "step_delete_pod_k8s", "step_cleanup_pod_node_krb5"]


# ---------- DELETE /pods/<name>은 단계 결과를 응답으로 돌려준다 ----------

def test_delete_pod_already_absent_body(api, monkeypatch):
    def absent(ctx):
        ctx["rollback"]["podDeleted"] = True
        ctx["already_absent"] = True
    monkeypatch.setattr(main, "POD_DELETE_STEPS", [absent])
    r = api.delete("/pods/ailab-u-abc?request_id=3")
    assert r.status_code == 200
    assert r.get_json() == {"status": "deleted", "pod_name": "ailab-u-abc", "already_absent": True,
                            "progress": {"servicesDeleted": False, "nodeportsReleased": False,
                                         "podDeleteRequested": False, "podDeleted": True}}


def test_delete_pod_passes_username_from_pod_name(api, monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "POD_DELETE_STEPS", [lambda ctx: seen.update(ctx)])
    assert api.delete("/pods/ailab-exp-np-001-7f3a9c21?request_id=3").status_code == 200
    assert seen["username"] == "exp-np-001" and seen["request_id"] == "3"
