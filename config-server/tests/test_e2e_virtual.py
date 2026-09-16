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
        self.secrets = {}

    # Pod 로그인 비밀번호 Secret(<pod>-account)
    def create_namespaced_secret(self, namespace, body):
        name = body.metadata.name
        if name in self.secrets:
            raise ApiException(status=409, reason="Conflict")
        self.secrets[name] = {"data": dict(body.string_data or {}), "owners": []}

    def read_namespaced_secret(self, name, namespace):
        if name not in self.secrets:
            raise ApiException(status=404, reason="Not Found")
        data = {k: base64.b64encode(str(v).encode()).decode() for k, v in self.secrets[name]["data"].items()}
        return types.SimpleNamespace(data=data)

    def replace_namespaced_secret(self, name, namespace, body):
        self.secrets[name] = {"data": dict(body.string_data or {}), "owners": []}

    def patch_namespaced_secret(self, name, namespace, body):
        self.secrets[name]["owners"] = body["metadata"]["ownerReferences"]

    def delete_namespaced_secret(self, name, namespace):
        if name not in self.secrets:
            raise ApiException(status=404, reason="Not Found")
        del self.secrets[name]

    def read_namespaced_pod(self, name, ns):
        if name not in self.pods:
            raise ApiException(status=404, reason="Not Found")
        return self.pods[name]

    def create_namespaced_pod(self, namespace, body):
        name = body["metadata"]["name"]
        self.pods[name] = types.SimpleNamespace(
            metadata=types.SimpleNamespace(name=name, labels=body["metadata"]["labels"], uid=f"uid-{name}"),
            spec=types.SimpleNamespace(node_name=body["spec"]["nodeName"]), body=body)
        return self.pods[name]

    def list_namespaced_pod(self, ns, label_selector):
        key, value = label_selector.split("=")
        return types.SimpleNamespace(items=[p for p in self.pods.values() if p.metadata.labels.get(key) == value])

    def delete_namespaced_pod(self, name, ns):
        if name not in self.pods:
            raise ApiException(status=404, reason="Not Found")
        del self.pods[name]

    def list_namespaced_service(self, ns, label_selector=None):
        return types.SimpleNamespace(items=[])


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
    db.execute("""CREATE TABLE operation_log (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INT, request_id TEXT,
        username TEXT, pod_name TEXT, node_name TEXT, resource_type TEXT, action TEXT, phase TEXT, attempt INT DEFAULT 1,
        duration_ms INT, error_code TEXT, error_detail TEXT, target_state TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    e = types.SimpleNamespace(db=db, v1=FakeV1(), redis={}, status={}, calls=[], fail_end_log=False, was=None)

    def log_operation(**kw):
        v = lambda x: x.value if hasattr(x, "value") else x
        if e.fail_end_log and kw["action"] in (Action.PROVISION, Action.REVOKE) and kw["phase"] != Phase.START:
            if kw.get("raise_errors"):
                raise RuntimeError("log db down")
            return  # log_operation은 raise_errors가 없으면 실패를 삼킨다
        job_id = kw.get("job_id") or main.current_job_id.get()
        cur = db.execute("INSERT INTO operation_log (job_id, request_id, username, pod_name, node_name, resource_type,"
                         " action, phase, error_code, error_detail, target_state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (job_id, str(kw["request_id"]), kw["username"], kw.get("pod_name"), kw.get("node_name"),
                          kw.get("resource_type"), v(kw["action"]), v(kw["phase"]), kw.get("error_code"),
                          kw.get("error_detail"), kw.get("target_state")))
        if kw.get("start_job"):
            db.execute("UPDATE operation_log SET job_id=? WHERE id=?", (cur.lastrowid, cur.lastrowid))
        db.commit()
        return cur.lastrowid

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
    e.job_results = {}
    monkeypatch.setattr(main, "save_job_result", lambda a, r, d: e.job_results.__setitem__((a, r), d))
    monkeypatch.setattr(main, "load_job_result", lambda a, r: e.job_results.get((a, r)))
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
        for kind, rid, user, job_id in main.find_unfinished_jobs():
            main.run_job(kind, rid, user, job_id)


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
    # 작업 번호: 생성 작업의 모든 행이 같은 번호, 회수 작업은 다른 번호. 목표 상태에 비밀번호 해시 없음
    jobs = e.db.execute("SELECT action, job_id, target_state FROM operation_log WHERE request_id='101'").fetchall()
    create_ids = {j for a, j, t in jobs if a not in ("REVOKE", "DELETE_SERVICE", "RELEASE_NODEPORT", "DELETE_POD_K8S",
                                                     "DELETE_ACCOUNT", "REMOVE_KRB5")}
    revoke_ids = {j for a, j, t in jobs if a in ("REVOKE", "DELETE_SERVICE", "RELEASE_NODEPORT", "DELETE_POD_K8S",
                                                 "DELETE_ACCOUNT", "REMOVE_KRB5")}
    assert len(create_ids) == 1 and len(revoke_ids) == 1 and create_ids != revoke_ids and None not in create_ids
    start_target = next(t for a, j, t in jobs if a == "PROVISION" and t)
    assert "exp-np-e2e" in start_target and "$6$" not in start_target and "passwd_hash" not in start_target
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
    # 같은 신청을 다시 처리하면 작업 번호로 두 시도가 구분된다
    starts = e.db.execute("SELECT job_id FROM operation_log WHERE request_id='102' AND action='PROVISION' AND phase='START'").fetchall()
    assert len({j for (j,) in starts}) == 2


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


def test_restart_resumes_interrupted_job_to_completion(env, lease_env):
    """제어기 사망 후 재시작(C08): 실패로 마감하지 않고 인수해 끝까지 완료한다."""
    e = env
    e.api.post("/operations/provision", json={"request_id": "104", "username": "exp-np-e2e",
                                              "account": {"passwd_base64": PW}})
    e.redis[("PROVISION", "104")]["state"] = "running"     # 실행 중에 제어기가 죽은 상황
    tick(e)                                                # 새 제어기의 첫 바퀴
    assert result(e, "provision", "104")["phase"] == "SUCCESS"
    assert "exp-np-e2e" in passwd_names() and len(e.v1.pods) == 1


def test_restart_resumes_mid_pod_creation_without_duplicates(env, lease_env):
    """계정 3단계와 노드 선택까지 끝내고 죽은 작업 — 저장된 pod_name·uid로 이어가고,
    끝난 단계(계정 생성)는 다시 실행하지 않아 중복이 없다."""
    e = env
    # 죽은 제어기가 계정 3단계까지 실제로 끝낸 상태를 재현 (파일·홈·principal은 남아 있다)
    ctx = {"request_id": "106", "name": "exp-np-e2g", "pg_name": "exp-np-e2g", "supp_groups": [], "gecos": "",
           "plaintext_pw": base64.b64decode(PW).decode()}
    with main.app.app_context():
        for step in main.ACCOUNT_CREATE_STEPS:
            step(ctx)
    uid = ctx["uid"]
    r = e.api.post("/operations/provision", json={"request_id": "106", "username": "exp-np-e2g",
                                                  "account": {"passwd_base64": PW}})
    jid = r.get_json()["job_id"]
    e.redis[("PROVISION", "106")]["state"] = "running"
    lease_env[jid] = {"owner": "dead-controller", "alive": False,
                      "done": ["step_create_account", "step_create_home", "step_create_krb5_principal",
                               "step_prepare_pod", "step_select_node"],
                      "ctx": {"uid": uid, "gid": uid, "pod_name": "ailab-exp-np-e2g-saved01",
                              "node": "farm2"}}
    made_before = len([c for c in e.calls if c[0] == "krb5_principal"])

    tick(e)

    assert result(e, "provision", "106")["phase"] == "SUCCESS", rows(e, "106")
    assert list(e.v1.pods) == ["ailab-exp-np-e2g-saved01"]   # 저장된 이름 그대로, 한 개만
    # 끝난 계정 단계는 재실행하지 않았다 — principal 생성 호출이 늘지 않는다
    assert len([c for c in e.calls if c[0] == "krb5_principal"]) == made_before
    assert ("krb5_deploy" in [c[0] for c in e.calls])        # 남은 단계는 실행됐다


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


def test_keytab_kept_when_user_has_another_pod_on_same_node(env, lease_env):
    """실측 회귀(9/11 exp-np-probeyoon6yo): 같은 사용자의 Pod 두 개가 한 노드에 있을 때 하나를 지우면
    keytab까지 지워져, 살아있는 Pod의 TGT가 만료 후 갱신되지 않고 홈 접근이 끊겼다."""
    e = env
    e.api.post("/operations/provision", json={"request_id": "900", "username": "exp-np-two",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    e.api.post("/operations/provision", json={"request_id": "901", "username": "exp-np-two"})
    tick(e)
    assert len(e.v1.pods) == 2
    first, second = sorted(e.v1.pods)
    before = [c for c in e.calls if c[0] == "krb5_remove"]

    e.api.post("/operations/revoke", json={"request_id": "900", "pod_name": first})
    tick(e)

    assert result(e, "revoke", "900")["phase"] == "SUCCESS"
    assert list(e.v1.pods) == [second]                      # 지운 것만 사라진다
    assert [c for c in e.calls if c[0] == "krb5_remove"] == before   # keytab은 유지

    # 마지막 Pod를 지울 때는 정리된다
    e.api.post("/operations/revoke", json={"request_id": "901", "pod_name": second})
    tick(e)
    assert ("krb5_remove", ("exp-np-two", "farm2")) in e.calls
    assert e.v1.pods == {}


def test_non_numeric_request_id_is_rejected(env):
    r = env.api.post("/operations/provision", json={"request_id": "smoke-1", "username": "exp-np-e2e"})
    assert r.status_code == 400 and env.redis == {}


# ---------- baseline 보상 조건 ----------

def test_pod_failure_is_retried_then_handed_off_as_degraded(env, monkeypatch):
    """5xx Pod 생성 실패: 재시도 후에도 안 되면 자원을 임의로 되돌리지 않고 DEGRADED로 이관한다(v2.1).
    (구버전은 즉시 FAIL + 계정 되돌리기 — 재시도 도입으로 소진 시 이관으로 바뀌었다)"""
    e = env
    def broken(namespace, body):
        raise ApiException(status=500, reason="quota exceeded")
    monkeypatch.setattr(e.v1, "create_namespaced_pod", broken)
    e.api.post("/operations/provision", json={"request_id": "500", "username": "exp-np-e2e",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    res = result(e, "provision", "500")
    assert res["phase"] == "FAIL" and res["error_code"] == "DEGRADED"
    assert len([1 for a, p in rows(e, "500") if p == "RETRY"]) == 2   # 3회 시도
    assert "exp-np-e2e" in passwd_names()                             # 계정은 점검용으로 보존
    assert "delete_home" not in [c[0] for c in e.calls]
    detail = e.db.execute("SELECT error_detail FROM operation_log WHERE request_id='500'"
                          " AND action='PROVISION' AND phase='FAIL'").fetchone()[0]
    assert "POD_CREATE_FAILED" in detail and "inspect" in detail      # 점검 근거를 남긴다


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
                                             done=[s.__name__ for s in main.ACCOUNT_CREATE_STEPS])
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


def test_provision_result_carries_created_resources(env):
    """작업 결과 조회가 admin_be가 신청 기록에 반영할 값을 함께 돌려준다."""
    e = env
    e.api.post("/operations/provision", json={"request_id": "111", "username": "exp-np-res",
                                              "account": {"passwd_base64": PW}})
    tick(e)

    res = result(e, "provision", "111")
    assert res["phase"] == "SUCCESS", rows(e, "111")
    made = res["result"]
    uid = int(passwd_line("exp-np-res").split(":")[2])
    gid = int(passwd_line("exp-np-res").split(":")[3])
    assert (made["uid"], made["gid"]) == (uid, gid)
    assert made["pod_name"] == next(iter(e.v1.pods)) and made["node"] == "farm2"
    # 포트는 admin_be가 신청 기록에 그대로 저장하는 형식(internal_port·external_port·usage_purpose)이다.
    assert sorted(p["usage_purpose"] for p in made["ports"]) == ["jupyter", "ssh"]
    assert all(p["external_port"] and p["internal_port"] for p in made["ports"])


def test_failed_provision_has_no_result(env):
    """실패한 작업은 만든 자원이 없으므로 result가 비어 있다."""
    e = env
    e.was = lambda url, timeout: e.Resp(404, {"error": "not found"})
    e.api.post("/operations/provision", json={"request_id": "112", "username": "exp-np-res2",
                                              "account": {"passwd_base64": PW}})
    tick(e)

    res = result(e, "provision", "112")
    assert res["phase"] in ("FAIL", "UNKNOWN") and res["result"] is None


def test_pod_gets_priority_class(env):
    """노드 디스크가 쪼들리면 kubelet이 Pod를 쫓아낸다. 우선순위가 비어 있으면 사용자 컨테이너가
    가장 먼저 밀려난다 — 실제로 그렇게 축출돼 마이그레이션이 실패했다."""
    e = env
    e.api.post("/operations/provision", json={"request_id": "114", "username": "exp-np-prio",
                                              "account": {"passwd_base64": PW}})
    tick(e)

    pod = next(p for p in e.v1.pods.values() if "exp-np-prio" in p.metadata.name)
    assert pod.body["spec"]["priorityClassName"] == main.app.config["POD_PRIORITY_CLASS"]


def test_pod_env_carries_ticket_cache_path(env):
    """홈은 인증이 필요한 NFS라 노드 쪽 준비 직후 한순간 쓸 수 없다. entrypoint는 그때 티켓 캐시
    경로가 없으면 기동을 실패로 끝내므로, Pod에 그 경로를 넘긴다."""
    e = env
    e.api.post("/operations/provision", json={"request_id": "113", "username": "exp-np-krb5cc",
                                              "account": {"passwd_base64": PW}})
    tick(e)

    pod = next(p for p in e.v1.pods.values() if "exp-np-krb5cc" in p.metadata.name)
    env_map = {v["name"]: v.get("value") for v in pod.body["spec"]["containers"][0]["env"]}
    assert env_map["KRB5CCNAME"] == f"FILE:/run/user/{env_map['UID']}/krb5cc_ailab"


# ---------- Proposed-Full (VERIFY_MODE=full) ----------

@pytest.fixture
def full(env, monkeypatch):
    """VERIFY_MODE=full + probe 관찰 도구 대역. 기본값은 다섯 시험 전부 통과."""
    from lifecycle_steps import verify
    e = env
    monkeypatch.setattr(main, "VERIFY_MODE", "full")

    def sh(pod, cmd):
        if "id -u" in cmd:
            u = cmd.split("/bin/sh ")[1].split(" ")[0]
            uid = next(l.split(":")[2] for l in passwd_names_lines() if l.startswith(u + ":"))
            return uid, 0
        if "df -P" in cmd:
            return "ok\nnas:/volume1/share/user 1 1 1 1% /home", 0
        if "klist" in cmd:
            return e.probe_klist if hasattr(e, "probe_klist") else ("", 0)
        if "nvidia-smi" in cmd:
            return "1", 0
        return "", 0

    def passwd_names_lines():
        with main.app.app_context():
            return main.read_passwd_lines()

    monkeypatch.setattr(verify, "_sh", sh)
    monkeypatch.setattr(verify, "log_operation", main.log_operation)   # env의 sqlite 기록기로
    monkeypatch.setattr(verify, "load_k8s", lambda: None)
    monkeypatch.setattr(verify.client, "CoreV1Api", lambda: e.v1)
    monkeypatch.setattr(verify, "_tcp_check", lambda h, p, expect_banner=None: (True, "SSH-2.0-Test"))
    monkeypatch.setattr(main, "_get_farm_node_info", lambda n: {"name": n, "host": "10.0.0.2", "port": 22})

    class _NoRows:
        def cursor(self):
            outer = self

            class Cur:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

                def execute(self, sql, params=()):
                    pass

                def fetchone(self):
                    return (0,)

                def fetchall(self):
                    return []
            return Cur()

        def close(self):
            pass
    monkeypatch.setattr(verify, "get_db_connection", lambda: _NoRows())
    return e


def test_full_provision_passes_five_probes_then_succeeds(full, lease_env):
    e = full
    e.api.post("/operations/provision", json={"request_id": "800", "username": "exp-np-full",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    assert result(e, "provision", "800")["phase"] == "SUCCESS", rows(e, "800")
    probes = [(a, p) for a, p in rows(e, "800") if a == "VERIFY_ACCESS"]
    assert [p for _, p in probes].count("SUCCESS") == 5          # 다섯 시험 전부 근거 행이 남는다
    seq = [a for a, p in rows(e, "800") if p == "SUCCESS"]
    assert seq.index("CREATE_SERVICE") < seq.index("VERIFY_ACCESS")   # 자원이 다 만들어진 뒤 시험


def test_full_provision_failing_probe_blocks_completion_as_degraded(full, lease_env):
    """티켓 시험이 계속 실패하면 재시도 후 DEGRADED — 완료(SUCCESS)로 기록되지 않는다."""
    e = full
    e.probe_klist = ("kinit: no ticket", 1)
    e.api.post("/operations/provision", json={"request_id": "801", "username": "exp-np-fulx",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    res = result(e, "provision", "801")
    assert res["phase"] == "FAIL" and res["error_code"] == "DEGRADED"
    assert not [1 for a, p in rows(e, "801") if a == "PROVISION" and p == "SUCCESS"]
    from lifecycle_steps import verify
    fails = [1 for a, p in rows(e, "801") if a == "VERIFY_ACCESS" and p == "FAIL"]
    assert len(fails) == verify.VERIFY_MAX_ATTEMPTS               # 전파 지연 대비 여유 재시도


def test_full_revoke_verifies_blocked_access(full, lease_env):
    e = full
    e.api.post("/operations/provision", json={"request_id": "802", "username": "exp-np-fulr",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    pod_name = next(iter(e.v1.pods))
    e.api.post("/operations/revoke", json={"request_id": "802", "pod_name": pod_name})
    tick(e)
    assert result(e, "revoke", "802")["phase"] == "SUCCESS", rows(e, "802")
    assert ("VERIFY_REVOKED", "SUCCESS") in rows(e, "802")
    assert e.v1.pods == {}


# ---------- 로그인 비밀번호는 Pod 설정이 아니라 Pod별 Secret에 ----------

def test_pod_password_lives_in_owned_secret_and_is_removed_on_revoke(env):
    e = env
    e.api.post("/operations/provision", json={"request_id": "900", "username": "exp-np-pw",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    assert result(e, "provision", "900")["phase"] == "SUCCESS", rows(e, "900")
    pod_name = next(iter(e.v1.pods))
    env_vars = e.v1.pods[pod_name].body["spec"]["containers"][0]["env"]
    by_name = {v["name"]: v for v in env_vars}
    assert "value" not in by_name["USER_PW"]                                   # 평문 없음
    assert by_name["USER_PW"]["valueFrom"]["secretKeyRef"] == {"name": f"{pod_name}-account", "key": "USER_PW"}
    assert "HOME" not in by_name                                              # root로 들어가도 사용자 홈을 읽지 않게
    secret = e.v1.secrets[f"{pod_name}-account"]
    assert secret["data"]["USER_PW"] == base64.b64decode(PW).decode()
    assert secret["owners"][0]["kind"] == "Pod" and secret["owners"][0]["uid"] == f"uid-{pod_name}"

    e.api.post("/operations/revoke", json={"request_id": "900", "pod_name": pod_name})
    tick(e)
    assert result(e, "revoke", "900")["phase"] == "SUCCESS", rows(e, "900")
    assert e.v1.secrets == {}


def test_failed_pod_creation_removes_password_secret(env, monkeypatch):
    e = env
    def broken(namespace, body):
        raise ApiException(status=422, reason="Invalid")
    monkeypatch.setattr(e.v1, "create_namespaced_pod", broken)
    e.api.post("/operations/provision", json={"request_id": "901", "username": "exp-np-pw2",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    assert e.v1.secrets == {}

