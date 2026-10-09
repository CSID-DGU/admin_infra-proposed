"""계정 접속 차단·해제 작업(POST /operations/access) — 등록 검증과, Service 를 지우지 않고 선택자만 바꾸는 단계.
차단 중에 새로 만들어지는 Service 가 막힌 채로 만들어지는지도 여기서 본다."""
import json
import types

import pytest

import main
import utils
from lifecycle_steps import access, verify
from main import Action, Phase

KEY = "access-op-7"
BLOCK = utils.ACCESS_BLOCK_LABEL


@pytest.fixture
def store(monkeypatch):
    """Redis 작업 입력 저장소 대역. 키는 (action, request_id)."""
    data = {}

    def save(action, request_id, job):
        if (action, request_id) in data:
            return False
        data[(action, request_id)] = {"state": "queued", "job": job}
        return True

    monkeypatch.setattr(main, "save_job_input", save)
    monkeypatch.setattr(main, "load_job_input", lambda a, r: data.get((a, r)))
    monkeypatch.setattr(main, "mark_job_running", lambda a, r: data[(a, r)].update(state="running"))
    monkeypatch.setattr(main, "delete_job_input", lambda a, r: data.pop((a, r), None))
    return data


@pytest.fixture
def cluster(monkeypatch):
    """클러스터 대역. selectors 는 {Service 이름: 선택자} — alice 의 Pod 두 개에 걸친 Service 세 개다."""
    state = types.SimpleNamespace(
        selectors={"svc-a-ssh": {"pod_name": "ailab-alice-1"}, "svc-a-jupyter": {"pod_name": "ailab-alice-1"},
                   "svc-b-ssh": {"pod_name": "ailab-alice-2"}},
        patched=[], patch_error=None)

    class FakeV1:
        def list_namespaced_service(self, namespace, label_selector):
            assert label_selector == "username=alice,app=ailab-nodeport"
            return types.SimpleNamespace(items=[
                types.SimpleNamespace(metadata=types.SimpleNamespace(name=name),
                                      spec=types.SimpleNamespace(selector=dict(selector)))
                for name, selector in state.selectors.items()])

        def patch_namespaced_service(self, name, namespace, body):
            if state.patch_error and name == state.patch_error:
                raise RuntimeError("apiserver hiccup")
            state.patched.append(name)
            for key, value in body["spec"]["selector"].items():
                if value is None:
                    state.selectors[name].pop(key, None)
                else:
                    state.selectors[name][key] = value

    monkeypatch.setattr(main, "load_k8s", lambda: None)
    monkeypatch.setattr(access.client, "CoreV1Api", FakeV1)
    monkeypatch.setattr(main, "RETRY_DELAY_SEC", 0)
    return state


@pytest.fixture
def saved(monkeypatch):
    results = {}
    monkeypatch.setattr(main, "save_job_result", lambda a, r, d: results.update({(a, r): d}))
    return results


def _run(store, blocked):
    """등록된 것으로 두고 제어기가 하듯 작업을 끝까지 실행한다."""
    store[("CHANGE_ACCESS", KEY)] = {"state": "queued", "job": {"username": "alice", "blocked": blocked}}
    with main.app.app_context():
        main.run_job("access", KEY, "alice")


def _end(logs):
    return logs[-1]["action"], logs[-1]["phase"], logs[-1].get("error_code")


# ---------- 작업 등록 ----------

def test_registration_returns_202_with_prefixed_key(api, logs, store):
    r = api.post("/operations/access", json={"request_id": 7, "username": "alice", "blocked": True})

    assert r.status_code == 202
    assert r.get_json()["request_id"] == "7" and r.get_json()["status"] == "accepted"
    job = {"username": "alice", "blocked": True}
    assert store[("CHANGE_ACCESS", KEY)]["job"] == job
    assert len(logs) == 1 and logs[0]["request_id"] == KEY
    assert logs[0]["action"] == Action.CHANGE_ACCESS and logs[0]["phase"] == Phase.START
    assert json.loads(logs[0]["target_state"]) == job


def test_same_operation_twice_is_409(api, logs, store):
    body = {"request_id": 7, "username": "alice", "blocked": True}
    assert api.post("/operations/access", json=body).status_code == 202
    assert api.post("/operations/access", json=body).status_code == 409


@pytest.mark.parametrize("body", [
    {"request_id": 7, "username": "alice"},
    {"request_id": 7, "blocked": True},
    {"request_id": 0, "username": "alice", "blocked": True},
    {"request_id": 7, "username": "Alice; rm", "blocked": True},
])
def test_invalid_body_is_400(api, logs, store, body):
    assert api.post("/operations/access", json=body).status_code == 400
    assert not store


# ---------- 차단·해제 ----------

def test_block_covers_every_service_of_the_account_and_keeps_them(logs, store, cluster, saved):
    _run(store, blocked=True)

    assert _end(logs) == (Action.CHANGE_ACCESS, Phase.SUCCESS, None)
    assert sorted(cluster.patched) == ["svc-a-jupyter", "svc-a-ssh", "svc-b-ssh"]
    assert cluster.selectors == {
        "svc-a-ssh": {"pod_name": "ailab-alice-1", BLOCK: "true"},
        "svc-a-jupyter": {"pod_name": "ailab-alice-1", BLOCK: "true"},
        "svc-b-ssh": {"pod_name": "ailab-alice-2", BLOCK: "true"}}
    assert saved[("CHANGE_ACCESS", KEY)] == {"blocked": True, "services": 3}


def test_unblock_restores_the_original_selector(logs, store, cluster, saved):
    for selector in cluster.selectors.values():
        selector[BLOCK] = "true"

    _run(store, blocked=False)

    assert _end(logs) == (Action.CHANGE_ACCESS, Phase.SUCCESS, None)
    assert cluster.selectors["svc-b-ssh"] == {"pod_name": "ailab-alice-2"}
    assert all(BLOCK not in selector for selector in cluster.selectors.values())
    assert saved[("CHANGE_ACCESS", KEY)] == {"blocked": False, "services": 3}


def test_services_already_in_the_wanted_state_are_left_alone(logs, store, cluster, saved):
    cluster.selectors["svc-a-ssh"][BLOCK] = "true"

    _run(store, blocked=True)

    assert sorted(cluster.patched) == ["svc-a-jupyter", "svc-b-ssh"]


def test_account_without_containers_succeeds(logs, store, cluster, saved):
    cluster.selectors = {}

    _run(store, blocked=True)

    assert _end(logs) == (Action.CHANGE_ACCESS, Phase.SUCCESS, None)
    assert saved[("CHANGE_ACCESS", KEY)] == {"blocked": True, "services": 0}


def test_transient_failure_is_retried_to_the_end(logs, store, cluster, saved, monkeypatch):
    cluster.patch_error = "svc-b-ssh"
    original = access.client.CoreV1Api.patch_namespaced_service

    def flaky(self, name, namespace, body):
        try:
            return original(self, name, namespace, body)
        finally:
            cluster.patch_error = None
    monkeypatch.setattr(access.client.CoreV1Api, "patch_namespaced_service", flaky)

    _run(store, blocked=True)

    assert _end(logs) == (Action.CHANGE_ACCESS, Phase.SUCCESS, None)
    assert all(selector.get(BLOCK) == "true" for selector in cluster.selectors.values())


def test_persistent_failure_does_not_end_in_success(logs, store, cluster, saved):
    cluster.patch_error = "svc-a-ssh"

    _run(store, blocked=True)

    assert logs[-1]["action"] == Action.CHANGE_ACCESS and logs[-1]["phase"] != Phase.SUCCESS
    assert "ACCESS_CHANGE_FAILED" in {entry.get("error_code") for entry in logs}
    assert ("CHANGE_ACCESS", KEY) not in saved


# ---------- 차단 중에 새로 만들어지는 Service ----------

def _port():
    return {"internal_port": 22, "external_port": 30001, "usage_purpose": "ssh"}


def test_service_of_a_blocked_account_selects_no_pod():
    body = utils.nodeport_service_body("alice", "ns", "ailab-alice-1", _port(), blocked=True)

    assert body.spec.selector == {"pod_name": "ailab-alice-1", BLOCK: "true"}
    assert body.spec.ports[0].node_port == 30001


def test_service_is_open_by_default():
    assert utils.nodeport_service_body("alice", "ns", "ailab-alice-1", _port()).spec.selector \
        == {"pod_name": "ailab-alice-1"}


@pytest.mark.parametrize("path,body,action", [
    ("/operations/migrate", {"request_id": 9, "username": "alice", "pod_name": "ailab-alice-1", "recreate": True},
     "MIGRATE"),
    ("/operations/port", {"request_id": 9, "username": "alice", "pod_name": "ailab-alice-1", "ports": []},
     "CHANGE_PORT"),
])
def test_block_flag_reaches_the_job_that_creates_services(api, logs, store, path, body, action):
    assert api.post(path, json={**body, "access_blocked": True}).status_code == 202

    (key, entry), = store.items()
    assert key[0] == action and entry["job"]["access_blocked"] is True
    kind = main._JOB_KIND[action]
    assert main._job_ctx(kind, key[1], {"nodes": [], **entry["job"]})["access_blocked"] is True


def test_job_without_the_flag_creates_open_services():
    assert main._job_ctx("port", "port-change-1", {"username": "alice", "pod_name": "p", "ports": []})[
        "access_blocked"] is False


def test_external_access_probe_is_skipped_for_a_blocked_account(monkeypatch):
    seen = {}
    monkeypatch.setattr(verify, "_run_probe", lambda ctx, action, name, check: seen.update(result=check(ctx)))
    monkeypatch.setattr(main, "_get_farm_node_info", lambda node: pytest.fail("차단 중에는 접속을 시험하지 않는다"))

    verify.step_verify_endpoint({"access_blocked": True, "node": "farm1", "allocated_ports": [_port()]})

    assert seen["result"] == (None, {"scope": "skip", "reason": "접속 차단 중"})
