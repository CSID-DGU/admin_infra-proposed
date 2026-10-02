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


def test_failed_commit_of_running_container_unpauses_it(monkeypatch):
    ran = []

    def helper(settings, node, args, fail_code):
        ran.append(args[0])
        if args[0] == "commit":
            raise node_image.NodeImageError(fail_code, "killed mid-commit")
        assert fail_code == "IMAGE_UNPAUSE_FAILED"
        return {"unpaused": True}

    monkeypatch.setattr(node_image, "_run_helper", helper)
    ref = node_image.ref_for("ns", "pod")
    with pytest.raises(node_image.NodeImageError) as e:
        node_image.commit(SETTINGS, "farm2", "containerd://" + CID, ref, running=True)
    assert e.value.code == "IMAGE_COMMIT_FAILED" and ran == ["commit", "unpause"]

    ran.clear()
    with pytest.raises(node_image.NodeImageError):
        node_image.commit(SETTINGS, "farm2", "containerd://" + CID, ref, running=False)
    assert ran == ["commit"]   # 멈춘 적이 없는 컨테이너는 풀 것이 없다


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
