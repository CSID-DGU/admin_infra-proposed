"""마이그레이션 작업(v2.0): 새 노드에 Pod를 먼저 만들고 기존 Pod를 정리한다. 옮길 이유가 없으면 건너뜀으로 끝난다."""
import base64

import main
from test_e2e_virtual import env, full, tick, result, rows, PW  # noqa: F401  (env·full은 pytest fixture)

NODES = [{"node_name": "farm2", "num_gpu": 1, "cpu_limit": "4", "memory_limit": "16Gi"},
         {"node_name": "farm7", "num_gpu": 1, "cpu_limit": "4", "memory_limit": "16Gi"}]


def _provisioned(e, monkeypatch, rid="1000", user="exp-np-mig"):
    e.was = lambda url: e.Resp(200, {"image": "dguailab/decs:1", "passwd_base64": PW, "gpu_nodes": NODES})
    monkeypatch.setattr(main, "delete_pod_util", lambda name, ns: e.v1.delete_namespaced_pod(name, ns))
    assert e.api.post("/operations/provision", json={"request_id": rid, "username": user,
                                                     "account": {"passwd_base64": PW}}).status_code == 202
    tick(e)
    made = result(e, "provision", rid)
    assert made["phase"] == "SUCCESS" and made["result"]["node"] == "farm2"
    return made["result"]["pod_name"]


def test_force_migration_moves_pod_and_inherits_password(env, monkeypatch):
    e = env
    old_pod = _provisioned(e, monkeypatch)
    # 신청의 비밀번호는 승인 뒤 지워진다 — 새 Pod는 기존 Pod의 Secret에서 이어받아야 한다
    e.was = lambda url: e.Resp(200, {"image": "dguailab/decs:1", "passwd_base64": "", "gpu_nodes": NODES})
    r = e.api.post("/operations/migrate", json={"request_id": "1000", "username": "exp-np-mig", "pod_name": old_pod,
                                                "nodes": ["farm2", "farm7"], "force": True})
    assert r.status_code == 202
    tick(e)
    res = result(e, "migrate", "1000")
    assert res["phase"] == "SUCCESS", res
    out = res["result"]
    assert out["status"] == "migrated" and out["from_node"] == "farm2" and out["to_node"] == "farm7"
    new_pod = out["pod_name"]
    assert new_pod != old_pod and old_pod not in e.v1.pods
    assert e.v1.pods[new_pod].spec.node_name == "farm7"
    assert f"{old_pod}-account" not in e.v1.secrets
    assert e.v1.secrets[f"{new_pod}-account"]["data"]["USER_PW"] == base64.b64decode(PW).decode()
    assert out["old_pod_cleanup"] is None and out["ports"]
    # 기존 노드의 keytab을 정리한다(그 노드에 같은 사용자의 다른 Pod가 없으므로)
    assert ("krb5_remove", ("exp-np-mig", "farm2")) in e.calls


def test_no_other_candidate_is_skipped_without_touching_pod(env, monkeypatch):
    e = env
    old_pod = _provisioned(e, monkeypatch, rid="1001", user="exp-np-mig1")
    e.api.post("/operations/migrate", json={"request_id": "1001", "username": "exp-np-mig1", "pod_name": old_pod,
                                            "nodes": ["farm2"]})
    tick(e)
    out = result(e, "migrate", "1001")
    assert out["phase"] == "SUCCESS" and out["result"]["status"] == "skipped"
    assert out["result"]["reason"] == "no_candidate_node" and old_pod in e.v1.pods and len(e.v1.pods) == 1


def test_small_improvement_is_skipped_unless_forced(env, monkeypatch):
    e = env
    old_pod = _provisioned(e, monkeypatch, rid="1002", user="exp-np-mig2")
    monkeypatch.setattr(main, "get_node_gpu_score", lambda node, url, t: {"farm2": 50.0, "farm7": 45.0}[node])
    e.api.post("/operations/migrate", json={"request_id": "1002", "username": "exp-np-mig2", "pod_name": old_pod,
                                            "nodes": ["farm2", "farm7"]})
    tick(e)
    out = result(e, "migrate", "1002")["result"]
    assert out["status"] == "skipped" and out["reason"] == "no_significant_improvement"


def test_missing_pod_fails_without_retry(env, monkeypatch):
    e = env
    _provisioned(e, monkeypatch, rid="1003", user="exp-np-mig3")
    e.api.post("/operations/migrate", json={"request_id": "1003", "username": "exp-np-mig3",
                                            "pod_name": "ailab-exp-np-mig3-gone", "nodes": ["farm2", "farm7"], "force": True})
    tick(e)
    out = result(e, "migrate", "1003")
    assert out["phase"] == "FAIL" and out["error_code"] == "POD_NOT_FOUND"


def test_same_migration_twice_is_409(env, monkeypatch):
    e = env
    body = {"request_id": "1004", "username": "exp-np-mig4", "nodes": ["farm2", "farm7"]}
    assert e.api.post("/operations/migrate", json=body).status_code == 202
    assert e.api.post("/operations/migrate", json=body).status_code == 409


def test_old_node_keytab_kept_when_another_pod_of_user_remains(env, monkeypatch):
    e = env
    old_pod = _provisioned(e, monkeypatch, rid="1005", user="exp-np-mig5")
    assert e.api.post("/operations/provision", json={"request_id": "1006", "username": "exp-np-mig5"}).status_code == 202
    tick(e)
    assert result(e, "provision", "1006")["result"]["node"] == "farm2"
    e.calls.clear()
    e.api.post("/operations/migrate", json={"request_id": "1005", "username": "exp-np-mig5", "pod_name": old_pod,
                                            "nodes": ["farm2", "farm7"], "force": True})
    tick(e)
    assert result(e, "migrate", "1005")["result"]["status"] == "migrated"
    assert not [c for c in e.calls if c[0] == "krb5_remove"]   # 1006의 Pod가 farm2에 남아 있어 유지


def test_full_mode_verifies_new_pod_before_cleaning_old(full, monkeypatch):
    e = full
    monkeypatch.setattr(main, "delete_pod_util", lambda name, ns: e.v1.delete_namespaced_pod(name, ns))
    e.was = lambda url: e.Resp(200, {"image": "dguailab/decs:1", "passwd_base64": PW, "gpu_nodes": NODES})
    e.api.post("/operations/provision", json={"request_id": "810", "username": "exp-np-fmig", "account": {"passwd_base64": PW}})
    tick(e)
    old_pod = result(e, "provision", "810")["result"]["pod_name"]
    e.api.post("/operations/migrate", json={"request_id": "810", "username": "exp-np-fmig", "pod_name": old_pod,
                                            "nodes": ["farm2", "farm7"], "force": True})
    tick(e)
    assert result(e, "migrate", "810")["phase"] == "SUCCESS"
    seq = [a for a, p in rows(e, "810") if p == "SUCCESS"]
    migrate_part = seq[seq.index("PROVISION") + 1:]
    assert migrate_part.count("VERIFY_ACCESS") == 5
    assert migrate_part.index("VERIFY_ACCESS") < len(migrate_part) - 1 - migrate_part[::-1].index("DELETE_POD_K8S")


def test_migrate_steps_include_probes_only_in_full_mode(monkeypatch):
    names = lambda: [s.__name__ for s in main._job_steps("migrate", {})]
    monkeypatch.setattr(main, "VERIFY_MODE", "noprobe")
    assert "step_verify_uid" not in names()
    monkeypatch.setattr(main, "VERIFY_MODE", "full")
    n = names()
    assert n[-1] == "step_migrate_cleanup_old" and n.index("step_verify_endpoint") < n.index("step_migrate_cleanup_old")
