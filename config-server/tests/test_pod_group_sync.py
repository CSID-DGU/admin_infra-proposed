"""admin_infra_server#25 — 떠 있는 Pod 의 /etc/group 에 새 보조 그룹을 더한다."""
import subprocess
import types

import pytest

import main
import utils


def _pod(name, phase="Running"):
    return types.SimpleNamespace(metadata=types.SimpleNamespace(name=name),
                                 status=types.SimpleNamespace(phase=phase))


@pytest.fixture
def k8s(monkeypatch):
    """Pod 목록과 exec 를 대역으로 바꾼다. 반환: (pods 목록, exec 결과 dict, exec 호출 목록)"""
    pods, outputs, execs = [], {}, []

    class FakeV1:
        def list_namespaced_pod(self, namespace, label_selector):
            assert label_selector == "username=alice"
            return types.SimpleNamespace(items=pods)

        def connect_get_namespaced_pod_exec(self, *a, **k):
            raise AssertionError("stream 을 거쳐야 한다")

    def fake_stream(fn, name, namespace, command, **kw):
        execs.append((name, command))
        out = outputs.get(name, "__RC=0")
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(utils, "load_k8s", lambda: None)
    monkeypatch.setattr(utils.client, "CoreV1Api", FakeV1)
    monkeypatch.setattr(utils, "stream", fake_stream)
    return pods, outputs, execs


def _sync(groups):
    with main.app.app_context():
        return utils.sync_running_pod_groups("alice", groups)


def test_only_running_pods_are_synced(k8s):
    pods, outputs, execs = k8s
    pods += [_pod("ailab-alice-1"), _pod("ailab-alice-2", "Pending")]
    assert _sync({"teamy": 70001, "teamx": 70000}) == {"synced": ["ailab-alice-1"], "failed": []}
    name, command = execs[0]
    assert [e[0] for e in execs] == ["ailab-alice-1"]
    # 이름·gid 는 셸 문자열이 아니라 위치 인자로 간다.
    assert command[:2] == ["/bin/sh", "-c"]
    assert command[4:] == ["alice", "teamx:70000", "teamy:70001"]


def test_one_pod_failing_does_not_stop_the_others(k8s):
    pods, outputs, execs = k8s
    pods += [_pod("p1"), _pod("p2"), _pod("p3")]
    outputs["p1"] = RuntimeError("exec 끊김")
    outputs["p2"] = "group teamx has gid 1, expected 70000\n__RC=1"
    assert _sync({"teamx": 70000}) == {"synced": ["p3"], "failed": ["p1", "p2"]}


def test_listing_failure_is_reported_not_raised(k8s, monkeypatch):
    """"떠 있는 Pod 없음"과 구분돼야 한다 — 둘 다 빈 목록이면 반영 누락을 알아챌 수 없다."""
    def boom():
        raise RuntimeError("kubeconfig 없음")
    monkeypatch.setattr(utils, "load_k8s", boom)
    assert _sync({"teamx": 70000}) == {"synced": [], "failed": [], "error": "POD_LIST_FAILED"}


def test_no_running_pods_has_no_error(k8s):
    assert _sync({"teamx": 70000}) == {"synced": [], "failed": []}


def test_nothing_to_do_without_groups(k8s):
    pods, outputs, execs = k8s
    pods.append(_pod("p1"))
    assert _sync({}) == {"synced": [], "failed": []}
    assert execs == []


# ---------- 셸 스크립트 자체 (getent/groupadd/usermod 를 가짜로 바꿔 실제 sh 로 돌린다) ----------

@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    """getent 는 group.db 파일을 읽고, groupadd/usermod 는 호출을 calls 에 적는다."""
    db, calls = tmp_path / "group.db", tmp_path / "calls"
    db.write_text("")
    calls.write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "getent": f'grep "^$2:" {db} || true',
        "groupadd": f'echo "groupadd $*" >> {calls}; echo "$3:x:$2:" >> {db}',
        "usermod": f'echo "usermod $*" >> {calls}',
    }.items():
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return db, calls


def _run_script(*args):
    return subprocess.run(["/bin/sh", "-c", utils._POD_GROUP_SYNC_SCRIPT, "decs-group-sync", *args],
                          capture_output=True, text=True).stdout


def test_script_creates_missing_group_and_adds_member(fake_bin):
    db, calls = fake_bin
    db.write_text("teamx:x:70000:\n")
    out = _run_script("alice", "teamx:70000", "teamy:70001")
    assert "__RC=0" in out
    assert calls.read_text().splitlines() == [
        "usermod -aG teamx alice",
        "groupadd -g 70001 teamy",
        "usermod -aG teamy alice",
    ]


def test_script_refuses_gid_mismatch_but_continues(fake_bin):
    db, calls = fake_bin
    db.write_text("video:x:44:\n")
    out = _run_script("alice", "video:70000", "teamy:70001")
    assert "__RC=1" in out
    assert "usermod -aG video alice" not in calls.read_text()
    assert "usermod -aG teamy alice" in calls.read_text()


def test_script_does_not_evaluate_names(fake_bin, tmp_path):
    db, calls = fake_bin
    marker = tmp_path / "pwned"
    _run_script("alice", f"x$(touch {marker}):70000")
    assert not marker.exists()
