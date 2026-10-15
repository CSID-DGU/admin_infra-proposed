"""노드 로컬 이미지 모듈: 이름·입력 검증, 도우미 Pod 정의, 도우미 결과 읽기, 누적 변경분 계산."""
import types

import pytest

import node_image_cli
from adapters import node_image

SETTINGS = node_image.HelperSettings(namespace="ailab-full", image="dguailab/config-server:x",
                                     socket="/run/containerd/containerd.sock", timeout_sec=30)
CID = "a" * 64


def test_ref_is_per_namespace_and_pod_and_never_pullable():
    ref = node_image.ref_for("ailab-full", "ailab-exp-001-7f3a9c21")
    assert ref == "ailab.local/user-image/ailab-full:ailab-exp-001-7f3a9c21"
    assert node_image.is_user_image(ref) and not node_image.is_user_image("dguailab/decs:1")
    assert not node_image.is_user_image(None)


def test_container_id_must_be_runtime_id():
    assert node_image.container_id("containerd://" + CID) == CID
    for bad in ("", None, "containerd://abc", CID + "; reboot"):
        with pytest.raises(node_image.NodeImageError) as e:
            node_image.container_id(bad)
        assert e.value.code == "IMAGE_COMMIT_NO_CONTAINER"


def test_foreign_ref_is_rejected_before_any_pod_is_created():
    for bad in ("dguailab/decs:1", "ailab.local/user-image/ns:pod extra", "ailab.local/user-image/ns"):
        with pytest.raises(node_image.NodeImageError) as e:
            node_image.remove(SETTINGS, "farm2", bad)
        assert e.value.code == "IMAGE_REF_INVALID"


def test_helper_pod_is_pinned_to_node_and_has_no_api_token():
    spec = node_image._helper_pod(SETTINGS, "farm2", ["remove", "--ref", "r"])["spec"]
    assert spec["nodeName"] == "farm2" and spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False and spec["activeDeadlineSeconds"] > 30
    assert spec["containers"][0]["command"][-3:] == ["remove", "--ref", "r"]
    assert spec["volumes"][0]["hostPath"] == {"path": SETTINGS.socket, "type": "Socket"}


def test_missing_helper_image_is_reported():
    empty = node_image.HelperSettings(namespace="ns", image="", socket="/s", timeout_sec=1)
    with pytest.raises(node_image.NodeImageError) as e:
        node_image.remove(empty, "farm2", node_image.ref_for("ns", "pod"))
    assert e.value.code == "IMAGE_HELPER_NOT_CONFIGURED"


def test_result_is_last_result_line_ignoring_tool_noise():
    log = 'time="..." level=warning msg="x"\nRESULT {"added_bytes": 7}\n'
    assert node_image._parse_result(log) == {"added_bytes": 7}
    assert "error" in node_image._parse_result("level=fatal msg=boom")


def _inspected(*comments):
    return {"Manifest": {"layers": [{"size": 10 * (i + 1)} for i in range(len(comments))]},
            "ImageConfig": {"history": [{"comment": "buildkit", "empty_layer": True}]
                            + [{"comment": c} for c in comments]}}


def test_added_bytes_counts_only_committed_layers_on_top():
    marker = node_image_cli.COMMIT_MARKER
    assert node_image_cli.added_bytes(_inspected("buildkit", "buildkit")) == 0
    assert node_image_cli.added_bytes(_inspected("buildkit", marker)) == 20
    assert node_image_cli.added_bytes(_inspected("buildkit", marker, marker)) == 50


def test_only_freezing_commands_mount_the_node_cgroup():
    def volumes(args):
        return [v["name"] for v in node_image._helper_pod(SETTINGS, "farm2", args)["spec"]["volumes"]]

    assert volumes(["remove", "--ref", "r"]) == ["containerd"]
    assert volumes(["commit", "--container", CID]) == ["containerd", "cgroup"]
    assert volumes(["thaw", "--container", CID]) == ["containerd", "cgroup"]
    container = node_image._helper_pod(SETTINGS, "farm2", ["thaw"])["spec"]["containers"][0]
    assert "securityContext" not in container   # 특권 없이 쓰기 마운트만으로 된다


def _helper_replies(monkeypatch, thaw):
    """commit은 항상 실패하고, thaw는 thaw()의 결과를 돌려주는 도우미."""
    ran = []

    def helper(settings, node, args, fail_code):
        ran.append(args[0])
        if args[0] == "commit":
            raise node_image.NodeImageError(fail_code, "helper timed out")
        assert fail_code == "IMAGE_THAW_FAILED"
        return thaw()

    monkeypatch.setattr(node_image, "_run_helper", helper)
    return ran


def _commit_running(running=True):
    with pytest.raises(node_image.NodeImageError) as e:
        node_image.commit(SETTINGS, "farm2", "containerd://" + CID, node_image.ref_for("ns", "pod"), running=running)
    return e.value


def test_failed_commit_of_running_container_is_thawed_again(monkeypatch):
    ran = _helper_replies(monkeypatch, lambda: {"frozen": False})
    assert _commit_running().code == "IMAGE_COMMIT_FAILED" and ran == ["commit", "thaw"]

    ran.clear()
    assert _commit_running(running=False).code == "IMAGE_COMMIT_FAILED"
    assert ran == ["commit"]   # 멈춘 적이 없는 컨테이너는 풀 것이 없다


def test_container_still_frozen_after_failed_commit_is_reported_as_left_paused(monkeypatch):
    _helper_replies(monkeypatch, lambda: {"frozen": True})
    error = _commit_running()
    assert error.code == "IMAGE_LEFT_PAUSED" and "helper timed out" in error.detail


def test_unconfirmed_thaw_is_reported_as_left_paused(monkeypatch):
    def thaw():
        raise node_image.NodeImageError("IMAGE_HELPER_TIMEOUT", "thaw helper stuck")

    _helper_replies(monkeypatch, thaw)
    error = _commit_running()
    assert error.code == "IMAGE_LEFT_PAUSED" and "thaw helper stuck" in error.detail


def test_left_frozen_that_thaws_on_retry_is_an_ordinary_failure(monkeypatch):
    def helper(settings, node, args, fail_code):
        if args[0] == "commit":
            raise node_image.NodeImageError("IMAGE_LEFT_PAUSED", "container still frozen after thaw")
        return {"frozen": False}

    monkeypatch.setattr(node_image, "_run_helper", helper)
    assert _commit_running().code == "IMAGE_COMMIT_FAILED"


def test_helper_result_code_overrides_callers_code(monkeypatch):
    _with_v1(monkeypatch, _FakeV1("Failed", 'RESULT {"error": "still frozen", "code": "IMAGE_LEFT_PAUSED"}'))
    with pytest.raises(node_image.NodeImageError) as e:
        node_image._run_helper(SETTINGS, "farm2", ["commit"], "IMAGE_COMMIT_FAILED")
    assert e.value.code == "IMAGE_LEFT_PAUSED"


class _FakeCgroup:
    """컨테이너 cgroup 하나. freeze_after번째 읽기부터 다 멈춘다(None이면 끝내 다 멈추지 않는다)."""

    def __init__(self, root, layout, freeze_after=0):
        self.dir = root / layout.format(cid=CID)
        self.dir.mkdir(parents=True)
        self.freeze_after, self.reads = freeze_after, 0
        (self.dir / "cgroup.freeze").write_text("0\n")

    def frozen(self):
        """도우미가 읽는 cgroup.events 대신 이 값을 쓴다."""
        requested = (self.dir / "cgroup.freeze").read_text().strip() == "1"
        if not requested:
            return False
        self.reads += 1
        return self.freeze_after is not None and self.reads > self.freeze_after


def _cli(monkeypatch, cgroup, commit_ok=True):
    calls = []

    def nerdctl(socket, *args):
        calls.append(args[0])
        assert args[0] != "commit" or cgroup.frozen(), "구울 때는 이미 다 멈춰 있어야 한다"
        return types.SimpleNamespace(returncode=0 if commit_ok else 1, stdout="", stderr="boom")

    monkeypatch.setattr(node_image_cli, "_nerdctl", nerdctl)
    monkeypatch.setattr(node_image_cli, "_is_frozen", lambda directory: cgroup.frozen())
    monkeypatch.setattr(node_image_cli.time, "sleep", lambda sec: None)
    monkeypatch.setattr(node_image_cli, "_inspect", lambda socket, ref: {})
    monkeypatch.setattr(node_image_cli, "added_bytes", lambda inspected: 7)
    return calls


def _freeze_value(cgroup):
    return (cgroup.dir / "cgroup.freeze").read_text()


@pytest.mark.parametrize("layout", [
    "kubepods/burstable/podu1/{cid}",
    "kubepods/podu1/{cid}",
    "kubepods.slice/kubepods-burstable.slice/kubepods-burstable-podu1.slice/cri-containerd-{cid}.scope",
    "kubepods.slice/kubepods-podu1.slice/cri-containerd-{cid}.scope",
])
def test_cgroup_dir_is_found_for_both_drivers_and_all_qos(tmp_path, layout):
    cgroup = _FakeCgroup(tmp_path, layout)
    assert node_image_cli.cgroup_dir(str(tmp_path), CID) == str(cgroup.dir)


def test_cgroup_without_freezer_is_refused(tmp_path):
    cgroup = _FakeCgroup(tmp_path, "kubepods/burstable/podu1/{cid}")
    (cgroup.dir / "cgroup.freeze").unlink()
    with pytest.raises(RuntimeError, match="freezer"):
        node_image_cli.cgroup_dir(str(tmp_path), CID)


def test_running_container_is_frozen_committed_then_thawed(monkeypatch, tmp_path):
    cgroup = _FakeCgroup(tmp_path, "kubepods/burstable/podu1/{cid}", freeze_after=3)
    calls = _cli(monkeypatch, cgroup)
    assert node_image_cli.commit("/s", CID, "r", running=True, cgroup_root=str(tmp_path)) == {"added_bytes": 7}
    assert calls == ["commit"] and _freeze_value(cgroup) == "0"   # containerd에는 멈춤·풀기를 묻지 않는다


def test_container_that_never_freezes_is_thawed_without_committing(monkeypatch, tmp_path):
    cgroup = _FakeCgroup(tmp_path, "kubepods/burstable/podu1/{cid}", freeze_after=None)
    calls = _cli(monkeypatch, cgroup)
    with pytest.raises(RuntimeError, match="did not freeze within 0s"):
        node_image_cli.commit("/s", CID, "r", running=True, freeze_wait_sec=0, cgroup_root=str(tmp_path))
    assert calls == [] and _freeze_value(cgroup) == "0"


def test_failed_commit_still_thaws(monkeypatch, tmp_path):
    cgroup = _FakeCgroup(tmp_path, "kubepods/burstable/podu1/{cid}")
    _cli(monkeypatch, cgroup, commit_ok=False)
    with pytest.raises(RuntimeError, match="commit failed"):
        node_image_cli.commit("/s", CID, "r", running=True, cgroup_root=str(tmp_path))
    assert _freeze_value(cgroup) == "0"


def test_container_that_stays_frozen_is_reported_with_left_paused_code(monkeypatch, tmp_path, capsys):
    cgroup = _FakeCgroup(tmp_path, "kubepods/burstable/podu1/{cid}")
    _cli(monkeypatch, cgroup)
    monkeypatch.setattr(node_image_cli, "_is_frozen", lambda directory: True)   # 풀어도 그대로 멈춰 있다
    monkeypatch.setattr(node_image_cli, "CGROUP_ROOT", str(tmp_path))
    monkeypatch.setattr(node_image_cli, "_THAW_WAIT_SEC", 0)
    assert node_image_cli.main(["--socket", "/s", "commit", "--container", CID, "--ref", "r", "--running"]) == 1
    assert '"code": "IMAGE_LEFT_PAUSED"' in capsys.readouterr().out


def test_stopped_container_is_committed_without_touching_cgroup(monkeypatch, tmp_path):
    cgroup = _FakeCgroup(tmp_path, "kubepods/burstable/podu1/{cid}")
    calls = _cli(monkeypatch, cgroup)
    monkeypatch.setattr(node_image_cli, "_nerdctl", lambda socket, *args: calls.append(args[0])
                        or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    node_image_cli.commit("/s", CID, "r", running=False, cgroup_root=str(tmp_path / "missing"))
    assert calls == ["commit"]


def test_thaw_command_unfreezes_and_reports(monkeypatch, tmp_path):
    cgroup = _FakeCgroup(tmp_path, "kubepods/burstable/podu1/{cid}")
    (cgroup.dir / "cgroup.freeze").write_text("1")
    _cli(monkeypatch, cgroup)
    assert node_image_cli.thaw(CID, cgroup_root=str(tmp_path)) == {"frozen": False}
    assert _freeze_value(cgroup) == "0"


class _FakeV1:
    """도우미 Pod 하나의 일생: 만들고, 끝나기를 기다리고, 로그를 읽고, 지운다."""

    def __init__(self, phase, log=""):
        self.phase, self.log, self.deleted = phase, log, []

    def create_namespaced_pod(self, namespace, body):
        return types.SimpleNamespace(metadata=types.SimpleNamespace(name="ailab-image-helper-x"))

    def read_namespaced_pod(self, name, namespace):
        return types.SimpleNamespace(status=types.SimpleNamespace(phase=self.phase))

    def read_namespaced_pod_log(self, name, namespace):
        return self.log

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds=None):
        self.deleted.append(name)


def _with_v1(monkeypatch, v1):
    monkeypatch.setattr(node_image.client, "CoreV1Api", lambda: v1)
    monkeypatch.setattr(node_image.time, "sleep", lambda sec: None)
    return v1


def test_helper_failure_carries_callers_code_and_pod_is_removed(monkeypatch):
    v1 = _with_v1(monkeypatch, _FakeV1("Failed", 'RESULT {"error": "rmi failed: conflict"}'))
    with pytest.raises(node_image.NodeImageError) as e:
        node_image.remove(SETTINGS, "farm2", node_image.ref_for("ns", "pod"))
    assert e.value.code == "IMAGE_REMOVE_FAILED" and "conflict" in e.value.detail
    assert v1.deleted == ["ailab-image-helper-x"]


def test_helper_that_never_finishes_times_out_and_pod_is_removed(monkeypatch):
    v1 = _with_v1(monkeypatch, _FakeV1("Running"))
    stuck = node_image.HelperSettings(namespace="ns", image="img", socket="/s", timeout_sec=0)
    with pytest.raises(node_image.NodeImageError) as e:
        node_image.remove(stuck, "farm2", node_image.ref_for("ns", "pod"))
    assert e.value.code == "IMAGE_HELPER_TIMEOUT" and v1.deleted == ["ailab-image-helper-x"]


def test_successful_helper_returns_result(monkeypatch):
    _with_v1(monkeypatch, _FakeV1("Succeeded", 'RESULT {"removed": true}'))
    assert node_image.remove(SETTINGS, "farm2", node_image.ref_for("ns", "pod")) is True


def _nerdctl_replies(monkeypatch, replies):
    calls = []

    def fake(socket, *args):
        calls.append(args)
        code, err = replies[len(calls) - 1]
        return types.SimpleNamespace(returncode=code, stdout="", stderr=err)

    monkeypatch.setattr(node_image_cli, "_nerdctl", fake)
    return calls


def test_remove_forces_only_when_a_stopped_container_holds_the_image(monkeypatch):
    held = "conflict: unable to delete r (must be forced) - image is being used by stopped container abc"
    calls = _nerdctl_replies(monkeypatch, [(1, held), (0, "")])
    assert node_image_cli.remove("/s", "r") == {"removed": True}
    assert calls == [("rmi", "r"), ("rmi", "--force", "r")]


def test_remove_does_not_force_an_image_a_running_container_uses(monkeypatch):
    running = "conflict: unable to delete r (cannot be forced) - image is being used by running container abc"
    calls = _nerdctl_replies(monkeypatch, [(1, running)])
    with pytest.raises(RuntimeError):
        node_image_cli.remove("/s", "r")
    assert calls == [("rmi", "r")]


def test_remove_of_missing_image_is_not_an_error(monkeypatch):
    _nerdctl_replies(monkeypatch, [(1, "no such image: r")])
    assert node_image_cli.remove("/s", "r") == {"removed": False}
