"""GPU 점검 — 배정 GPU 하나씩 비교, SSH 확인, 1시간 재알림 억제, 재부팅 감지."""
import types

import pytest

import check_gpu_pods as c
from adapters.gpu_check_state import CheckState

NS = types.SimpleNamespace
A, B = "GPU-aaaa", "GPU-bbbb"


def _pod(name="p1", gpus=f"{A},{B}", phase="Running", node="n1", ip="10.0.0.1", ready=True):
    env = [NS(name="NVIDIA_VISIBLE_DEVICES", value=gpus)] if gpus is not None else []
    return NS(metadata=NS(name=name, uid="u-" + name, labels={"username": "alice"}),
              spec=NS(node_name=node, containers=[NS(name="shell", env=env)]),
              status=NS(phase=phase, pod_ip=ip, container_statuses=[NS(ready=ready)]))


def test_assigned_gpus_parses_uuid_list():
    assert c.assigned_gpus(_pod()) == [A, B]


@pytest.mark.parametrize("value", ["void", "all", "0,1", "", None])
def test_assigned_gpus_none_when_not_comparable(value):
    assert c.assigned_gpus(_pod(gpus=value)) is None


def test_missing_gpus_compares_one_by_one():
    assert c.missing_gpus([A, B], f"{A}\n{B}\n") == []
    assert c.missing_gpus([A, B], f"{A}\n") == [B]
    # 문자열 'GPU' 포함만으로는 정상이 아니다
    assert c.missing_gpus([A, B], "Failed to initialize NVML: GPU is lost") == [A, B]
    assert c.missing_gpus([A], "") == [A]


class FakeLog:
    def __getattr__(self, _):
        return lambda *a, **k: None


class FakeV1:
    def __init__(self, pods, nodes=(), cm=None, read_status=None):
        self.pods, self.nodes, self.cm, self.read_status = pods, list(nodes), cm, read_status
        self.saved = {}

    def read_namespaced_config_map(self, name, ns):
        from kubernetes import client
        if self.read_status:
            raise client.exceptions.ApiException(status=self.read_status)
        if self.cm is None:
            raise client.exceptions.ApiException(status=404)
        return NS(data=self.cm)

    def replace_namespaced_config_map(self, name, ns, body):
        self.saved[name] = body.data

    def list_namespaced_pod(self, ns, label_selector=None):
        return NS(items=self.pods)

    def list_node(self):
        return NS(items=self.nodes)


@pytest.fixture
def env(monkeypatch):
    sent, calls = [], {"diag": 0}
    monkeypatch.setattr(c, "load_k8s", lambda: None)
    monkeypatch.setitem(c.app.config, "INFRA_SLACK_WEBHOOK_URL", "http://hook.invalid")
    monkeypatch.setattr(c, "_send_slack", lambda m, w: sent.append(m) or True)
    monkeypatch.setattr(c, "tcp_ssh_alive", lambda host, *a, **k: True)
    monkeypatch.setattr(c, "_save_gpu_diagnostics", lambda v1, pod, node, kl, started: calls.update(diag=calls["diag"] + 1) or "gpu-diag-x")
    return NS(sent=sent, calls=calls, mp=monkeypatch)


def _run(env, v1, visible):
    env.mp.setattr(c.client, "CoreV1Api", lambda: v1)
    env.mp.setattr(c, "_exec", lambda v, name, cmd: visible)
    with c.app.app_context():
        c.check_gpu_pods()


def test_healthy_pod_sends_nothing(env):
    _run(env, FakeV1([_pod()]), f"{A}\n{B}")
    assert env.sent == [] and env.calls["diag"] == 0


def test_lost_gpu_alerts_once_per_hour_and_saves_diagnostics(env):
    v1 = FakeV1([_pod()])
    _run(env, v1, A)
    assert len(env.sent) == 1 and B in env.sent[0] and env.calls["diag"] == 1
    # 방금 알린 것으로 상태가 저장되면 다음 실행은 억제된다
    v1.cm = v1.saved["gpu-check-state"]
    _run(env, v1, A)
    assert len(env.sent) == 1 and env.calls["diag"] == 1


def test_alert_again_after_interval(env):
    import time
    import json
    v1 = FakeV1([_pod()], cm={"alerts": json.dumps({"p1:gpu": time.time() - c.REALERT_INTERVAL_SEC - 1}), "boot_ids": "{}"})
    _run(env, v1, A)
    assert len(env.sent) == 1


def test_ssh_down_alerts_without_gpu_diagnostics(env):
    env.mp.setattr(c, "tcp_ssh_alive", lambda host, *a, **k: False)
    _run(env, FakeV1([_pod()]), f"{A}\n{B}")
    assert len(env.sent) == 1 and "SSH" in env.sent[0] and env.calls["diag"] == 0


def test_failed_slack_does_not_start_suppression(env):
    env.mp.setattr(c, "_send_slack", lambda m, w: False)
    v1 = FakeV1([_pod()])
    _run(env, v1, A)
    assert "gpu-check-state" not in v1.saved


def _node(name, boot):
    return NS(metadata=NS(name=name), status=NS(node_info=NS(boot_id=boot), addresses=[NS(type="InternalIP", address="10.0.0.9")]))


def test_first_sight_of_node_only_records_boot_id(env):
    v1 = FakeV1([], nodes=[_node("n1", "b1")])
    _run(env, v1, "")
    assert env.sent == [] and '"b1"' in v1.saved["gpu-check-state"]["boot_ids"]


def test_boot_id_change_alerts_with_down_pods(env):
    down = _pod("p2", phase="Pending", gpus=None)
    v1 = FakeV1([_pod(), down], nodes=[_node("n1", "b2")], cm={"alerts": "{}", "boot_ids": '{"n1": "b1"}'})
    _run(env, v1, f"{A}\n{B}")
    assert len(env.sent) == 1 and "재부팅" in env.sent[0] and "p2(Pending)" in env.sent[0]
    assert "GPU Pod 점검 1개 중 유실 0개" in env.sent[0]
    assert '"b2"' in v1.saved["gpu-check-state"]["boot_ids"]


def test_same_boot_id_is_silent(env):
    v1 = FakeV1([], nodes=[_node("n1", "b1")], cm={"alerts": "{}", "boot_ids": '{"n1": "b1"}'})
    _run(env, v1, "")
    assert env.sent == []


def test_assigned_gpus_ignores_mig_because_it_cannot_be_compared():
    assert c.assigned_gpus(_pod(gpus="MIG-1234")) is None


def test_exec_rc_parses_marker(monkeypatch):
    monkeypatch.setattr(c, "_exec", lambda v, n, cmd: "hello\n__RC=3\n")
    assert c._exec_rc(None, "p", "x") == ("hello", 3)
    monkeypatch.setattr(c, "_exec", lambda v, n, cmd: "garbage")
    assert c._exec_rc(None, "p", "x")[1] == -1


def test_new_kind_of_problem_alerts_even_while_suppressed(env):
    import json
    import time
    # SSH 문제로 방금 알린 Pod 에 GPU 문제가 새로 생기면 알린다
    v1 = FakeV1([_pod()], cm={"alerts": json.dumps({"p1:ssh": time.time()}), "boot_ids": "{}"})
    _run(env, v1, A)
    assert len(env.sent) == 1 and "GPU 유실" in env.sent[0]


def test_down_pods_list_only_user_pods_not_succeeded():
    cron = NS(metadata=NS(name="gpu-check-x", labels={"app": "x"}), status=NS(phase="Pending", container_statuses=None))
    done = _pod("d", phase="Succeeded")
    assert not c._pod_is_down(cron) and not c._pod_is_down(done)
    assert c._pod_is_down(_pod("u", phase="Pending")) and c._pod_is_down(_pod("u", ready=False))


def test_nfs_state_uses_user_read_exit_code(monkeypatch):
    pods = [_pod()]
    monkeypatch.setattr(c, "_exec_rc", lambda v, n, cmd: ("", 0))
    assert c._nfs_state(None, pods) == "정상"
    monkeypatch.setattr(c, "_exec_rc", lambda v, n, cmd: ("", 2))
    assert "이상" in c._nfs_state(None, pods)
    assert "확인 불가" in c._nfs_state(None, [_pod(phase="Pending")])


def test_node_ssh_uses_configured_port_and_never_guesses_22(monkeypatch):
    seen = []
    monkeypatch.setattr(c, "tcp_ssh_alive", lambda host, port=22, *a, **k: seen.append((host, port)) or port == 8081)
    monkeypatch.setitem(c.app.config, "FARM_NODES", [{"name": "n1", "host": "h1", "port": "8081"}])
    with c.app.app_context():
        assert c._node_ssh_state(_node("n1", "b")) == "정상" and seen == [("h1", 8081)]
        monkeypatch.setitem(c.app.config, "FARM_NODES", [{"name": "n1", "host": "h1", "port": "9999"}])
        assert c._node_ssh_state(_node("n1", "b")) == "응답 없음"
        seen.clear()
        assert "확인 불가" in c._node_ssh_state(_node("other", "b")) and seen == []


def test_gpu_state_reports_real_counts_and_unknown():
    assert "확인 불가" in c._gpu_state(None)
    assert c._gpu_state({"ok": 2, "lost": 1, "unknown": 1}) == "GPU Pod 점검 3개 중 유실 1개, 판단 불가 1개"


def test_state_read_failure_is_loud_and_never_overwritten(env, monkeypatch):
    # RBAC 누락(403): 상태를 못 읽는다. 덮어쓰지 않고, 오류로 남기고, 알림은 시각 창 안에서만 나간다.
    errors = []
    monkeypatch.setattr(c.app.logger, "error", lambda m, *a, **k: errors.append(m))
    monkeypatch.setattr(c.time, "time", lambda: 3600 * 100 + 30)  # 창 안
    v1 = FakeV1([_pod()], nodes=[_node("n1", "b1")], read_status=403)
    _run(env, v1, A)
    assert v1.saved == {}
    assert any("상태 저장소 사용 불가" in e for e in errors)
    assert any("GPU 유실" in m for m in env.sent) and any("상태 저장소 사용 불가" in m for m in env.sent)


def test_state_read_failure_outside_window_does_not_spam(env, monkeypatch):
    monkeypatch.setattr(c.time, "time", lambda: 3600 * 100 + 1800)  # 창 밖
    _run(env, FakeV1([_pod()], read_status=403), A)
    assert env.sent == []


def test_no_webhook_skips_diagnostics(env):
    env.mp.setitem(c.app.config, "INFRA_SLACK_WEBHOOK_URL", "")
    _run(env, FakeV1([_pod()]), A)
    assert env.calls["diag"] == 0 and env.sent == []
