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


def _run_script(tmp_path, files):
    import subprocess
    log = tmp_path / "log"
    log.mkdir(parents=True)
    for name, text in files.items():
        path = log / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    script = node_diag._SCRIPT.replace("/host-log", str(log)).replace("/tmp/gpu-lines", str(tmp_path / "o"))
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True).stdout


HDMI = "kernel: input: HDA NVidia HDMI/DP,pcm=3 as /devices/pci0000:80/snd_hda_intel\nkernel: nvidia-modeset: ERROR: GPU:0: Error while waiting\n"
XID = "kernel: NVRM: Xid (PCI:0000:84:00): 79, GPU has fallen off the bus.\n"


def test_filter_catches_driver_errors_not_hdmi_audio(tmp_path):
    out = _run_script(tmp_path, {"kern.log": HDMI + XID})
    assert "Xid" in out and "HDMI" not in out and "nvidia-modeset" not in out and "source: kern.log" in out


def test_previous_rotated_file_is_read_too(tmp_path):
    assert "Xid" in _run_script(tmp_path, {"kern.log": "x\n", "kern.log.1": XID})


def test_falls_back_to_syslog_then_journal_then_says_unreadable(tmp_path):
    assert "source: syslog" in _run_script(tmp_path / "a", {"syslog": XID})
    assert "source: journal" in _run_script(tmp_path / "b", {"journal/id/system.journal": "\x00" + XID})
    assert "no readable kernel log" in _run_script(tmp_path / "c", {})


def test_readable_but_no_gpu_lines_is_stated(tmp_path):
    assert "no GPU driver lines in: kern.log" in _run_script(tmp_path, {"kern.log": HDMI})
