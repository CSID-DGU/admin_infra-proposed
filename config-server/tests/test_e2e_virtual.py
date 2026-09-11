"""가상 E2E (#25): 외부 시스템(k8s·SSH·WAS·Redis·DB)만 가짜로 두고 실제 단계 함수·엔드포인트·작업 SQL을 돈다.
DB는 sqlite로 operation_log를 만들어 find_unfinished_jobs / get_job_result SQL을 그대로 실행한다."""
import base64
import sqlite3
import subprocess
import types

import pytest
from kubernetes.client.exceptions import ApiException

import main
from main import Action, Phase

PW = base64.b64encode(b"pw-e2e").decode()


class FakeV1:
    def __init__(self):
        self.pods = {}

    def read_namespaced_pod(self, name, ns):
        if name not in self.pods:
            raise ApiException(status=404, reason="Not Found")
        return self.pods[name]

    def create_namespaced_pod(self, namespace, body):
        name = body["metadata"]["name"]
        self.pods[name] = types.SimpleNamespace(
            metadata=types.SimpleNamespace(name=name, labels=body["metadata"]["labels"]),
            spec=types.SimpleNamespace(node_name=body["spec"]["nodeName"]), body=body)

    def list_namespaced_pod(self, ns, label_selector):
        key, value = label_selector.split("=")
        return types.SimpleNamespace(items=[p for p in self.pods.values() if p.metadata.labels.get(key) == value])

    def delete_namespaced_pod(self, name, ns):
        if name not in self.pods:
            raise ApiException(status=404, reason="Not Found")
        del self.pods[name]


class Sql:
    """pymysql 연결 흉내. %s → ? 만 바꿔 sqlite에서 같은 SQL을 실행한다."""

    def __init__(self, db):
        self.db = db

    def cursor(self):
        outer = self

        class Cur:
            def __enter__(self):
                self.c = outer.db.cursor()
                return self

            def __exit__(self, *a):
                pass

            def execute(self, sql, params=()):
                self.c.execute(sql.replace("%s", "?"), params)

            def fetchall(self):
                return self.c.fetchall()

            def fetchone(self):
                return self.c.fetchone()
        return Cur()

    def commit(self):
        self.db.commit()

    def close(self):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute("""CREATE TABLE operation_log (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, username TEXT,
        pod_name TEXT, node_name TEXT, resource_type TEXT, action TEXT, phase TEXT, attempt INT DEFAULT 1,
        duration_ms INT, error_code TEXT, error_detail TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    e = types.SimpleNamespace(db=db, v1=FakeV1(), redis={}, status={}, calls=[], fail_end_log=False, was=None)

    def log_operation(**kw):
        v = lambda x: x.value if hasattr(x, "value") else x
        if e.fail_end_log and kw["action"] in (Action.PROVISION, Action.REVOKE) and kw["phase"] != Phase.START:
            if kw.get("raise_errors"):
                raise RuntimeError("log db down")
            return  # log_operation은 raise_errors가 없으면 실패를 삼킨다
        db.execute("INSERT INTO operation_log (request_id, username, pod_name, node_name, resource_type, action,"
                   " phase, error_code, error_detail) VALUES (?,?,?,?,?,?,?,?,?)",
                   (str(kw["request_id"]), kw["username"], kw.get("pod_name"), kw.get("node_name"),
                    kw.get("resource_type"), v(kw["action"]), v(kw["phase"]), kw.get("error_code"), kw.get("error_detail")))
        db.commit()

    monkeypatch.setattr(main, "log_operation", log_operation)
    monkeypatch.setattr(main, "get_log_db_connection", lambda: Sql(db))

    # Redis
    def save(a, r, job):
        if (a, r) in e.redis:
            return False
        e.redis[(a, r)] = {"state": "queued", "job": job}
        return True
    monkeypatch.setattr(main, "save_job_input", save)
    monkeypatch.setattr(main, "load_job_input", lambda a, r: e.redis.get((a, r)))
    monkeypatch.setattr(main, "mark_job_running", lambda a, r: e.redis[(a, r)].update(state="running"))
    monkeypatch.setattr(main, "delete_job_input", lambda a, r: e.redis.pop((a, r), None))
    monkeypatch.setattr(main, "mark_job_done", lambda a, r, res: e.redis.setdefault((a, r), {}).update(state="done", result=res))
    monkeypatch.setattr(main, "set_pod_creation_status", lambda k, st, m="": e.status.__setitem__(str(k), st))
    monkeypatch.setattr(main, "get_pod_creation_status", lambda k: {"stage": e.status.get(str(k))})

    # 계정 파일은 임시 폴더의 실제 파일
    base = str(tmp_path / "etc")
    for k, val in list(main.app.config.items()):
        if isinstance(val, str) and val.startswith(main.BASE_ETC_DIR):
            monkeypatch.setitem(main.app.config, k, base + val[len(main.BASE_ETC_DIR):])
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "TEST.REALM")
    monkeypatch.setattr(main, "UID_MIN", 50000)
    monkeypatch.setattr(main, "UID_MAX", 54999)

    rec = lambda name, ret=None: (lambda *a, **k: (e.calls.append((name, a)), ret)[1])
    monkeypatch.setattr(main, "create_user_home_directory", rec("create_home"))
    monkeypatch.setattr(main, "delete_user_home_directory", rec("delete_home"))
    monkeypatch.setattr(main, "_create_krb5_principal_and_secret", rec("krb5_principal"))
    monkeypatch.setattr(main, "_delete_krb5_principal_and_secret", rec("krb5_principal_delete"))
    monkeypatch.setattr(main, "_deploy_krb5_to_farm", rec("krb5_deploy"))
    monkeypatch.setattr(main, "_remove_krb5_from_farm", rec("krb5_remove"))
    monkeypatch.setattr(main, "_remove_krb5_from_all_farms", rec("krb5_remove_all"))
    monkeypatch.setattr(main, "_record_krb5_cleanup_pending", rec("krb5_pending"))
    monkeypatch.setattr(main, "allocate_nodeports", lambda username, pod_name, node_name, ports: (
        e.calls.append(("allocate", pod_name)),
        [dict(p, external_port=32000 + i) for i, p in enumerate(ports)])[1])
    monkeypatch.setattr(main, "release_nodeports", rec("release"))
    monkeypatch.setattr(main, "create_nodeport_services", rec("svc_create"))
    monkeypatch.setattr(main, "delete_nodeport_services", rec("svc_delete"))
    monkeypatch.setattr(main, "wait_for_pod_deleted", lambda *a, **k: True)
    monkeypatch.setattr(main, "load_k8s", lambda: None)
    monkeypatch.setattr(main.client, "CoreV1Api", lambda: e.v1)
    monkeypatch.setattr(main, "select_best_node_from_prometheus", lambda nodes, url, t: nodes[0])
    monkeypatch.setattr(main, "resolve_k8s_node_name", lambda n: n)
    monkeypatch.setattr(main, "resolve_farm_home_mount_root", lambda n: "/home/share/user")
    monkeypatch.setattr(main, "load_user_image", lambda u, img: img)
    monkeypatch.setattr(main, "is_pod_ready", lambda pod: True)
    monkeypatch.setattr(main, "get_pod_failure_reason", lambda pod: None)
    monkeypatch.setattr(main, "get_pod_progress_stage", lambda *a: None)

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._b = code, body

        def json(self):
            return self._b

    def was(url, timeout):
        e.calls.append(("was", url))
        if e.was:
            return e.was(url)
        return Resp(200, {"image": "dguailab/decs:1", "passwd_base64": PW, "gpu_nodes": [
            {"node_name": "farm2", "num_gpu": 1, "cpu_limit": "4", "memory_limit": "16Gi"}]})
    monkeypatch.setattr(main.requests, "get", was)
    e.Resp = Resp
    e.api = main.app.test_client()
    return e


def tick(e):
    """제어기 한 바퀴: 끝나지 않은 작업을 찾아 실행."""
    with main.app.app_context():
        for kind, rid, user in main.find_unfinished_jobs():
            main.run_job(kind, rid, user)


def rows(e, rid):
    return [(a, p) for a, p in e.db.execute(
        "SELECT action, phase FROM operation_log WHERE request_id=? ORDER BY id", (rid,))]


def passwd_names():
    with main.app.app_context():
        return [l.split(":")[0] for l in main.read_passwd_lines()]


def passwd_line(name):
    with main.app.app_context():
        return next(l for l in main.read_passwd_lines() if l.startswith(name + ":"))


def shadow_line(name):
    with main.app.app_context():
        return next(l for l in main.read_shadow_lines() if l.startswith(name + ":"))


def result(e, kind, rid):
    return e.api.get(f"/operations/{kind}/{rid}").get_json()


# ---------- 시나리오 ----------

def test_provision_then_revoke_full_flow(env):
    e = env
    r = e.api.post("/operations/provision", json={"request_id": "101", "username": "exp-np-e2e",
                                                  "account": {"passwd_base64": PW}})
    assert r.status_code == 202 and result(e, "provision", "101")["phase"] == "START"

    tick(e)

    assert result(e, "provision", "101")["phase"] == "SUCCESS", rows(e, "101")
    steps = [a for a, p in rows(e, "101") if p == "SUCCESS"]
    assert steps == ["CREATE_ACCOUNT", "CREATE_HOME", "CREATE_KRB5_PRINCIPAL", "FETCH_USER_CONFIG", "SELECT_NODE",
                     "ALLOCATE_NODEPORT", "DEPLOY_KRB5", "CREATE_POD_K8S", "WAIT_READY", "CREATE_SERVICE", "PROVISION"]
    assert "exp-np-e2e" in passwd_names()
    uid = int(passwd_line("exp-np-e2e").split(":")[2])
    assert 50000 <= uid <= 54999
    shadow = shadow_line("exp-np-e2e")
    assert shadow.split(":")[1].startswith("$6$")
    assert e.status["101"] == "ready" and e.redis == {}
    pod_name = next(iter(e.v1.pods))

    r = e.api.post("/operations/revoke", json={"request_id": "101", "pod_name": pod_name, "delete_account": True})
    assert r.status_code == 202
    tick(e)

    assert result(e, "revoke", "101")["phase"] == "SUCCESS", rows(e, "101")
    assert e.v1.pods == {} and "exp-np-e2e" not in passwd_names()
    names = [c[0] for c in e.calls]
    assert "delete_home" not in names                     # 홈 보존
    assert ("krb5_remove", ("exp-np-e2e", "farm2")) in e.calls   # 지운 Pod의 노드에서 keytab 정리
    assert "krb5_principal_delete" in names
    was_urls = [c[1] for c in e.calls if c[0] == "was"]
    assert was_urls == ["http://admin-prod.default/api/requests/config/by-request/101"]   # 제어기는 신청 번호로 조회


def test_duplicate_registration_and_retry_after_finish(env):
    e = env
    body = {"request_id": "102", "username": "exp-np-e2e", "account": {"passwd_base64": PW}}
    assert e.api.post("/operations/provision", json=body).status_code == 202
    assert e.api.post("/operations/provision", json=body).status_code == 409
    e.was = lambda url: e.Resp(404, {"status": 404})
    tick(e)
    assert result(e, "provision", "102")["error_code"] == "USER_CONFIG_NOT_FOUND"
    assert e.status["102"] == "failed"
    # 노드 선택 전에 실패해 keytab 노드를 모르므로 baseline처럼 계정 되돌리기를 보류한다
    assert "exp-np-e2e" in passwd_names()
    assert "held:ACCOUNT_NODE_UNKNOWN" in e.db.execute(
        "SELECT error_detail FROM operation_log WHERE request_id='102' AND action='PROVISION' AND phase='FAIL'").fetchone()[0]
    # 끝난 뒤에는 다시 등록할 수 있다(계정이 남아 있으니 account 없이)
    e.was = None
    assert e.api.post("/operations/provision", json={"request_id": "102", "username": "exp-np-e2e"}).status_code == 202
    tick(e)
    assert result(e, "provision", "102")["phase"] == "SUCCESS"


def test_farm_timeout_is_unknown_and_ports_are_released(env, monkeypatch):
    e = env
    def timeout(*a):
        raise subprocess.TimeoutExpired("ssh", 60)
    monkeypatch.setattr(main, "_deploy_krb5_to_farm", timeout)
    e.api.post("/operations/provision", json={"request_id": "103", "username": "exp-np-e2e",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    assert ("DEPLOY_KRB5", "UNKNOWN") in rows(e, "103")
    assert result(e, "provision", "103")["phase"] == "UNKNOWN"
    assert any(c[0] == "release" for c in e.calls) and e.v1.pods == {}


def test_restart_marks_running_job_failed_and_keeps_queued(env):
    e = env
    e.api.post("/operations/provision", json={"request_id": "104", "username": "exp-np-e2e",
                                              "account": {"passwd_base64": PW}})
    e.api.post("/operations/provision", json={"request_id": "105", "username": "exp-np-e2f",
                                              "account": {"passwd_base64": PW}})
    e.redis[("PROVISION", "104")]["state"] = "running"     # 실행 중에 제어기가 죽은 상황
    with main.app.app_context():
        main.mark_interrupted_jobs()
    assert result(e, "provision", "104")["error_code"] == "CONTROLLER_RESTARTED"
    tick(e)
    assert result(e, "provision", "105")["phase"] == "SUCCESS"
    assert "exp-np-e2e" not in passwd_names()             # 중단된 작업은 다시 실행되지 않음


def test_sync_path_still_works_end_to_end(env):
    e = env
    r = e.api.put("/accounts/users", json={"name": "exp-np-sync", "passwd_base64": PW, "request_id": "200"})
    assert r.status_code == 201 and r.get_json()["user"]["name"] == "exp-np-sync"
    r = e.api.post("/create-pod", json={"username": "exp-np-sync", "request_id": "200"})
    assert r.status_code == 201 and r.get_json()["node"] == "farm2" and len(r.get_json()["ports"]) == 2
    r = e.api.post("/delete-pod", json={"pod_name": r.get_json()["pod_name"], "request_id": "200"})
    assert r.status_code == 200 and r.get_json()["progress"]["podDeleted"]
    r = e.api.delete("/accounts/users/exp-np-sync?node_name=farm2&request_id=200")
    assert r.status_code == 200
    assert "delete_home" in [c[0] for c in e.calls]       # 동기 경로는 baseline대로 홈 삭제
    assert [c[1] for c in e.calls if c[0] == "was"] == ["http://admin-prod.default/api/requests/config/exp-np-sync"]
    assert e.api.delete("/accounts/users/exp-np-sync").status_code == 404
    assert not [1 for a, p in rows(e, "200") if a in ("PROVISION", "REVOKE")]  # 동기 경로는 작업 행을 안 남김


# ---------- 가상 E2E로 찾은 문제의 수정 확인 (#25) ----------

def test_result_row_loss_is_retried_not_turned_into_failure(env):
    e = env
    e.api.post("/operations/provision", json={"request_id": "300", "username": "exp-np-e2e",
                                              "account": {"passwd_base64": PW}})
    e.fail_end_log = True
    tick(e)          # 단계는 전부 성공, 결과 행 기록만 실패
    assert result(e, "provision", "300")["phase"] == "START"
    e.fail_end_log = False
    tick(e)          # 다음 바퀴: 결과 행만 다시 기록
    assert result(e, "provision", "300")["phase"] == "SUCCESS"
    assert len(e.v1.pods) == 1 and e.redis == {}


def test_account_in_use_by_another_pod_is_not_revoked(env):
    e = env
    e.api.post("/operations/provision", json={"request_id": "400", "username": "exp-np-e2e",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    e.api.post("/operations/provision", json={"request_id": "401", "username": "exp-np-e2e"})
    tick(e)
    assert len(e.v1.pods) == 2
    first = sorted(e.v1.pods)[0]
    e.api.post("/operations/revoke", json={"request_id": "400", "pod_name": first, "delete_account": True})
    tick(e)
    res = result(e, "revoke", "400")
    assert res["phase"] == "FAIL" and res["error_code"] == "ACCOUNT_IN_USE"
    assert len(e.v1.pods) == 1 and "exp-np-e2e" in passwd_names()    # 남은 컨테이너의 계정 유지


def test_non_numeric_request_id_is_rejected(env):
    r = env.api.post("/operations/provision", json={"request_id": "smoke-1", "username": "exp-np-e2e"})
    assert r.status_code == 400 and env.redis == {}


# ---------- baseline 보상 조건 ----------

def test_pod_failure_after_node_selection_rolls_back_account_but_keeps_home(env, monkeypatch):
    e = env
    def broken(namespace, body):
        raise ApiException(status=500, reason="quota exceeded")
    monkeypatch.setattr(e.v1, "create_namespaced_pod", broken)
    e.api.post("/operations/provision", json={"request_id": "500", "username": "exp-np-e2e",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    res = result(e, "provision", "500")
    assert res["phase"] == "FAIL" and res["error_code"] == "POD_CREATE_FAILED"
    assert "exp-np-e2e" not in passwd_names()                       # 계정 되돌림
    names = [c[0] for c in e.calls]
    assert "delete_home" not in names                                # 홈은 보존
    assert ("krb5_remove", ("exp-np-e2e", "farm2")) in e.calls and "krb5_remove_all" not in names
    assert ("DELETE_ACCOUNT", "SUCCESS") in rows(e, "500")


def test_rollback_held_when_user_has_another_pod(env):
    """계정을 만든 뒤 Pod 단계가 실패했는데 같은 사용자의 다른 컨테이너가 이미 떠 있으면 되돌리지 않는다."""
    e = env
    e.api.post("/operations/provision", json={"request_id": "600", "username": "exp-np-e2e",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    assert len(e.v1.pods) == 1
    ctx = {"request_id": "601", "node": "farm2", "pod_name": "ailab-exp-np-e2e-failed"}
    with main.app.app_context():
        outcome = main._compensate_provision("provision", {"username": "exp-np-e2e"}, ctx,
                                             done=list(main.ACCOUNT_CREATE_STEPS))
    assert outcome == "held:ACCOUNT_IN_USE"
    assert "exp-np-e2e" in passwd_names() and len(e.v1.pods) == 1


def test_revoke_of_absent_pod_without_node_is_held_not_scanning_all_farms(env):
    e = env
    e.api.post("/operations/provision", json={"request_id": "700", "username": "exp-np-e2e",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    e.v1.pods.clear()                                                # Pod가 이미 사라진 상황
    e.api.post("/operations/revoke", json={"request_id": "700", "pod_name": "ailab-exp-np-e2e-gone",
                                           "delete_account": True})
    tick(e)
    res = result(e, "revoke", "700")
    assert res["phase"] == "FAIL" and res["error_code"] == "ACCOUNT_NODE_UNKNOWN"
    assert "exp-np-e2e" in passwd_names()
    assert "krb5_remove_all" not in [c[0] for c in e.calls]
    # 노드를 알려 주면 회수된다
    e.api.post("/operations/revoke", json={"request_id": "700", "username": "exp-np-e2e", "node_name": "farm2",
                                           "delete_account": True})
    tick(e)
    assert result(e, "revoke", "700")["phase"] == "SUCCESS" and "exp-np-e2e" not in passwd_names()
