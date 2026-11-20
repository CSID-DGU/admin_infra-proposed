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
    # 홈 읽기는 기본으로 정상이다. homes 에 {pod 이름: (판정, 설명)}을 넣으면 그 Pod 만 그렇게 답한다.
    homes, home_reads = {}, []

    def read_home(v1, pod_name, user):
        home_reads.append(pod_name)
        answer = homes.get(pod_name, ("ok", ""))
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(c, "_read_home", read_home)
    return NS(sent=sent, calls=calls, mp=monkeypatch, homes=homes, home_reads=home_reads)


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


# ── 홈(NFS) 읽기 점검 ─────────────────────────────────────────────────────────

HEALTHY_GPU = f"{A}\n{B}"
TIMEOUT = ("fail", "10초 안에 읽히지 않음")


def _user_pod(name, user, node="n1", **kw):
    pod = _pod(name, node=node, **kw)
    pod.metadata.labels = {"username": user}
    return pod


def _saved_homes(v1):
    import json
    return json.loads(v1.saved["gpu-check-state"]["home_failures"])


@pytest.mark.parametrize("rc,output,verdict,text", [
    (0, "", "ok", ""),
    (124, "", "fail", "10초 안에 읽히지 않음"),
    (137, "Session terminated, killing shell...\nKilled", "fail", "10초 안에 읽히지 않음"),
    (1, "stat: cannot read file system information for '/home/alice': Permission denied", "fail",
     "읽기 실패 (rc=1: stat: cannot read file system information for '/home/alice': Permission denied)"),
    (2, "", "fail", "읽기 실패 (rc=2)"),
    (127, "/bin/sh: 1: timeout: not found", "unknown", "점검 명령을 실행할 수 없음 (rc=127)"),
    (-1, "", "unknown", "점검 명령의 종료코드를 받지 못함"),
])
def test_read_home_judges_by_exit_code(monkeypatch, rc, output, verdict, text):
    seen = []
    monkeypatch.setattr(c, "_exec_rc", lambda v, n, cmd: seen.append(cmd) or (output, rc))
    assert c._read_home(None, "p1", "alice") == (verdict, text)
    # 사용자 계정으로, 제한 시간을 걸고, 캐시를 쓰지 않는 stat -f 와 홈 목록 읽기를 한다
    assert seen == ["timeout -k 2 10 su -s /bin/sh alice -c 'stat -f ~ >/dev/null && ls ~ >/dev/null'"]


def test_read_home_without_exit_code_after_exec_timeout_is_a_failure(monkeypatch):
    # exec 제한 시간(15초)까지 기다리고도 종료코드가 없으면 읽기가 끝나지 않은 것이다
    clock = iter([100.0, 115.0])
    monkeypatch.setattr(c.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(c, "_exec_rc", lambda v, n, cmd: ("", -1))
    assert c._read_home(None, "p1", "alice") == ("fail", "15초 안에 읽기가 끝나지 않음")


def test_exec_rc_reads_exit_code_followed_by_late_output(monkeypatch):
    # 2026-11-19 실측: timeout 이 su 를 KILL 로 끝내면 종료코드 뒤에 "Killed"가 더 찍힌다
    monkeypatch.setattr(c, "_exec", lambda v, n, cmd: "Session terminated, killing shell...__RC=137\nKilled\n")
    assert c._exec_rc(None, "p", "x") == ("Session terminated, killing shell...\nKilled", 137)


def test_home_failure_alerts_once_and_is_not_read_again_within_the_hour(env):
    v1 = FakeV1([_user_pod("p1", "alice")])
    env.homes["p1"] = TIMEOUT
    _run(env, v1, HEALTHY_GPU)
    assert len(env.sent) == 1 and "홈(NFS) 읽기 이상" in env.sent[0]
    assert "▶ n1 p1 (사용자 alice): 10초 안에 읽히지 않음" in env.sent[0]
    assert "Pod 1개 중 1개" in env.sent[0]
    assert _saved_homes(v1)["p1"]["alerted"] is True
    # 다음 실행: 저장된 기록이 있으면 홈을 다시 읽지 않고, 다시 알리지도 않는다
    v1.cm = v1.saved["gpu-check-state"]
    _run(env, v1, HEALTHY_GPU)
    assert env.home_reads == ["p1"] and len(env.sent) == 1


def test_home_failure_is_read_again_after_the_hour(env):
    import json
    import time
    old = {"p1": {"at": time.time() - c.HOME_RECHECK_SEC - 1, "why": "10초 안에 읽히지 않음", "alerted": True}}
    cm = {"alerts": "{}", "boot_ids": "{}", "home_failures": json.dumps(old)}
    # 여전히 읽히지 않으면 다시 알린다
    v1 = FakeV1([_user_pod("p1", "alice")], cm=dict(cm))
    env.homes["p1"] = TIMEOUT
    _run(env, v1, HEALTHY_GPU)
    assert env.home_reads == ["p1"] and len(env.sent) == 1
    # 읽히면 기록을 지우고 알리지 않는다
    env.sent.clear()
    v1 = FakeV1([_user_pod("p1", "alice")], cm=dict(cm))
    env.homes.clear()
    _run(env, v1, HEALTHY_GPU)
    assert env.sent == [] and _saved_homes(v1) == {}


def test_home_failures_of_many_pods_go_out_as_one_message(env):
    pods = [_user_pod("p1", "alice", node="n2"), _user_pod("p2", "bob"), _user_pod("p3", "carol")]
    env.homes.update(p1=TIMEOUT, p2=("fail", "읽기 실패 (rc=1: Permission denied)"))
    _run(env, FakeV1(pods), HEALTHY_GPU)
    assert len(env.sent) == 1
    lines = env.sent[0].splitlines()
    # 노드, Pod 이름 순으로 적는다
    assert lines[1] == "▶ n1 p2 (사용자 bob): 읽기 실패 (rc=1: Permission denied)"
    assert lines[2] == "▶ n2 p1 (사용자 alice): 10초 안에 읽히지 않음"
    assert "Pod 3개 중 2개" in env.sent[0] and "p3" not in env.sent[0]


def test_home_alert_that_could_not_be_sent_is_retried_without_reading_again(env):
    env.mp.setattr(c, "_send_slack", lambda m, w: False)
    v1 = FakeV1([_user_pod("p1", "alice")])
    env.homes["p1"] = TIMEOUT
    _run(env, v1, HEALTHY_GPU)
    # 알리지 못해도 실패 기록은 저장한다(다시 읽지 않기 위해). 알림은 아직 안 나간 것으로 남는다
    assert _saved_homes(v1)["p1"]["alerted"] is False
    env.mp.setattr(c, "_send_slack", lambda m, w: env.sent.append(m) or True)
    v1.cm = v1.saved["gpu-check-state"]
    _run(env, v1, HEALTHY_GPU)
    assert env.home_reads == ["p1"] and len(env.sent) == 1
    assert _saved_homes(v1)["p1"]["alerted"] is True


@pytest.mark.parametrize("answer", [("unknown", "점검 명령의 종료코드를 받지 못함"), RuntimeError("exec failed")])
def test_home_read_that_cannot_be_judged_does_not_alert(env, answer):
    v1 = FakeV1([_user_pod("p1", "alice")])
    env.homes["p1"] = answer
    _run(env, v1, HEALTHY_GPU)
    assert env.sent == [] and "gpu-check-state" not in v1.saved


def test_home_is_read_only_in_running_user_pods_with_a_safe_account_name(env):
    system = _pod("gpu-check-x")
    system.metadata.labels = {"app": "x"}
    terminating = _user_pod("p-term", "dave")
    terminating.metadata.deletion_timestamp = "2026-11-19T00:00:00Z"
    pods = [_user_pod("p-ok", "alice"), _user_pod("p-pending", "bob", phase="Pending"), terminating, system,
            _user_pod("p-bad", "x; rm -rf /")]
    _run(env, FakeV1(pods), HEALTHY_GPU)
    assert env.home_reads == ["p-ok"]


def test_home_reads_stop_at_the_deadline_and_the_rest_wait_for_the_next_run(env):
    # 첫 Pod 를 읽는 동안 제한 시간을 넘기면 나머지는 읽지 않는다
    clock = {"now": 1_000_000.0}
    env.mp.setattr(c.time, "time", lambda: clock["now"])

    def slow_read(v1, pod_name, user):
        env.home_reads.append(pod_name)
        clock["now"] += c.HOME_CHECK_DEADLINE_SEC + 1
        return TIMEOUT
    env.mp.setattr(c, "_read_home", slow_read)
    v1 = FakeV1([_user_pod("p1", "alice"), _user_pod("p2", "bob"), _user_pod("p3", "carol")])
    _run(env, v1, HEALTHY_GPU)
    assert env.home_reads == ["p1"]
    assert len(env.sent) == 1 and "p1" in env.sent[0] and "p2" not in env.sent[0]
    assert set(_saved_homes(v1)) == {"p1"}


def test_home_failure_record_of_a_vanished_pod_is_dropped(env):
    import json
    import time
    cm = {"alerts": "{}", "boot_ids": "{}",
          "home_failures": json.dumps({"gone": {"at": time.time(), "why": "x", "alerted": True}})}
    v1 = FakeV1([_user_pod("p1", "alice")], cm=cm)
    _run(env, v1, HEALTHY_GPU)
    assert _saved_homes(v1) == {} and env.sent == []


def test_home_and_gpu_problems_are_reported_separately(env):
    v1 = FakeV1([_user_pod("p1", "alice")])
    env.homes["p1"] = TIMEOUT
    _run(env, v1, A)  # GPU 하나가 보이지 않는다
    assert len(env.sent) == 2
    assert "GPU 유실" in env.sent[0] and "홈" not in env.sent[0]
    assert "홈(NFS) 읽기 이상" in env.sent[1]


def test_home_check_without_state_store_runs_only_in_the_hourly_window(env, monkeypatch):
    # 상태를 저장하지 못하면 "실패한 Pod 는 다시 읽지 않는다"를 지킬 수 없어 한 시간에 한 번만 읽는다
    env.homes["p1"] = TIMEOUT
    monkeypatch.setattr(c.time, "time", lambda: 3600 * 100 + 1800)  # 창 밖
    _run(env, FakeV1([_user_pod("p1", "alice")], read_status=403), HEALTHY_GPU)
    assert env.home_reads == [] and env.sent == []
    monkeypatch.setattr(c.time, "time", lambda: 3600 * 100 + 30)  # 창 안
    v1 = FakeV1([_user_pod("p1", "alice")], read_status=403)
    _run(env, v1, HEALTHY_GPU)
    assert env.home_reads == ["p1"] and v1.saved == {}
    assert any("홈(NFS) 읽기 이상" in m for m in env.sent)


def test_nfs_state_in_reboot_report_reads_the_home_the_same_way(monkeypatch):
    monkeypatch.setattr(c, "_read_home", lambda v, name, user: ("unknown", "점검 명령의 종료코드를 받지 못함"))
    assert c._nfs_state(None, [_pod()]) == "확인 불가 (점검 명령의 종료코드를 받지 못함)"
    monkeypatch.setattr(c, "_read_home", lambda v, name, user: ("fail", "10초 안에 읽히지 않음"))
    assert c._nfs_state(None, [_pod()]) == "이상 (홈 10초 안에 읽히지 않음)"


def test_home_failure_is_saved_before_the_alert_is_sent(env):
    # 알림을 보내는 중에 실행 제한 시간으로 끊겨도, 실패 기록은 이미 저장돼 있어 다음 실행이 홈을 다시 읽지 않는다
    def cut_off(message, what):
        raise KeyboardInterrupt
    env.mp.setattr(c, "_send_slack", cut_off)
    v1 = FakeV1([_user_pod("p1", "alice")])
    env.homes["p1"] = TIMEOUT
    with pytest.raises(KeyboardInterrupt):
        _run(env, v1, HEALTHY_GPU)
    assert _saved_homes(v1)["p1"]["alerted"] is False
