"""노드 진단 Pod 결과 처리와 진단 ConfigMap 크기 한도."""
import types

import pytest

from adapters import gpu_check_state, node_diag

NS = types.SimpleNamespace


class FakeV1:
    def __init__(self, phase, log="out"):
        self.phase, self.log, self.deleted = phase, log, []

    def create_namespaced_pod(self, ns, body):
        return NS(metadata=NS(name="diag-1"))

    def read_namespaced_pod(self, name, ns):
        return NS(status=NS(phase=self.phase))

    def read_namespaced_pod_log(self, name, ns):
        return self.log

    def delete_namespaced_pod(self, name, ns, grace_period_seconds=None):
        self.deleted.append(name)


def test_succeeded_returns_log():
    v1 = FakeV1("Succeeded", "xid lines")
    assert node_diag.kernel_gpu_log(v1, "ns", "img", "n1") == "xid lines"
    assert v1.deleted == ["diag-1"]


def test_failed_pod_raises_and_is_still_deleted():
    v1 = FakeV1("Failed", "boom")
    with pytest.raises(RuntimeError, match="failed: boom"):
        node_diag.kernel_gpu_log(v1, "ns", "img", "n1")
    assert v1.deleted == ["diag-1"]


def test_fit_truncates_per_section_by_bytes_and_marks():
    big = "가" * 400_000  # 1.2MB(UTF-8)
    out = gpu_check_state._fit({"a": big, "b": "small"})
    assert out["b"] == "small"
    assert out["a"].startswith(gpu_check_state.TRUNCATED_MARK)
    assert sum(len(v.encode()) for v in out.values()) <= gpu_check_state.DIAG_MAX_BYTES
    out["a"].encode()  # 잘린 끝이 유효한 UTF-8


def test_fit_handles_none_and_empty():
    assert gpu_check_state._fit({}) == {}
    assert gpu_check_state._fit({"a": None}) == {"a": ""}
