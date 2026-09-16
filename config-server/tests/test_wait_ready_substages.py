"""컨테이너 준비 대기 안의 세부 단계(이미지 다운로드·컨테이너 시작·볼륨 마운트)는 다른 단계와 같은
START/SUCCESS/FAIL 행으로 남는다. 진행 상황 단계 이름이 같은 동안은 새 행을 만들지 않는다."""
import types

import pytest

import main
from lifecycle_steps import provision


class _Pod:
    def read_namespaced_pod(self, name, ns):
        return types.SimpleNamespace()


@pytest.fixture
def ready_env(monkeypatch, logs, pod_status):
    monkeypatch.setattr(provision.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "get_pod_failure_reason", lambda pod: None)
    monkeypatch.setattr(main, "summarize_pod_start_events", lambda *a: {})
    monkeypatch.setattr(main, "_cleanup_create_failure", lambda *a, **k: {})
    monkeypatch.setitem(main.app.config, "POD_READY_MAX_WAIT_SEC", 30)

    def setup(stages, ready_after):
        calls = {"progress": 0, "ready": 0}

        def progress(v1, ns, pod):
            stage = stages[min(calls["progress"], len(stages) - 1)]
            calls["progress"] += 1
            return (stage, stage) if stage else None

        def ready(pod):
            calls["ready"] += 1
            return ready_after is not None and calls["ready"] > ready_after

        monkeypatch.setattr(main, "get_pod_progress_stage", progress)
        monkeypatch.setattr(main, "is_pod_ready", ready)
        return {"request_id": "1", "username": "u", "pod_name": "ailab-u-1", "node": "farm2", "v1": _Pod()}
    return setup


def _rows(logs):
    return [(r["action"].value, r["phase"].value, r.get("error_code")) for r in logs
            if r["action"] in (main.Action.PULL_IMAGE, main.Action.START_CONTAINER, main.Action.MOUNT_VOLUME)]


def test_pull_then_start_are_separate_steps(ready_env, logs):
    ctx = ready_env(["pulling_image", "pulling_image", "starting_container"], ready_after=12)
    with main.app.app_context():
        provision.step_wait_ready(ctx)
    assert _rows(logs) == [("PULL_IMAGE", "START", None), ("PULL_IMAGE", "SUCCESS", None),
                           ("START_CONTAINER", "START", None), ("START_CONTAINER", "SUCCESS", None)]


def test_cached_image_has_no_pull_step(ready_env, logs):
    ctx = ready_env(["starting_container"], ready_after=3)
    with main.app.app_context():
        provision.step_wait_ready(ctx)
    assert _rows(logs) == [("START_CONTAINER", "START", None), ("START_CONTAINER", "SUCCESS", None)]


def test_timeout_closes_open_step_as_fail(ready_env, logs, monkeypatch):
    monkeypatch.setitem(main.app.config, "POD_READY_MAX_WAIT_SEC", 12)
    ctx = ready_env(["mount_retrying"], ready_after=None)
    with main.app.app_context(), pytest.raises(main.StepFailed):
        provision.step_wait_ready(ctx)
    assert _rows(logs) == [("MOUNT_VOLUME", "START", None), ("MOUNT_VOLUME", "FAIL", "POD_READY_TIMEOUT")]


def test_no_events_leaves_no_substage_rows(ready_env, logs):
    ctx = ready_env([None], ready_after=2)
    with main.app.app_context():
        provision.step_wait_ready(ctx)
    assert _rows(logs) == []


def test_failure_row_carries_container_log(ready_env, logs, monkeypatch):
    """단계별로 볼 때 먼저 보게 되는 것은 준비 대기 실패 행이다. 컨테이너가 남긴 마지막 출력이
    그 행의 상세에 함께 남아야 원인을 바로 읽을 수 있다."""
    ctx = ready_env(["starting_container"], ready_after=None)
    monkeypatch.setattr(main, "get_pod_failure_reason", lambda pod: "PodFailed - Error (exit=1)")
    monkeypatch.setattr(main, "_cleanup_create_failure",
                        lambda *a, **k: {"podLogTail": "[ERROR] /home/u is not writable by u."})

    with main.app.app_context(), pytest.raises(main.StepFailed):
        provision.step_wait_ready(ctx)

    fail_row = [r for r in logs if r["action"] is main.Action.WAIT_READY and r["phase"] is main.Phase.FAIL][0]
    assert "PodFailed" in fail_row["error_detail"] and "not writable" in fail_row["error_detail"]


def test_container_death_is_not_retried(ready_env, logs, monkeypatch):
    """컨테이너가 죽으면 이 단계가 Pod를 지운다. 다시 기다려 봐야 없는 Pod만 보게 되고 그 오류가
    진짜 원인을 덮어쓰므로, 재시도하지 않는다는 표시를 달고 원인을 그대로 들고 나간다."""
    ctx = ready_env(["starting_container"], ready_after=None)
    monkeypatch.setattr(main, "get_pod_failure_reason", lambda pod: "PodFailed - Error (exit=1)")

    with main.app.app_context(), pytest.raises(main.StepFailed) as excinfo:
        provision.step_wait_ready(ctx)

    assert excinfo.value.retry is False
    assert excinfo.value.body["detail"] == "PodFailed - Error (exit=1)"


def test_failed_pod_log_is_kept_before_deletion(monkeypatch):
    """Pod를 지우면 컨테이너 로그도 사라진다. 기동에 실패한 이유는 그 로그에만 남으므로 먼저 읽어 둔다."""
    deleted = []

    class _V1:
        def read_namespaced_pod_log(self, name, ns, tail_lines=None):
            return "[ERROR] /home/u is not writable by u (55002:55002).\n"

        def delete_namespaced_pod(self, name, ns):
            deleted.append(name)

    monkeypatch.setattr(main, "release_nodeports", lambda pod_name: None)
    monkeypatch.setattr(provision, "delete_account_secret", lambda *a: None)

    with main.app.app_context():
        rollback = provision._cleanup_create_failure("ailab-u-1", _V1())

    assert "not writable" in rollback["podLogTail"]
    assert deleted == ["ailab-u-1"] and rollback["podDeleted"] is True
