"""같은 노드 재생성: 현재 노드에서 Pod를 다시 만들고, 요청하면 컨테이너 변경분을 그 노드의 이미지로 구워 이어 준다."""
import main
from adapters import node_image
from test_e2e_virtual import env, tick, result, PW  # noqa: F401  (env는 pytest fixture)

NODES = [{"node_name": "farm2", "num_gpu": 1, "gpu_models": ["RTX A5000"], "cpu_limit": "4", "memory_limit": "16Gi"}]
BASE = "dguailab/decs:1"
NS = main.app.config["NAMESPACE"]


def _provisioned(e, monkeypatch, rid, user):
    e.was = lambda url: e.Resp(200, {"image": BASE, "passwd_base64": PW, "gpu_nodes": NODES})
    monkeypatch.setattr(main, "delete_pod_util", lambda name, ns: e.v1.delete_namespaced_pod(name, ns))
    assert e.api.post("/operations/provision", json={"request_id": rid, "username": user,
                                                     "account": {"passwd_base64": PW}}).status_code == 202
    tick(e)
    return result(e, "provision", rid)["result"]["pod_name"]


def _images(e, monkeypatch, added=1000, commit_error=None):
    """노드 이미지 작업을 기록만 하는 대역. 실제로는 노드에 도우미 Pod를 띄운다."""
    def commit(settings, node, container, ref, running):
        e.calls.append(("image_commit", (node, ref, running)))
        if commit_error:
            raise commit_error
        return added

    monkeypatch.setattr(main.node_image, "commit", commit)
    monkeypatch.setattr(main.node_image, "remove",
                        lambda settings, node, ref: e.calls.append(("image_remove", (node, ref))) or True)


def _recreate(e, rid, user, old_pod, **extra):
    r = e.api.post("/operations/migrate", json={"request_id": rid, "username": user, "pod_name": old_pod,
                                                "recreate": True, **extra})
    assert r.status_code == 202, r.get_json()
    tick(e)
    return result(e, "migrate", rid)


def _container(e, pod_name):
    return e.v1.pods[pod_name].body["spec"]["containers"][0]


def test_recreate_keeps_changes_in_node_local_image(env, monkeypatch):
    e = env
    old_pod = _provisioned(e, monkeypatch, "2000", "exp-np-rc")
    _images(e, monkeypatch)
    res = _recreate(e, "2000", "exp-np-rc", old_pod)
    assert res["phase"] == "SUCCESS", res
    out = res["result"]
    new_pod = out["pod_name"]
    assert out["status"] == "migrated" and out["from_node"] == out["to_node"] == "farm2" and out["changes_kept"]
    assert new_pod != old_pod and old_pod not in e.v1.pods
    ref = node_image.ref_for(NS, new_pod)
    assert ("image_commit", ("farm2", ref, True)) in e.calls
    # 구운 이미지는 그 노드에만 있다 — 내려받으려 하면 안 된다
    assert _container(e, new_pod)["image"] == ref and _container(e, new_pod)["imagePullPolicy"] == "Never"
    # 기존 Pod는 기본 이미지였으므로 지울 구운 이미지가 없다
    assert not [c for c in e.calls if c[0] == "image_remove"]


def test_recreate_without_keeping_changes_resets_to_base_image(env, monkeypatch):
    e = env
    old_pod = _provisioned(e, monkeypatch, "2001", "exp-np-rc1")
    _images(e, monkeypatch)
    out = _recreate(e, "2001", "exp-np-rc1", old_pod, keep_changes=False)["result"]
    assert out["status"] == "migrated" and not out["changes_kept"]
    assert _container(e, out["pod_name"])["image"] == BASE
    assert _container(e, out["pod_name"])["imagePullPolicy"] == "IfNotPresent"
    assert not [c for c in e.calls if c[0] == "image_commit"]


def test_commit_failure_fails_job_and_leaves_old_pod(env, monkeypatch):
    e = env
    old_pod = _provisioned(e, monkeypatch, "2002", "exp-np-rc2")
    _images(e, monkeypatch, commit_error=node_image.NodeImageError("IMAGE_COMMIT_FAILED", "boom"))
    res = _recreate(e, "2002", "exp-np-rc2", old_pod)
    assert res["phase"] == "FAIL" and res["error_code"] == "IMAGE_COMMIT_FAILED"
    # 기본 이미지로 조용히 넘어가 새 Pod를 만들지 않는다
    assert list(e.v1.pods) == [old_pod]


def test_changes_over_limit_are_rejected_and_image_removed(env, monkeypatch):
    e = env
    old_pod = _provisioned(e, monkeypatch, "2003", "exp-np-rc3")
    monkeypatch.setitem(main.app.config, "USER_IMAGE_MAX_ADDED_BYTES", 500)
    _images(e, monkeypatch, added=501)
    res = _recreate(e, "2003", "exp-np-rc3", old_pod)
    assert res["phase"] == "FAIL" and res["error_code"] == "IMAGE_CHANGES_TOO_LARGE"
    assert list(e.v1.pods) == [old_pod]
    committed = [c[1][1] for c in e.calls if c[0] == "image_commit"]
    assert [c[1] for c in e.calls if c[0] == "image_remove"] == [("farm2", committed[0])]


def test_previous_committed_image_is_removed_after_next_recreate(env, monkeypatch):
    e = env
    old_pod = _provisioned(e, monkeypatch, "2004", "exp-np-rc4")
    _images(e, monkeypatch)
    first = _recreate(e, "2004", "exp-np-rc4", old_pod)["result"]["pod_name"]
    e.calls.clear()
    out = _recreate(e, "2004", "exp-np-rc4", first, keep_changes=False)["result"]
    # 초기화했으므로 이전에 구운 이미지는 더 쓸 곳이 없다
    assert ("image_remove", ("farm2", node_image.ref_for(NS, first))) in e.calls
    assert _container(e, out["pod_name"])["image"] == BASE


def test_revoke_removes_committed_image(env, monkeypatch):
    e = env
    old_pod = _provisioned(e, monkeypatch, "2005", "exp-np-rc5")
    _images(e, monkeypatch)
    pod = _recreate(e, "2005", "exp-np-rc5", old_pod)["result"]["pod_name"]
    e.calls.clear()
    assert e.api.post("/operations/revoke", json={"request_id": "2005", "username": "exp-np-rc5", "pod_name": pod,
                                                  "delete_account": True}).status_code == 202
    tick(e)
    assert result(e, "revoke", "2005")["phase"] == "SUCCESS"
    assert ("image_remove", ("farm2", node_image.ref_for(NS, pod))) in e.calls


def test_nodes_are_required_unless_recreate(env):
    r = env.api.post("/operations/migrate", json={"request_id": "2006", "username": "exp-np-rc6"})
    assert r.status_code == 400
