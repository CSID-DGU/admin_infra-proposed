"""추가 포트 변경 작업(POST /operations/port) — 등록 검증과, Pod 를 다시 만들지 않고 포트를 맞추는 단계."""
import json
import types

import pytest

import main
from lifecycle_steps import port
from main import Action, Phase

KEY = "port-change-7"
POD = "ailab-alice-7f3a9c21"
NODE = "farm1"


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
    """배정 표와 클러스터 대역. rows 는 {내부 포트: (외부 포트, 용도, 사용자)}, services 는 {내부 포트: Service 이름}."""
    state = types.SimpleNamespace(
        rows={22: (30001, "ssh", "alice"), 8888: (30002, "jupyter", "alice")},
        services={22: "svc-ssh", 8888: "svc-jupyter"},
        pod_exists=True, deleting=False, next_port=30100, create_error=None, created=[], deleted=[])

    class FakeV1:
        def read_namespaced_pod(self, name, namespace):
            if not state.pod_exists:
                raise port.client.exceptions.ApiException(status=404)
            return types.SimpleNamespace(
                metadata=types.SimpleNamespace(deletion_timestamp="now" if state.deleting else None),
                spec=types.SimpleNamespace(node_name=NODE))

        def list_namespaced_service(self, namespace, label_selector):
            assert label_selector == f"pod_name={POD},app=ailab-nodeport"
            return types.SimpleNamespace(items=[
                types.SimpleNamespace(metadata=types.SimpleNamespace(name=name),
                                      spec=types.SimpleNamespace(ports=[types.SimpleNamespace(port=p)]))
                for p, name in state.services.items()])

        def delete_namespaced_service(self, name, namespace):
            state.deleted.append(name)
            state.services = {p: n for p, n in state.services.items() if n != name}

    def allocate(username, pod_name, node_name, ports):
        assert (username, pod_name, node_name) == ("alice", POD, NODE)
        for p in ports:
            state.rows[p["internal_port"]] = (state.next_port, p["usage_purpose"], username)
            state.next_port += 1

    def create(username, namespace, pod_name, ports, blocked=False):
        if state.create_error:
            raise state.create_error
        for p in ports:
            state.created.append((p["internal_port"], p["external_port"]))
            state.services[p["internal_port"]] = f"svc-{p['internal_port']}"

    def release(pod_name, internal_ports):
        for p in internal_ports:
            state.rows.pop(p, None)

    monkeypatch.setattr(main, "load_k8s", lambda: None)
    monkeypatch.setattr(port.client, "CoreV1Api", FakeV1)
    monkeypatch.setattr(port, "_allocations",
                        lambda pod_name: [(p, ext, purpose, user) for p, (ext, purpose, user) in state.rows.items()])
    monkeypatch.setattr(port, "_release", release)
    monkeypatch.setattr(main, "allocate_nodeports", allocate)
    monkeypatch.setattr(main, "create_nodeport_services", create)
    monkeypatch.setattr(main, "RETRY_DELAY_SEC", 0)
    return state


@pytest.fixture
def saved(monkeypatch):
    results = {}
    monkeypatch.setattr(main, "save_job_result", lambda a, r, d: results.update({(a, r): d}))
    return results


def _body(ports, **over):
    return {"request_id": 7, "username": "alice", "pod_name": POD, "ports": ports, **over}


def _run(store, ports, username="alice"):
    """등록된 것으로 두고 제어기가 하듯 작업을 끝까지 실행한다."""
    store[("CHANGE_PORT", KEY)] = {"state": "queued",
                                   "job": {"username": username, "pod_name": POD, "ports": ports}}
    with main.app.app_context():
        main.run_job("port", KEY, username)


def _end(logs):
    return logs[-1]["action"], logs[-1]["phase"], logs[-1].get("error_code")


# ---------- 작업 등록 ----------

def test_registration_returns_202_with_prefixed_key(api, logs, store):
    r = api.post("/operations/port", json=_body([{"internal_port": 3000, "usage_purpose": "웹 서버"}]))

    assert r.status_code == 202
    assert r.get_json()["request_id"] == "7" and r.get_json()["status"] == "accepted"
    job = {"username": "alice", "pod_name": POD, "ports": [{"internal_port": 3000, "usage_purpose": "웹 서버"}]}
    assert store[("CHANGE_PORT", KEY)]["job"] == job
    assert len(logs) == 1 and logs[0]["request_id"] == KEY
    assert logs[0]["action"] == Action.CHANGE_PORT and logs[0]["phase"] == Phase.START
    assert json.loads(logs[0]["target_state"]) == job


def test_empty_list_is_accepted(api, logs, store):
    assert api.post("/operations/port", json=_body([])).status_code == 202


def test_same_operation_twice_is_409(api, logs, store):
    body = _body([{"internal_port": 3000}])
    assert api.post("/operations/port", json=body).status_code == 202
    assert api.post("/operations/port", json=body).status_code == 409


@pytest.mark.parametrize("body", [
    _body([{"internal_port": 22}]),
    _body([{"internal_port": 8888}]),
    _body([{"internal_port": 6080}]),
    _body([{"internal_port": 0}]),
    _body([{"internal_port": 65536}]),
    _body([{"internal_port": "abc"}]),
    _body([{"internal_port": True}]),
    _body([{"internal_port": 3000}, {"internal_port": 3000}]),
    _body([{"internal_port": 3000 + i} for i in range(11)]),
    _body([{"internal_port": 3000, "usage_purpose": "x" * 256}]),
    _body([{"internal_port": 3000, "usage_purpose": "ssh"}]),
    _body([{"internal_port": 3000, "usage_purpose": "Jupyter"}]),
    _body([{"internal_port": 3000, "usage_purpose": "novnc"}]),
    _body([{"internal_port": 3000}], pod_name="other-pod"),
    _body([{"internal_port": 3000}], username="a b; id"),
    _body([{"internal_port": 3000}], request_id="port-1"),
    {"request_id": 7, "username": "alice", "pod_name": POD},
])
def test_invalid_registration_is_400_and_not_stored(api, logs, store, body):
    assert api.post("/operations/port", json=body).status_code == 400
    assert store == {} and logs == []


def test_prefix_guard_covers_registration(api, logs, store, monkeypatch):
    monkeypatch.setattr(main, "ACCOUNT_PREFIX", "exp-np-")
    assert api.post("/operations/port", json=_body([{"internal_port": 3000}])).status_code == 403
    assert store == {} and logs == []


# ---------- 작업 실행 ----------

def test_added_port_gets_external_port_and_service(logs, store, cluster, saved):
    _run(store, [{"internal_port": 3000, "usage_purpose": "web"}])

    assert cluster.created == [(3000, 30100)]
    assert cluster.deleted == []
    assert _end(logs) == (Action.CHANGE_PORT, Phase.SUCCESS, None)
    result = saved[("CHANGE_PORT", KEY)]
    assert result["pod_name"] == POD and result["node"] == NODE
    assert result["ports"] == [
        {"internal_port": 22, "external_port": 30001, "usage_purpose": "ssh"},
        {"internal_port": 8888, "external_port": 30002, "usage_purpose": "jupyter"},
        {"internal_port": 3000, "external_port": 30100, "usage_purpose": "web"},
    ]


def test_removed_port_loses_service_and_allocation(logs, store, cluster, saved):
    cluster.rows[3000] = (30050, "web", "alice")
    cluster.services[3000] = "svc-web"

    _run(store, [])

    assert cluster.deleted == ["svc-web"] and 3000 not in cluster.rows
    assert cluster.created == []
    assert [p["internal_port"] for p in saved[("CHANGE_PORT", KEY)]["ports"]] == [22, 8888]


def test_unchanged_port_keeps_its_external_port(logs, store, cluster, saved):
    cluster.rows[3000] = (30050, "web", "alice")
    cluster.services[3000] = "svc-web"

    _run(store, [{"internal_port": 3000, "usage_purpose": "web"}, {"internal_port": 5000, "usage_purpose": "api"}])

    assert cluster.deleted == [] and cluster.created == [(5000, 30100)]
    assert cluster.rows[3000][0] == 30050


def test_base_and_vnc_ports_are_never_touched(logs, store, cluster, saved):
    cluster.rows[6080] = (30060, "novnc", "alice")
    cluster.services[6080] = "svc-vnc"

    _run(store, [])

    assert cluster.deleted == [] and set(cluster.rows) == {22, 8888, 6080}
    assert _end(logs) == (Action.CHANGE_PORT, Phase.SUCCESS, None)


def test_rerun_creates_service_left_missing_by_an_interrupted_attempt(logs, store, cluster, saved):
    cluster.rows[3000] = (30050, "web", "alice")  # 배정까지만 되고 Service 를 못 만든 채 끊겼다

    _run(store, [{"internal_port": 3000, "usage_purpose": "web"}])

    assert cluster.created == [(3000, 30050)]
    assert _end(logs) == (Action.CHANGE_PORT, Phase.SUCCESS, None)


def test_missing_pod_fails_without_changes(logs, store, cluster, saved):
    cluster.pod_exists = False

    _run(store, [{"internal_port": 3000, "usage_purpose": "web"}])

    assert cluster.created == [] and set(cluster.rows) == {22, 8888}
    assert _end(logs) == (Action.CHANGE_PORT, Phase.FAIL, "POD_NOT_FOUND")
    assert saved == {}


def test_pod_being_deleted_fails(logs, store, cluster, saved):
    cluster.deleting = True

    _run(store, [{"internal_port": 3000, "usage_purpose": "web"}])

    assert cluster.created == []
    assert _end(logs) == (Action.CHANGE_PORT, Phase.FAIL, "POD_NOT_FOUND")


def test_pod_of_another_user_is_refused(logs, store, cluster, saved):
    cluster.rows = {22: (30001, "ssh", "bob"), 8888: (30002, "jupyter", "bob")}

    _run(store, [{"internal_port": 3000, "usage_purpose": "web"}])

    assert cluster.created == [] and 3000 not in cluster.rows
    assert _end(logs) == (Action.CHANGE_PORT, Phase.FAIL, "POD_OWNER_MISMATCH")


def test_service_create_failure_is_not_reported_as_success(logs, store, cluster, saved):
    cluster.create_error = RuntimeError("boom")

    _run(store, [{"internal_port": 3000, "usage_purpose": "web"}])

    assert logs[-1]["action"] == Action.CHANGE_PORT and logs[-1]["phase"] != Phase.SUCCESS
    assert "PORT_CHANGE_FAILED" in {entry.get("error_code") for entry in logs}
    assert saved == {}
