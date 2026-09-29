"""real_collector 를 가짜 server-state 위에서 확인한다. system.stack_kube 와 system.stack_probe 를 바꿔 끼운다."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluator  # noqa: E402
import real_collector as rc  # noqa: E402
import system  # noqa: E402

USER = "exp-fu-mabc12301"
PASSWORD = "pw-SECRET-평문"
POD, POD_UID = f"ailab-{USER}-0a1b2c3d", "uid-pod-a"
POD_NODE, OTHER_NODE = "10.0.0.1", "10.0.0.2"


def _checks(**over):
    c = {
        "hostname": {"rc": 0, "stdout": POD + "\n", "stderr": ""},
        "uid": {"rc": 0, "stdout": "70001\n", "stderr": ""},
        "gid": {"rc": 0, "stdout": "70001\n", "stderr": ""},
        "groups": {"rc": 0, "stdout": "70001\n", "stderr": ""},
        "home_rw": {"write": True, "read": True, "remove": True},
        "home_mount": {"rc": 0, "stdout": f"nas:/volume1/share/user/{USER} nfs4\n", "stderr": ""},
        "krb": {"rc": 0, "stdout": "", "stderr": ""},
        "gpu": {"rc": 0, "stdout": "GPU 0: A100\n", "stderr": "", "lines": 1},
        "gpu_context": {"rc": 0, "stdout": "0\n", "stderr": ""},
    }
    c.update(over)
    return c


class FakeStack:
    def __init__(self):
        self.pods = f"{POD} {POD_UID} {POD_NODE} Running\n"
        self.services = f"ailab-{USER}-ssh-10022 10022\n"
        self.slices = f"{POD_UID}\n"
        self.nodes = f"farm1 {POD_NODE}\nfarm2 {OTHER_NODE}\n"
        self.fail = set()
        self.probe = {"connect": "ok", "error": None, "checks": _checks()}
        self.probe_calls = []
        self.kube_calls = 0

    def kube(self, host, namespace, args, *, stdin=None):
        self.kube_calls += 1
        kind = args[1]
        if kind in self.fail:
            return {"rc": 1, "stdout": "", "stderr": f"forbidden {kind}"}
        return {"rc": 0, "stdout": {"pods": self.pods, "services": self.services,
                                    "endpointslices": self.slices, "nodes": self.nodes}[kind], "stderr": ""}

    def stack_probe(self, host, address, port, user, *, password):
        self.probe_calls.append((host, address, port, user))
        if "probe" in self.fail:
            raise system.SystemCallFailed(f"server-state stack probe 실행 실패: echo {password}")
        return {"rc": 0, "probe": self.probe, "stderr": ""}


@pytest.fixture
def fake(monkeypatch):
    f = FakeStack()
    monkeypatch.setattr(system, "stack_kube", f.kube)
    monkeypatch.setattr(system, "stack_probe", f.stack_probe)
    return f


def _collector(clock=lambda: 0.0):
    return rc.RealCollector("farm1", "ailab-full", password_of={USER: PASSWORD}.__getitem__, clock=clock)


def _run(check):
    return _collector()(check, {"username": USER, "expected": {}})


def test_creation_all_pass_with_identifiers(fake):
    out = evaluator.evaluate_creation(_collector(), target={
        "username": USER, "expected": {"home_suffix": f"/{USER}"}})
    assert out["access_verdict"] == "PASS", out
    d = out["domains"]
    assert d["login"]["evidence"]["pod_uid"] == POD_UID
    assert d["endpoint"]["evidence"]["backend_pod_uid"] == POD_UID
    assert d["compute_uid"]["evidence"]["runtime_uid"] == 70001
    assert d["compute_nfs"]["evidence"]["mount_source"].endswith(f"/{USER}")
    assert d["compute_gpu"]["evidence"]["context_checked"] is True


def test_probe_from_desktop_on_other_node(fake):
    _run("login")
    assert fake.probe_calls == [("local", OTHER_NODE, 10022, USER)]


def test_no_other_node_is_unknown(fake):
    fake.nodes = f"farm1 {POD_NODE}\n"
    result, evidence = _run("login")
    assert result == "UNKNOWN" and not fake.probe_calls
    assert "InternalIP" in evidence["error"]


def test_observation_reused_within_ttl(fake):
    now = [0.0]
    col = _collector(clock=lambda: now[0])
    for check in evaluator.CREATION_CHECKS:
        col(check, {"username": USER})
    assert len(fake.probe_calls) == 1
    now[0] = 20.0
    col("login", {"username": USER})
    assert len(fake.probe_calls) == 2


@pytest.mark.parametrize("connect, login, endpoint, uid, inner", [
    ("ok", "PASS", "PASS", "PASS", "PASS"),
    ("auth_failed", "FAIL", "PASS", "FAIL", "UNKNOWN"),
    ("tcp_failed", "FAIL", "FAIL", "FAIL", "UNKNOWN"),
    ("timeout", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN"),
])
def test_creation_table_by_connect(fake, connect, login, endpoint, uid, inner):
    if connect != "ok":
        fake.probe = {"connect": connect, "error": "x", "checks": None}
    assert _run("login")[0] == login
    assert _run("endpoint")[0] == endpoint
    assert _run("compute_uid")[0] == uid
    for check in ("storage_rw", "credential", "compute_gpu", "compute_nfs"):
        assert _run(check)[0] == inner, check


def test_observation_failure_is_unknown(fake):
    fake.fail = {"probe"}
    for check in evaluator.CREATION_CHECKS:
        assert _run(check)[0] == "UNKNOWN", check


def test_pod_list_failure_only_blocks_dependent_checks(fake):
    fake.fail = {"pods"}
    assert _run("login")[0] == "UNKNOWN"
    assert _run("compute_blocked")[0] == "UNKNOWN"
    fake.fail = {"services"}
    assert _run("endpoint_blocked")[0] == "UNKNOWN"


@pytest.mark.parametrize("check, over, expected", [
    ("storage_rw", {"home_rw": {"write": True, "read": False, "remove": True}}, "FAIL"),
    ("credential", {"krb": {"rc": 1, "stdout": "", "stderr": ""}}, "FAIL"),
    ("credential", {"krb": {"rc": None, "stdout": "", "stderr": "timeout"}}, "UNKNOWN"),
    ("compute_gpu", {"gpu": {"rc": 9, "stdout": "", "stderr": "", "lines": 0}}, "FAIL"),
    ("compute_gpu", {"gpu_context": {"rc": 1, "stdout": "100\n", "stderr": ""}}, "FAIL"),
    ("compute_gpu", {"gpu_context": None}, "PASS"),
    ("compute_nfs", {"home_mount": {"rc": 0, "stdout": "/dev/sda1 ext4\n", "stderr": ""}}, "FAIL"),
    ("compute_uid", {"uid": {"rc": None, "stdout": "", "stderr": "timeout"}}, "UNKNOWN"),
])
def test_inner_checks(fake, check, over, expected):
    fake.probe = {"connect": "ok", "error": None, "checks": _checks(**over)}
    result, evidence = _run(check)
    assert result == expected, evidence
    if check == "compute_gpu":
        assert evidence["context_checked"] is (over.get("gpu_context", {}) is not None)


def test_e1_endpoint_to_other_pod_fails_in_evaluator(fake):
    fake.pods += f"ailab-{USER}-99999999 uid-pod-b {POD_NODE} Running\n"
    fake.slices = "uid-pod-b\n"
    out = evaluator.evaluate_creation(_collector(), target={
        "username": USER, "expected": {"home_suffix": f"/{USER}"}})
    assert all(d["result"] == "PASS" for d in out["domains"].values())
    assert out["relations"]["endpoint_to_pod"]["result"] == "FAIL"
    assert out["access_verdict"] == "FAIL"


def test_multiple_backends_keep_whole_list(fake):
    fake.slices = f"{POD_UID},uid-pod-b\n"
    _, evidence = _run("endpoint")
    assert evidence["backend_pod_uid"] == [POD_UID, "uid-pod-b"] and evidence["ambiguous"] is True


@pytest.mark.parametrize("connect, login, endpoint", [
    ("ok", "FAIL", "FAIL"),
    ("auth_failed", "PASS", "FAIL"),
    ("tcp_failed", "PASS", "PASS"),
    ("timeout", "UNKNOWN", "UNKNOWN"),
])
def test_reclamation_table(fake, connect, login, endpoint):
    fake.probe = {"connect": connect, "error": None, "checks": _checks() if connect == "ok" else None}
    assert _run("login_blocked")[0] == login
    assert _run("endpoint_blocked")[0] == endpoint
    assert _run("compute_blocked")[0] == "FAIL"


def test_reclamation_all_gone(fake):
    fake.pods = fake.services = fake.slices = ""
    for check in ("login_blocked", "endpoint_blocked", "compute_blocked"):
        assert _run(check)[0] == "PASS", check
    assert not fake.probe_calls


def test_credential_blocked_is_unknown(fake):
    result, evidence = _run("credential_blocked")
    assert result == "UNKNOWN"
    assert "vasc-16" in evidence["reason"]


def test_password_never_leaks(fake):
    col = _collector()
    assert PASSWORD not in repr(col) and PASSWORD not in repr(vars(col))
    out = evaluator.evaluate_creation(col, target={"username": USER, "expected": {}})
    assert PASSWORD not in repr(out)
    fake.fail = {"probe"}
    out = evaluator.evaluate_creation(_collector(), target={"username": USER, "expected": {}})
    assert PASSWORD not in repr(out)
    assert "***" in out["domains"]["login"]["evidence"]["error"]
