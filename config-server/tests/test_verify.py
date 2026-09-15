"""접근 검증 probe(VERIFY_MODE=full) — 판정 규칙 단위 테스트.
저수준 관찰 도구(_sh, _tcp_check)만 대역으로 바꾸고 판정 로직은 실제로 돈다."""
import pytest

import main
from lifecycle_steps import verify
from main import Phase, StepFailed


@pytest.fixture
def probe_env(monkeypatch, logs):
    """관찰 대역: sh[명령 부분 문자열] = (출력, rc), tcp[(host, port)] = (연결, 배너)."""
    env = {"sh": {}, "tcp": {}}

    def fake_sh(pod, cmd):
        for key, val in env["sh"].items():
            if key in cmd:
                return val
        raise AssertionError(f"대역에 없는 명령: {cmd}")

    monkeypatch.setattr(verify, "_sh", fake_sh)
    monkeypatch.setattr(verify, "_tcp_check",
                        lambda host, port, expect_banner=None: env["tcp"].get((host, int(port)), (False, "no fixture")))
    monkeypatch.setattr(main, "_get_farm_node_info",
                        lambda name: {"name": name, "host": "10.0.0.8", "port": 22})
    monkeypatch.setattr(verify, "log_operation", lambda **kw: logs.append(kw))
    env["status"] = []
    monkeypatch.setattr(main, "set_pod_creation_status", lambda *a, **k: env["status"].append(a))
    env["logs"] = logs
    return env


CTX = {"request_id": "9", "username": "exp-np-001", "pod_name": "ailab-exp-np-001-x", "uid": 50000,
       "node": "farm2", "allocated_ports": [{"usage_purpose": "ssh", "external_port": 32001}]}


def test_uid_probe_passes_on_matching_uid(probe_env):
    probe_env["sh"]["id -u"] = ("50000", 0)
    verify.step_verify_uid(dict(CTX))
    assert probe_env["logs"][-1]["phase"] == Phase.SUCCESS


def test_uid_probe_fails_on_mismatch_with_evidence(probe_env):
    probe_env["sh"]["id -u"] = ("0", 0)          # root로 실행됨 — 계정 권한 아님
    with pytest.raises(StepFailed):
        verify.step_verify_uid(dict(CTX))
    last = probe_env["logs"][-1]
    assert last["phase"] == Phase.FAIL and '"expected_uid": "50000"' in last["error_detail"]


def test_uid_probe_reads_uid_from_passwd_when_account_was_reused(probe_env, monkeypatch):
    """같은 사용자의 두 번째 신청은 계정 단계가 없어 문맥에 uid가 없다(2026-09-15 full 스택 KeyError)."""
    monkeypatch.setattr(main, "read_passwd_lines",
                        lambda: ["exp-np-001:x:50007:50007:t:/home/exp-np-001:/bin/bash"])
    probe_env["sh"]["id -u"] = ("50007", 0)
    ctx = {k: v for k, v in CTX.items() if k != "uid"}
    verify.step_verify_uid(ctx)
    last = probe_env["logs"][-1]
    assert last["phase"] == Phase.SUCCESS and '"expected_uid": "50007"' in last["error_detail"]


def test_uid_probe_fails_with_evidence_when_account_missing(probe_env, monkeypatch):
    monkeypatch.setattr(main, "read_passwd_lines", lambda: [])
    ctx = {k: v for k, v in CTX.items() if k != "uid"}
    with pytest.raises(StepFailed) as ei:
        verify.step_verify_uid(ctx)
    assert "VERIFY_TOOL_FAILED" not in str(ei.value.body)
    assert "계정 대장" in probe_env["logs"][-1]["error_detail"]


def test_krb5_probe_uses_passwd_uid_when_account_was_reused(probe_env, monkeypatch):
    monkeypatch.setattr(main, "read_passwd_lines",
                        lambda: ["exp-np-001:x:50007:50007:t:/home/exp-np-001:/bin/bash"])
    probe_env["sh"]["/run/user/50007/"] = ("", 0)
    ctx = {k: v for k, v in CTX.items() if k != "uid"}
    verify.step_verify_krb5(ctx)
    assert probe_env["logs"][-1]["phase"] == Phase.SUCCESS


def test_access_probe_records_progress_for_screen(probe_env):
    probe_env["sh"]["id -u"] = ("50000", 0)
    verify.step_verify_uid(dict(CTX))
    stage, message = probe_env["status"][-1][1], probe_env["status"][-1][2]
    assert stage == "verifying" and "접근 확인 중: 계정 권한" in message and "시도 1/" in message


def test_home_io_fails_when_mount_is_local_disk(probe_env):
    """silent split: 왕복은 되는데 홈이 NFS가 아니라 노드 로컬 디스크인 경우."""
    probe_env["sh"]["df -P"] = ("ok\n/dev/sda1 100 0 100 1% /home\n50000", 0)
    with pytest.raises(StepFailed):
        verify.step_verify_home_io(dict(CTX))
    assert '"mount_src": "/dev/sda1"' in probe_env["logs"][-1]["error_detail"]


def test_home_io_passes_on_nfs_roundtrip(probe_env):
    probe_env["sh"]["df -P"] = ("ok\nnas:/volume1/share/user 100 0 100 1% /home\n50000", 0)
    verify.step_verify_home_io(dict(CTX))
    assert probe_env["logs"][-1]["phase"] == Phase.SUCCESS


def test_home_io_names_krb5_as_likely_cause_when_owner_is_nobody(probe_env):
    """실측 사례: sec=krb5 마운트에서 티켓이 없으면 소유자가 nobody(65534)로 보이고 쓰기가 거부된다.
    권한 문제로 오인하지 않도록 근거에 원인 후보를 적는다."""
    probe_env["sh"]["df -P"] = ("nas:/volume1/share/user 100 0 100 1% /home\n65534", 1)
    with pytest.raises(StepFailed):
        verify.step_verify_home_io(dict(CTX))
    detail = probe_env["logs"][-1]["error_detail"]
    assert '"owner_uid": "65534"' in detail and "krb5" in detail


def test_gpu_probe_skips_with_reason_when_not_requested(probe_env):
    ctx = dict(CTX, user_info={"gpu_nodes": []})
    verify.step_verify_gpu(ctx)
    last = probe_env["logs"][-1]
    assert last["phase"] == Phase.SUCCESS and "GPU 미신청" in last["error_detail"]


def test_gpu_probe_fails_when_fewer_visible_than_requested(probe_env):
    probe_env["sh"]["nvidia-smi"] = ("1", 0)
    ctx = dict(CTX, user_info={"gpu_nodes": [{"num_gpu": 2}]})
    with pytest.raises(StepFailed):
        verify.step_verify_gpu(ctx)


def test_gpu_probe_expects_gpu_count_of_placed_node_not_candidate_max(probe_env):
    """후보 FARM2(2)·FARM6(3) 중 FARM2에 배치됐으면 2개면 통과다(2026-09-15 full 스택에서 오판정)."""
    probe_env["sh"]["nvidia-smi"] = ("2", 0)
    ctx = dict(CTX, node="farm2", user_info={"gpu_nodes": [
        {"node_name": "FARM2", "num_gpu": 2}, {"node_name": "FARM6", "num_gpu": 3}]})
    verify.step_verify_gpu(ctx)
    assert probe_env["logs"][-1]["phase"] == Phase.SUCCESS
    assert '"requested": 2' in probe_env["logs"][-1]["error_detail"]


def test_gpu_probe_fails_when_placed_node_gpus_not_visible(probe_env):
    probe_env["sh"]["nvidia-smi"] = ("2", 0)
    ctx = dict(CTX, node="farm6", user_info={"gpu_nodes": [
        {"node_name": "FARM2", "num_gpu": 2}, {"node_name": "FARM6", "num_gpu": 3}]})
    with pytest.raises(StepFailed):
        verify.step_verify_gpu(ctx)


def test_endpoint_probe_requires_ssh_banner(probe_env):
    probe_env["tcp"][("10.0.0.8", 32001)] = (True, "HTTP/1.1 400")   # 열려 있지만 SSH가 아님
    with pytest.raises(StepFailed):
        verify.step_verify_endpoint(dict(CTX))
    probe_env["tcp"][("10.0.0.8", 32001)] = (True, "SSH-2.0-OpenSSH_8.9")
    verify.step_verify_endpoint(dict(CTX))
    assert probe_env["logs"][-1]["phase"] == Phase.SUCCESS


def test_probe_tool_error_records_unknown_and_raises(probe_env, monkeypatch):
    import subprocess
    def boom(pod, cmd):
        raise subprocess.TimeoutExpired("exec", 20)
    monkeypatch.setattr(verify, "_sh", boom)
    with pytest.raises(StepFailed) as ei:
        verify.step_verify_uid(dict(CTX))
    assert ei.value.unknown                      # 실행 여부 불명으로 분류
    assert probe_env["logs"][-1]["error_code"] == "VERIFY_TOOL_FAILED"


# ---------- 회수 차단 판정 ----------

class _FakeV1:
    def __init__(self, pod_absent=True, services=0):
        self._absent, self._services = pod_absent, services

    def read_namespaced_pod(self, name, ns):
        from kubernetes.client.exceptions import ApiException
        if self._absent:
            raise ApiException(status=404, reason="Not Found")
        return object()

    def list_namespaced_service(self, ns, label_selector=None):
        import types
        return types.SimpleNamespace(items=[object()] * self._services)


@pytest.fixture
def revoke_env(probe_env, monkeypatch):
    state = {"v1": _FakeV1(), "db_rows": 0}
    ctx = main.app.app_context(); ctx.push()
    monkeypatch.setitem(main.app.config, "NAMESPACE", "test-ns")

    class Cur:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, params=()): pass
        def fetchone(self): return (state["db_rows"],)
        def fetchall(self): return []

    class Conn:
        def cursor(self): return Cur()
        def close(self): pass

    monkeypatch.setattr(verify, "get_db_connection", lambda: Conn())
    monkeypatch.setattr(verify, "load_k8s", lambda: None)
    monkeypatch.setattr(verify.client, "CoreV1Api", lambda: state["v1"])
    state.update(probe_env)
    yield state
    ctx.pop()


RCTX = {"request_id": "9", "username": "exp-np-001", "pod_name": "ailab-exp-np-001-x",
        "verify_ports": [32001], "verify_node": "farm2"}


def test_revoked_pass_needs_absence_AND_live_path_AND_closed_ports(revoke_env):
    revoke_env["tcp"][("10.0.0.8", 22)] = (True, "")        # 경로 생존 증명
    revoke_env["tcp"][("10.0.0.8", 32001)] = (False, "refused")
    verify.step_verify_revoked(dict(RCTX))
    assert revoke_env["logs"][-1]["phase"] == Phase.SUCCESS


def test_revoked_connection_failure_alone_is_not_evidence(revoke_env):
    """경로 생존이 증명되지 않으면 포트가 안 열려도 차단 성공으로 기록하지 않는다(논문 §5.1)."""
    revoke_env["tcp"][("10.0.0.8", 22)] = (False, "network down")
    revoke_env["tcp"][("10.0.0.8", 32001)] = (False, "refused")
    with pytest.raises(StepFailed):
        verify.step_verify_revoked(dict(RCTX))
    assert "판정 불능" in revoke_env["logs"][-1]["error_detail"]


def test_revoked_fails_when_port_still_open(revoke_env):
    revoke_env["tcp"][("10.0.0.8", 22)] = (True, "")
    revoke_env["tcp"][("10.0.0.8", 32001)] = (True, "SSH-2.0")
    with pytest.raises(StepFailed):
        verify.step_verify_revoked(dict(RCTX))
    assert '"open_ports": [32001]' in revoke_env["logs"][-1]["error_detail"]


def test_revoked_fails_when_pod_still_exists(revoke_env):
    revoke_env["v1"] = _FakeV1(pod_absent=False)
    revoke_env["tcp"][("10.0.0.8", 22)] = (True, "")
    revoke_env["tcp"][("10.0.0.8", 32001)] = (False, "refused")
    with pytest.raises(StepFailed):
        verify.step_verify_revoked(dict(RCTX))


# ---------- 조건 분기 ----------

def test_krb5_probe_runs_before_home_io(monkeypatch):
    """홈은 sec=krb5로 마운트되므로 티켓이 원인, 홈 쓰기 실패가 증상이다 — 원인을 먼저 본다."""
    names = [s.__name__ for s in verify.VERIFY_ACCESS_STEPS]
    assert names.index("step_verify_krb5") < names.index("step_verify_home_io")


def test_full_mode_appends_verify_steps(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_MODE", "full")
    steps = main._job_steps("provision", {"username": "u"})
    assert steps[-5:] == verify.VERIFY_ACCESS_STEPS
    rsteps = main._job_steps("revoke", {"pod_name": "p", "delete_account": True})
    assert rsteps[0] is verify.step_capture_access_targets
    assert rsteps[-2] is verify.step_verify_revoked and rsteps[-1] is verify.step_verify_account_revoked


def test_noprobe_mode_is_unchanged(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_MODE", "noprobe")
    assert main._job_steps("provision", {"username": "u"}) == main.POD_CREATE_STEPS
    assert main._job_steps("revoke", {"pod_name": "p"}) == main.POD_DELETE_STEPS
