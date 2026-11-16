"""Pod 별 독립 NFS client: Pod 를 만들기 전에 노드에 nfs-attach, Pod 가 사라진 뒤 nfs-detach.

노드 쪽(provi-pod-nfs)은 가짜로 두고, 이 서버가 언제 무엇을 부르고 그 답을 어떻게 다루는지를 본다. 가짜 노드는
실제 스크립트의 약속을 그대로 따른다 — 답은 표준출력의 nfs_state 줄이고, busy(종료 코드 75)·failed(1)일 때
_farm_ssh 는 그 출력을 예외에 붙여 올린다.
"""
import types

import pytest

import main
import reconcile_krb5 as rec
from adapters import node_image
from main import Action, Phase
from test_e2e_virtual import env, tick, result, rows, PW  # noqa: F401  (env는 pytest fixture)

NS = main.app.config["NAMESPACE"]
NODES = [{"node_name": "farm2", "num_gpu": 1, "gpu_models": ["RTX A5000"], "cpu_limit": "4", "memory_limit": "16Gi"}]


def remote_exit(stdout):
    """원격 스크립트가 0 이 아닌 코드로 끝났을 때 _farm_ssh 가 올리는 예외. 메시지에는 ssh -v 출력이 길게 붙는다."""
    err = RuntimeError("farm SSH 실패 (farm2:22): debug1: Exit status 75\nprovi-pod-nfs: run rc=0 0.01s: ...")
    err.stdout = stdout
    return err


class FakeNodes:
    """노드들의 ailab-krb5-admin nfs-attach·nfs-detach·nfs-list."""

    def __init__(self):
        self.made = {}           # 노드 → {(네임스페이스, Pod): 마지막 attach 뒤 지난 초}
        self.calls = []          # (노드, 명령)
        self.timeline = None     # 다른 대역의 호출과 순서를 견줄 때 쓰는 공용 목록
        self.busy_polls = {}     # Pod → busy 로 답할 남은 횟수(컨테이너가 끝나는 중)
        self.held = set()        # 끝나지 않는 컨테이너가 마운트를 잡고 있는 Pod
        self.attach_error = None
        self.down = set()        # 접속되지 않는 노드

    def ssh(self, host, port, command, stdin_data=""):
        self.calls.append((host, command))
        if self.timeline is not None:
            self.timeline.append(("nfs", (host, command)))
        if host in self.down:
            raise RuntimeError(f"farm SSH 실패 ({host}:{port}): ssh: connect to host {host}: Connection timed out")
        action, *args = command.split()
        made = self.made.setdefault(host, {})
        if action == "nfs-attach":
            _user, _uid, ns, pod = args
            if self.attach_error:
                raise remote_exit(f"nfs_state=failed reason={self.attach_error}\n")
            reused = int((ns, pod) in made)
            made[(ns, pod)] = 0
            return f"nfs_state=ready path=/run/provi/nfs/{ns}/{pod} slot={len(made) - 1} reused={reused}\n"
        if action == "nfs-detach":
            ns, pod, *rest = args
            if (ns, pod) not in made:
                return "nfs_state=absent\n"
            if rest and made[(ns, pod)] < int(rest[0]):
                raise remote_exit(f"nfs_state=busy reason=attached_{made[(ns, pod)]}s_ago\n")
            if pod in self.held or self.busy_polls.get(pod, 0) > 0:
                self.busy_polls[pod] = self.busy_polls.get(pod, 0) - 1
                raise remote_exit("nfs_state=busy reason=holders=1(pid=4242:/home)\n")
            del made[(ns, pod)]
            return "nfs_state=removed\n"
        if action == "nfs-list":
            return "".join(f"{ns} {pod} user=u uid=21001 slot={i} phase=ready mounted=1 holders=0 tcp=1 "
                           f"created_at=1 attached_at=1\n" for i, (ns, pod) in enumerate(made))
        return ""

    def commands(self, action=None):
        return [c for _host, c in self.calls if action is None or c.split()[0] == action]


_REAL_POD_NFS_CALL = main._pod_nfs_call
# 이 기능을 넣기 전의 코드가 만든 Pod(노드의 NFS client 마운트를 /home 으로 받은 Pod)를 만들 때는 판정 함수를 잠시 바꾼다.
# 운영에 지금 떠 있는 Pod 가 모두 이 모양이다.
_REAL_POD_NFS_ENABLED = main.pod_nfs_enabled


@pytest.fixture
def nodes(monkeypatch):
    fake = FakeNodes()
    monkeypatch.setitem(main.app.config, "FARM_NODES", [{"name": n, "host": n, "port": "22"} for n in ("farm2", "farm7")])
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "TEST.REALM")
    # conftest 의 pod_nfs_node 가 노드 호출을 곧바로 성공으로 바꿔 두므로, 이 파일은 실제 함수로 되돌려 대역 노드와 주고받는다
    monkeypatch.setattr(main, "_pod_nfs_call", _REAL_POD_NFS_CALL)
    monkeypatch.setattr(main, "_farm_ssh", fake.ssh)
    monkeypatch.setattr(rec, "_farm_ssh", fake.ssh)
    # 기다림은 실제 시계로 돈다. 한도를 짧게 줄여 시험이 오래 걸리지 않게 한다.
    monkeypatch.setattr(main, "POD_NFS_DETACH_WAIT_SEC", 0.2)
    monkeypatch.setattr(main, "POD_NFS_DETACH_POLL_SEC", 0.01)
    return fake


# ---------- 노드 호출 ----------

def test_attach_sends_account_namespace_pod_and_returns_the_mount_path(nodes):
    path = main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "ailab-exp-bl-a-1a2b3c4d")
    assert nodes.calls == [("farm2", f"nfs-attach exp-bl-a 60001 {NS} ailab-exp-bl-a-1a2b3c4d")]
    assert path == f"/run/provi/nfs/{NS}/ailab-exp-bl-a-1a2b3c4d"


def test_attach_failure_carries_the_reason_the_node_gave(nodes):
    """실패 이유는 노드 스크립트가 표준출력에 남긴 것이다. ssh 디버그 출력이 붙은 예외 메시지가 아니다."""
    nodes.attach_error = "exp-bl-a_의_Kerberos_티켓이_없음"
    with pytest.raises(main.PodNfsError) as failed:
        main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "ailab-exp-bl-a-1")
    assert str(failed.value) == "farm2: exp-bl-a_의_Kerberos_티켓이_없음"


def test_attach_refuses_a_path_other_than_the_expected_one(nodes, monkeypatch):
    """노드가 다른 경로를 답하면 그 경로를 Pod 에 넘기지 않는다."""
    monkeypatch.setattr(main, "_farm_ssh", lambda *a, **k: "nfs_state=ready path=/home/tako2/share/user slot=0 reused=0\n")
    with pytest.raises(main.PodNfsError, match="다름"):
        main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "ailab-exp-bl-a-1")


def test_attach_without_a_state_line_is_an_error(nodes, monkeypatch):
    """스크립트가 설치되지 않은 노드는 상태 줄 없이 끝난다."""
    monkeypatch.setattr(main, "_farm_ssh", lambda *a, **k: "unknown action\n")
    with pytest.raises(main.PodNfsError, match="상태를 답하지 않음"):
        main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "ailab-exp-bl-a-1")


def test_connection_failure_is_raised_as_it_is(nodes):
    nodes.down.add("farm2")
    with pytest.raises(RuntimeError, match="Connection timed out") as failed:
        main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "ailab-exp-bl-a-1")
    assert not isinstance(failed.value, main.PodNfsError)


def test_detach_reports_removed_and_absent(nodes):
    main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "pod-a")
    assert main._detach_pod_nfs("farm2", "pod-a")["nfs_state"] == "removed"
    assert main._detach_pod_nfs("farm2", "pod-a")["nfs_state"] == "absent"
    assert nodes.commands("nfs-detach") == [f"nfs-detach {NS} pod-a"] * 2


def test_detach_asks_again_while_the_container_is_ending(nodes):
    main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "pod-a")
    nodes.busy_polls["pod-a"] = 2
    assert main._detach_pod_nfs("farm2", "pod-a", wait_sec=5)["nfs_state"] == "removed"
    assert len(nodes.commands("nfs-detach")) == 3


def test_detach_without_wait_asks_once(nodes):
    main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "pod-a")
    nodes.held.add("pod-a")
    answer = main._detach_pod_nfs("farm2", "pod-a")
    assert (answer["nfs_state"], answer["reason"]) == ("busy", "holders=1(pid=4242:/home)")
    assert len(nodes.commands("nfs-detach")) == 1


def test_detach_gives_up_at_the_limit_and_leaves_it(nodes):
    main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "pod-a")
    nodes.held.add("pod-a")
    assert main._detach_pod_nfs("farm2", "pod-a", wait_sec=0.05)["nfs_state"] == "busy"
    assert len(nodes.commands("nfs-detach")) > 1
    assert (NS, "pod-a") in nodes.made["farm2"]


def test_detach_passes_the_minimum_age_to_the_node(nodes):
    """"방금 attach 한 것인가"는 노드가 판정한다 — 목록을 읽은 뒤 attach 가 다시 올 수 있다."""
    main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "pod-a")
    answer = main._detach_pod_nfs("farm2", "pod-a", min_age_sec=600)
    assert nodes.commands("nfs-detach") == [f"nfs-detach {NS} pod-a 600"]
    assert (answer["nfs_state"], answer["reason"]) == ("busy", "attached_0s_ago")


def test_detach_failure_answer_is_an_error(nodes, monkeypatch):
    def failed(*a, **k):
        raise remote_exit("nfs_state=failed reason=지운_뒤에도_남음:_netns_provi-n000\n")
    monkeypatch.setattr(main, "_farm_ssh", failed)
    with pytest.raises(main.PodNfsError, match="지운_뒤에도_남음"):
        main._detach_pod_nfs("farm2", "pod-a")


def test_release_never_raises_and_reports_what_happened(nodes, logs):
    main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "pod-a")
    nodes.held.add("pod-a")
    assert main.release_pod_nfs("farm2", "pod-a", request_id="7", username="exp-bl-a") == "busy"
    nodes.held.clear()
    assert main.release_pod_nfs("farm2", "pod-a", request_id="7", username="exp-bl-a") == "removed"
    nodes.down.add("farm2")
    assert main.release_pod_nfs("farm2", "pod-a", request_id="7", username="exp-bl-a") == "failed"
    assert main.release_pod_nfs("farm9", "pod-a", request_id="7", username="exp-bl-a") == "failed"   # 모르는 노드
    seen = [(r["phase"], r.get("error_code")) for r in logs if r["action"] == Action.DETACH_POD_NFS]
    assert seen == [(Phase.START, None), (Phase.FAIL, "POD_NFS_BUSY"),
                    (Phase.START, None), (Phase.SUCCESS, None),
                    (Phase.START, None), (Phase.FAIL, "POD_NFS_DETACH_FAILED"),
                    (Phase.START, None), (Phase.FAIL, "POD_NFS_DETACH_FAILED")]
    assert {(r["resource_type"], r["node_name"], r["pod_name"]) for r in logs} <= {
        ("pod_nfs", "farm2", "pod-a"), ("pod_nfs", "farm9", "pod-a")}


def test_release_without_request_id_leaves_no_step_rows(nodes, logs):
    main._attach_pod_nfs("exp-bl-a", 60001, "farm2", "pod-a")
    assert main.release_pod_nfs("farm2", "pod-a") == "removed"
    assert logs == []


def _pod(volumes):
    return types.SimpleNamespace(spec=types.SimpleNamespace(volumes=[
        types.SimpleNamespace(name=name, host_path=types.SimpleNamespace(path=path) if path else None)
        for name, path in volumes]))


def test_a_pod_is_recognised_by_where_its_home_comes_from():
    assert main.pod_uses_nfs_client(_pod([("nfs-home", f"/run/provi/nfs/{NS}/pod-a"), ("krb5-ccache", "/run/user/1")]))
    assert not main.pod_uses_nfs_client(_pod([("nfs-home", "/home/tako7/share/user"), ("krb5-ccache", "/run/user/1")]))
    assert not main.pod_uses_nfs_client(_pod([("other", f"/run/provi/nfs/{NS}/pod-a")]))
    assert not main.pod_uses_nfs_client(_pod([("nfs-home", None)]))
    assert not main.pod_uses_nfs_client(types.SimpleNamespace(spec=types.SimpleNamespace(volumes=None)))


def test_kerberos_environment_always_uses_the_pod_nfs_client(monkeypatch):
    """Kerberos 를 쓰면 새 Pod 는 항상 Pod 별 독립 NFS client 를 쓴다. 배포 설정으로 끄는 값은 없다."""
    assert not any("NFS_CLIENT" in key for key in main.app.config)
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "")
    assert not main.pod_nfs_enabled()
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "TEST.REALM")
    assert main.pod_nfs_enabled()


# ---------- 생성 ----------

def _provision(e, rid, user):
    e.was = lambda url: e.Resp(200, {"image": "dguailab/decs:1", "passwd_base64": PW, "gpu_nodes": NODES})
    assert e.api.post("/operations/provision", json={"request_id": rid, "username": user,
                                                     "account": {"passwd_base64": PW}}).status_code == 202
    tick(e)
    return result(e, "provision", rid)


def _home_volume(e, pod_name):
    return next(v for v in e.v1.pods[pod_name].body["spec"]["volumes"] if v["name"] == "nfs-home")


def test_new_pod_gets_its_own_mount_as_home(env, nodes):
    e = env
    nodes.timeline = e.calls
    res = _provision(e, "301", "exp-np-n1")
    assert res["phase"] == "SUCCESS", rows(e, "301")
    pod_name = res["result"]["pod_name"]
    uid = res["result"]["uid"]
    assert nodes.commands() == [f"nfs-attach exp-np-n1 {uid} {NS} {pod_name}"]
    # 경로가 없으면 컨테이너가 시작되지 않게 Directory 로 받는다
    assert _home_volume(e, pod_name)["hostPath"] == {"path": f"/run/provi/nfs/{NS}/{pod_name}", "type": "Directory"}
    mounts = e.v1.pods[pod_name].body["spec"]["containers"][0]["volumeMounts"]
    assert {"name": "nfs-home", "mountPath": "/home", "readOnly": False} in mounts
    # 사용자 티켓을 노드에 배포한 뒤에 마운트한다
    order = [c[0] for c in e.calls if c[0] in ("krb5_deploy", "nfs")]
    assert order == ["krb5_deploy", "nfs"]
    done = [a for a, p in rows(e, "301") if p == "SUCCESS"]
    assert done[done.index("DEPLOY_KRB5"):done.index("CREATE_POD_K8S") + 1] == [
        "DEPLOY_KRB5", "ATTACH_POD_NFS", "CREATE_POD_K8S"]


def test_pod_made_before_this_feature_keeps_the_node_mount(env, nodes, monkeypatch):
    monkeypatch.setattr(main, "pod_nfs_enabled", lambda: False)
    e = env
    res = _provision(e, "302", "exp-np-n2")
    assert res["phase"] == "SUCCESS", rows(e, "302")
    assert nodes.calls == []
    assert _home_volume(e, res["result"]["pod_name"])["hostPath"] == {"path": "/home/share/user", "type": "Directory"}
    assert "ATTACH_POD_NFS" not in [a for a, _p in rows(e, "302")]


def test_attach_failure_makes_no_pod_and_names_the_step(env, nodes):
    e = env
    nodes.attach_error = "netns_에서_NAS_로_가는_TCP_연결이_1개가_아님:_0"
    res = _provision(e, "303", "exp-np-n3")
    assert res["phase"] != "SUCCESS"
    assert e.v1.pods == {}
    failed = e.db.execute("SELECT phase, error_code, error_detail, node_name FROM operation_log"
                          " WHERE request_id='303' AND action='ATTACH_POD_NFS' AND phase!='START'").fetchall()
    assert failed and all(row == ("FAIL", "POD_NFS_ATTACH_FAILED",
                                  "farm2: netns_에서_NAS_로_가는_TCP_연결이_1개가_아님:_0", "farm2") for row in failed)
    assert [c for c in e.calls if c[0] == "release"]   # 배정한 접속 포트를 반환함


def test_failed_start_removes_the_mount_made_for_the_pod(env, nodes, monkeypatch):
    """컨테이너가 준비되지 못해 Pod 를 지울 때, 그 Pod 를 위해 만든 것도 지운다."""
    e = env
    monkeypatch.setattr(main, "is_pod_ready", lambda pod: False)
    monkeypatch.setattr(main, "get_pod_failure_reason", lambda pod: "CrashLoopBackOff: boom")
    res = _provision(e, "304", "exp-np-n4")
    assert res["phase"] != "SUCCESS" and e.v1.pods == {}
    assert [c.split()[0] for c in nodes.commands()] == ["nfs-attach", "nfs-detach"]
    assert nodes.made["farm2"] == {}


def test_failed_start_of_a_node_mount_pod_does_not_touch_the_node(env, nodes, monkeypatch):
    monkeypatch.setattr(main, "pod_nfs_enabled", lambda: False)
    monkeypatch.setattr(main, "is_pod_ready", lambda pod: False)
    monkeypatch.setattr(main, "get_pod_failure_reason", lambda pod: "CrashLoopBackOff: boom")
    res = _provision(env, "305", "exp-np-n5")
    assert res["phase"] != "SUCCESS" and nodes.calls == []


def test_retry_of_the_same_pod_asks_the_node_again(env, nodes, monkeypatch):
    """Pod 생성이 실패해 같은 Pod 이름으로 다시 만들 때마다 nfs-attach 를 다시 부른다. 노드는 남아 있는 것을
    그대로 답하고 마지막 attach 시각을 새로 적는다(그래야 정리 작업이 그 사이에 지우지 않는다)."""
    e = env
    real_create, failures = e.v1.create_namespaced_pod, [1]

    def create(namespace, body):
        if failures:
            failures.pop()
            raise RuntimeError("apiserver unavailable")
        return real_create(namespace, body)
    monkeypatch.setattr(e.v1, "create_namespaced_pod", create)
    monkeypatch.setattr(main, "RETRY_DELAY_SEC", 0)
    res = _provision(e, "306", "exp-np-n6")
    assert res["phase"] == "SUCCESS", rows(e, "306")
    pod_name = res["result"]["pod_name"]
    # 첫 시도: attach → Pod 생성 실패 → detach(지움). 다시 시도: attach(새로 만듦) → Pod 생성
    assert [c.split()[0] for c in nodes.commands()] == ["nfs-attach", "nfs-detach", "nfs-attach"]
    assert all(c.split()[-1] == pod_name for c in nodes.commands())
    assert list(nodes.made["farm2"]) == [(NS, pod_name)]


# ---------- 화면의 "현재 단계" ----------

@pytest.fixture
def banner(env, monkeypatch):
    """승인·재시작 화면이 "(현재 단계: …)"로 보여 주는 문구. 기록된 순서대로 (신청 번호, 단계 이름, 문구)."""
    seen = []
    monkeypatch.setattr(main, "set_pod_creation_status",
                        lambda k, st, m="": (seen.append((str(k), st, m)), env.status.__setitem__(str(k), st)))
    return seen


def test_banner_names_the_nfs_step_between_kerberos_and_pod_creation(env, nodes, banner):
    assert _provision(env, "341", "exp-np-b1")["phase"] == "SUCCESS", rows(env, "341")
    texts = [m for k, _st, m in banner if k == "341"]
    at = texts.index("홈 디렉터리 NFS 연결 중 (node=farm2)")
    assert texts[at - 1] == "krb5 배포 중 (node=farm2)" and texts[at + 1] == "k8s pod 생성 중 (node=farm2)"
    # 단계 이름은 화면이 이미 아는 것을 쓴다(새 이름을 쓰면 화면의 상태 표시가 그 이름을 모른다)
    assert next(st for k, st, m in banner if m.startswith("홈 디렉터리 NFS 연결 중")) == "deploying_krb5"


def test_banner_has_no_nfs_step_for_a_node_mount_pod(env, nodes, banner, monkeypatch):
    monkeypatch.setattr(main, "pod_nfs_enabled", lambda: False)
    assert _provision(env, "342", "exp-np-b2")["phase"] == "SUCCESS"
    assert not [m for _k, _st, m in banner if "NFS" in m]


def test_banner_says_which_step_failed_when_the_node_refuses(env, nodes, banner):
    """승인 화면은 실패한 신청의 원인으로 마지막 문구를 보여 준다. "pod spec 생성 실패"만으로는 어디서 멈췄는지 모른다."""
    nodes.attach_error = "exp-np-b3_의_Kerberos_티켓이_없음"
    assert _provision(env, "343", "exp-np-b3")["phase"] != "SUCCESS"
    failed = [m for k, st, m in banner if k == "343" and st == "failed"]
    assert failed and set(failed) == {"홈 디렉터리 NFS 연결 실패"}


def test_banner_keeps_the_general_text_for_other_build_failures(env, nodes, banner, monkeypatch):
    def no_ports(**kw):
        raise RuntimeError("no free nodeport")
    monkeypatch.setattr(main, "allocate_nodeports", no_ports)
    assert _provision(env, "344", "exp-np-b4")["phase"] != "SUCCESS"
    assert {m for k, st, m in banner if k == "344" and st == "failed"} == {"pod spec 생성 실패"}


def test_banner_shows_the_old_container_cleanup_during_recreate(env, nodes, banner, monkeypatch):
    """재시작에서 기존 컨테이너가 끝나기를 기다리는 동안 "컨테이너 생성 완료"에 머물지 않는다."""
    e = env
    monkeypatch.setattr(main, "delete_pod_util", lambda name, ns: e.v1.delete_namespaced_pod(name, ns))
    monkeypatch.setattr(main.node_image, "commit", lambda settings, node, container, ref, running: 1000)
    monkeypatch.setattr(main.node_image, "remove", lambda settings, node, ref: True)
    old_pod = _provision(e, "345", "exp-np-b5")["result"]["pod_name"]
    banner.clear()
    seen_while_waiting = []
    real_release = main.release_pod_nfs
    monkeypatch.setattr(main, "release_pod_nfs",
                        lambda *a, **k: (seen_while_waiting.append(banner[-1]), real_release(*a, **k))[1])
    r = e.api.post("/operations/migrate", json={"request_id": "345", "username": "exp-np-b5", "pod_name": old_pod,
                                                "recreate": True})
    assert r.status_code == 202, r.get_json()
    tick(e)
    assert result(e, "migrate", "345")["phase"] == "SUCCESS", rows(e, "345")
    assert seen_while_waiting == [("345", "ready", "기존 컨테이너의 NFS 연결 정리 중")]
    assert banner[-1] == ("345", "ready", "마이그레이션 완료 (node=farm2)")


def test_banner_is_unchanged_for_recreate_of_a_shared_mount_pod(env, nodes, banner, monkeypatch):
    e = env
    monkeypatch.setattr(main, "delete_pod_util", lambda name, ns: e.v1.delete_namespaced_pod(name, ns))
    monkeypatch.setattr(main.node_image, "commit", lambda settings, node, container, ref, running: 1000)
    monkeypatch.setattr(main.node_image, "remove", lambda settings, node, ref: True)
    monkeypatch.setattr(main, "pod_nfs_enabled", lambda: False)
    old_pod = _provision(e, "346", "exp-np-b6")["result"]["pod_name"]
    banner.clear()
    r = e.api.post("/operations/migrate", json={"request_id": "346", "username": "exp-np-b6", "pod_name": old_pod,
                                                "recreate": True})
    assert r.status_code == 202, r.get_json()
    tick(e)
    assert result(e, "migrate", "346")["phase"] == "SUCCESS"
    assert not [m for _k, _st, m in banner if "NFS" in m]


# ---------- 삭제 ----------

def _revoke(e, rid, pod_name):
    assert e.api.post("/operations/revoke", json={"request_id": rid, "pod_name": pod_name}).status_code == 202
    tick(e)
    return result(e, "revoke", rid)


def test_revoke_removes_the_mount_after_the_pod_and_before_the_keytab(env, nodes):
    e = env
    pod_name = _provision(e, "311", "exp-np-d1")["result"]["pod_name"]
    nodes.timeline = e.calls
    e.calls.clear()
    assert _revoke(e, "311", pod_name)["phase"] == "SUCCESS", rows(e, "311")
    assert nodes.commands("nfs-detach") == [f"nfs-detach {NS} {pod_name}"]
    assert nodes.made["farm2"] == {}
    order = [c[0] for c in e.calls if c[0] in ("nfs", "krb5_remove")]
    assert order == ["nfs", "krb5_remove"]
    detach = e.db.execute("SELECT phase, node_name, pod_name, resource_type FROM operation_log"
                          " WHERE request_id='311' AND action='DETACH_POD_NFS' ORDER BY id").fetchall()
    assert detach == [("START", "farm2", pod_name, "pod_nfs"), ("SUCCESS", "farm2", pod_name, "pod_nfs")]


def test_revoke_of_a_pod_on_the_shared_mount_does_not_call_the_node(env, nodes, monkeypatch):
    """이 기능 전에 노드의 NFS client 마운트로 만든 Pod 는 노드에서 지울 것이 없다."""
    e = env
    monkeypatch.setattr(main, "pod_nfs_enabled", lambda: False)
    pod_name = _provision(e, "312", "exp-np-d2")["result"]["pod_name"]
    monkeypatch.setattr(main, "pod_nfs_enabled", _REAL_POD_NFS_ENABLED)
    assert _revoke(e, "312", pod_name)["phase"] == "SUCCESS"
    assert nodes.calls == []
    assert "DETACH_POD_NFS" not in [a for a, _p in rows(e, "312")]


def test_revoke_decides_cleanup_by_the_pod_not_by_the_current_setting(env, nodes, monkeypatch):
    """지울지는 지금의 판정(pod_nfs_enabled)이 아니라 지우는 Pod 의 설정으로 정한다. 판정이 바뀐 뒤에도(예: Kerberos
    설정이 빠짐) Pod 별 NFS client 로 만든 Pod 를 지우면 노드의 것을 지운다."""
    e = env
    pod_name = _provision(e, "313", "exp-np-d3")["result"]["pod_name"]
    monkeypatch.setattr(main, "pod_nfs_enabled", lambda: False)
    assert _revoke(e, "313", pod_name)["phase"] == "SUCCESS"
    assert nodes.commands("nfs-detach") == [f"nfs-detach {NS} {pod_name}"]
    assert nodes.made["farm2"] == {}


def test_busy_node_does_not_fail_the_revoke(env, nodes):
    """노드가 끝까지 "아직 사용 중"이라고 답해도 회수는 끝낸다. 남은 것은 정리 작업이 다시 지운다."""
    e = env
    pod_name = _provision(e, "314", "exp-np-d4")["result"]["pod_name"]
    nodes.held.add(pod_name)
    e.calls.clear()
    assert _revoke(e, "314", pod_name)["phase"] == "SUCCESS", rows(e, "314")
    assert (NS, pod_name) in nodes.made["farm2"]
    row = e.db.execute("SELECT phase, error_code, error_detail FROM operation_log WHERE request_id='314'"
                       " AND action='DETACH_POD_NFS' AND phase!='START'").fetchall()
    assert row == [("FAIL", "POD_NFS_BUSY", "holders=1(pid=4242:/home)")]
    assert ("krb5_remove", ("exp-np-d4", "farm2")) in e.calls   # 뒤 단계는 그대로 진행


def test_unreachable_node_does_not_fail_the_revoke(env, nodes):
    e = env
    pod_name = _provision(e, "315", "exp-np-d5")["result"]["pod_name"]
    nodes.down.add("farm2")
    assert _revoke(e, "315", pod_name)["phase"] == "SUCCESS", rows(e, "315")
    row = e.db.execute("SELECT phase, error_code FROM operation_log WHERE request_id='315'"
                       " AND action='DETACH_POD_NFS' AND phase!='START'").fetchall()
    assert row == [("FAIL", "POD_NFS_DETACH_FAILED")]


def test_recreate_gives_the_new_pod_its_own_mount_and_removes_the_old_one(env, nodes, monkeypatch):
    """같은 노드에서 다시 만들기(컨테이너 재시작). 새 Pod 는 이름이 달라 따로 받고, 기존 Pod 의 것은 기존
    컨테이너가 끝난 뒤에 지워진다."""
    e = env
    monkeypatch.setattr(main, "delete_pod_util", lambda name, ns: e.v1.delete_namespaced_pod(name, ns))
    monkeypatch.setattr(main.node_image, "commit", lambda settings, node, container, ref, running: 1000)
    monkeypatch.setattr(main.node_image, "remove", lambda settings, node, ref: True)
    old_pod = _provision(e, "321", "exp-np-r1")["result"]["pod_name"]
    nodes.busy_polls[old_pod] = 2      # 기존 컨테이너가 끝나는 동안 노드는 busy 로 답한다
    nodes.timeline = e.calls
    nodes.calls.clear()
    e.calls.clear()
    r = e.api.post("/operations/migrate", json={"request_id": "321", "username": "exp-np-r1", "pod_name": old_pod,
                                                "recreate": True})
    assert r.status_code == 202, r.get_json()
    tick(e)
    res = result(e, "migrate", "321")
    assert res["phase"] == "SUCCESS", rows(e, "321")
    new_pod = res["result"]["pod_name"]
    assert new_pod != old_pod and list(e.v1.pods) == [new_pod]
    assert _home_volume(e, new_pod)["hostPath"]["path"] == f"/run/provi/nfs/{NS}/{new_pod}"
    assert list(nodes.made["farm2"]) == [(NS, new_pod)]
    assert [c.split()[0] for c in nodes.commands()] == ["nfs-attach"] + ["nfs-detach"] * 3
    assert nodes.commands("nfs-detach")[0] == f"nfs-detach {NS} {old_pod}"
    assert res["result"]["old_pod_cleanup"] is None
    # 같은 사용자의 새 Pod 가 그 노드에 있으므로 keytab 은 남긴다
    assert not [c for c in e.calls if c[0] == "krb5_remove"]


def test_node_move_makes_the_mount_on_the_new_node_and_removes_it_from_the_old_node(env, nodes, monkeypatch):
    """다른 노드로 옮기기. 새 Pod 의 NFS client 는 옮겨 갈 노드에 만들고, 기존 Pod 의 것은 기존 컨테이너가 끝난 뒤
    기존 노드에서 지운다. 새 Pod 의 접속 포트까지 연결된 뒤에야 기존 것을 지운다."""
    e = env
    monkeypatch.setattr(main, "delete_pod_util", lambda name, ns: e.v1.delete_namespaced_pod(name, ns))
    old_pod = _provision(e, "324", "exp-np-r4")["result"]["pod_name"]
    assert nodes.made == {"farm2": {(NS, old_pod): 0}}
    both = NODES + [dict(NODES[0], node_name="farm7")]
    e.was = lambda url: e.Resp(200, {"image": "dguailab/decs:1", "passwd_base64": "", "gpu_nodes": both})
    nodes.busy_polls[old_pod] = 2      # 기존 컨테이너가 끝나는 동안 기존 노드는 busy 로 답한다
    nodes.timeline = e.calls
    nodes.calls.clear()
    e.calls.clear()
    r = e.api.post("/operations/migrate", json={"request_id": "324", "username": "exp-np-r4", "pod_name": old_pod,
                                                "nodes": ["farm2", "farm7"], "force": True})
    assert r.status_code == 202, r.get_json()
    tick(e)
    res = result(e, "migrate", "324")
    assert res["phase"] == "SUCCESS", rows(e, "324")
    out = res["result"]
    new_pod = out["pod_name"]
    assert (out["from_node"], out["to_node"]) == ("farm2", "farm7") and new_pod != old_pod
    assert list(e.v1.pods) == [new_pod] and e.v1.pods[new_pod].spec.node_name == "farm7"
    assert _home_volume(e, new_pod)["hostPath"] == {"path": f"/run/provi/nfs/{NS}/{new_pod}", "type": "Directory"}
    # 새 노드에는 만들기만, 기존 노드에는 지우기만 간다
    assert nodes.calls == ([("farm7", f"nfs-attach exp-np-r4 {_uid(e, 'exp-np-r4')} {NS} {new_pod}")]
                           + [("farm2", f"nfs-detach {NS} {old_pod}")] * 3)
    assert nodes.made == {"farm2": {}, "farm7": {(NS, new_pod): 0}}
    assert out["old_pod_cleanup"] is None
    # 순서: 새 노드에 만들기 → 새 Pod 의 접속 포트 연결 → 기존 노드에서 지우기 → 기존 노드의 keytab 정리
    order = [c[1][1].split()[0] if c[0] == "nfs" else c[0] for c in e.calls]
    assert order.index("nfs-attach") < order.index("svc_create") < order.index("nfs-detach")
    assert len(order) - 1 - order[::-1].index("nfs-detach") < order.index("krb5_remove")
    assert ("krb5_remove", ("exp-np-r4", "farm2")) in e.calls
    detach = e.db.execute("SELECT phase, node_name, pod_name FROM operation_log"
                          " WHERE request_id='324' AND action='DETACH_POD_NFS' ORDER BY id").fetchall()
    assert detach == [("START", "farm2", old_pod), ("SUCCESS", "farm2", old_pod)]
    attach = e.db.execute("SELECT phase, node_name, pod_name FROM operation_log"
                          " WHERE request_id='324' AND action='ATTACH_POD_NFS' AND pod_name=? ORDER BY id",
                          (new_pod,)).fetchall()
    assert attach == [("START", "farm7", new_pod), ("SUCCESS", "farm7", new_pod)]


def _uid(e, user):
    from test_e2e_virtual import passwd_line
    return int(passwd_line(user).split(":")[2])


def test_recreate_from_a_shared_mount_pod_moves_it_to_its_own_mount(env, nodes, monkeypatch):
    """이 기능 전에 노드의 NFS client 마운트로 만든 Pod 를 다시 만들면 새 Pod 부터 전용 마운트를 받는다. 기존 Pod 쪽은
    지울 것이 없다."""
    e = env
    monkeypatch.setattr(main, "delete_pod_util", lambda name, ns: e.v1.delete_namespaced_pod(name, ns))
    monkeypatch.setattr(main.node_image, "commit", lambda settings, node, container, ref, running: 1000)
    monkeypatch.setattr(main.node_image, "remove", lambda settings, node, ref: True)
    monkeypatch.setattr(main, "pod_nfs_enabled", lambda: False)
    old_pod = _provision(e, "322", "exp-np-r2")["result"]["pod_name"]
    assert _home_volume(e, old_pod)["hostPath"]["path"] == "/home/share/user"
    monkeypatch.setattr(main, "pod_nfs_enabled", _REAL_POD_NFS_ENABLED)
    r = e.api.post("/operations/migrate", json={"request_id": "322", "username": "exp-np-r2", "pod_name": old_pod,
                                                "recreate": True})
    assert r.status_code == 202, r.get_json()
    tick(e)
    res = result(e, "migrate", "322")
    assert res["phase"] == "SUCCESS", rows(e, "322")
    new_pod = res["result"]["pod_name"]
    assert _home_volume(e, new_pod)["hostPath"]["path"] == f"/run/provi/nfs/{NS}/{new_pod}"
    assert [c.split()[0] for c in nodes.commands()] == ["nfs-attach"]


def test_recreate_decides_cleanup_by_the_old_pod_not_by_the_current_setting(env, nodes, monkeypatch):
    """판정(pod_nfs_enabled)이 바뀐 뒤 Pod 별 NFS client 로 만든 Pod 를 다시 만들면, 새 Pod 는 노드의 마운트를 받고
    기존 Pod 의 NFS client 는 노드에서 지워진다(지금의 판정이 아니라 기존 Pod 의 설정을 보고 정한다)."""
    e = env
    monkeypatch.setattr(main, "delete_pod_util", lambda name, ns: e.v1.delete_namespaced_pod(name, ns))
    monkeypatch.setattr(main.node_image, "commit", lambda settings, node, container, ref, running: 1000)
    monkeypatch.setattr(main.node_image, "remove", lambda settings, node, ref: True)
    old_pod = _provision(e, "323", "exp-np-r3")["result"]["pod_name"]
    assert _home_volume(e, old_pod)["hostPath"]["path"] == f"/run/provi/nfs/{NS}/{old_pod}"
    monkeypatch.setattr(main, "pod_nfs_enabled", lambda: False)
    nodes.calls.clear()
    r = e.api.post("/operations/migrate", json={"request_id": "323", "username": "exp-np-r3", "pod_name": old_pod,
                                                "recreate": True})
    assert r.status_code == 202, r.get_json()
    tick(e)
    res = result(e, "migrate", "323")
    assert res["phase"] == "SUCCESS", rows(e, "323")
    new_pod = res["result"]["pod_name"]
    assert _home_volume(e, new_pod)["hostPath"]["path"] == "/home/share/user"
    assert nodes.commands() == [f"nfs-detach {NS} {old_pod}"]
    assert nodes.made["farm2"] == {}


# ---------- 정리 작업(krb5-reconcile) ----------

@pytest.fixture
def cluster(monkeypatch):
    """지금 있는 Pod 이름 목록을 돌려주는 쿠버네티스 대역."""
    state = types.SimpleNamespace(pods=[], error=None, selectors=[])

    def list_namespaced_pod(namespace, **kw):
        state.selectors.append((namespace, kw))
        if state.error:
            raise state.error
        return types.SimpleNamespace(items=[types.SimpleNamespace(metadata=types.SimpleNamespace(name=n)) for n in state.pods])
    monkeypatch.setattr(rec, "load_k8s", lambda: None)
    monkeypatch.setattr(rec.client, "CoreV1Api", lambda: types.SimpleNamespace(list_namespaced_pod=list_namespaced_pod))
    return state


def test_reconcile_removes_only_what_has_no_pod(nodes, cluster):
    nodes.made["farm2"] = {(NS, "pod-live"): 9000, (NS, "pod-gone"): 9000, ("ailab-operation", "pod-gone"): 9000}
    nodes.made["farm7"] = {(NS, "pod-gone-7"): 9000}
    cluster.pods = ["pod-live", "some-other-pod"]
    with main.app.app_context():
        rec.reconcile_pod_nfs()
    # 네임스페이스의 모든 Pod 를 기준으로 본다(라벨로 거르지 않는다)
    assert cluster.selectors == [(NS, {})]
    assert nodes.commands("nfs-detach") == [f"nfs-detach {NS} pod-gone 600", f"nfs-detach {NS} pod-gone-7 600"]
    # 살아 있는 Pod 의 것과 다른 스택의 것은 그대로다
    assert nodes.made["farm2"] == {(NS, "pod-live"): 9000, ("ailab-operation", "pod-gone"): 9000}
    assert nodes.made["farm7"] == {}


def test_reconcile_leaves_what_was_attached_a_moment_ago(nodes, cluster):
    """nfs-attach 는 Pod 를 만들기 직전에 불린다. 그 사이에 지우면 곧 생길 Pod 가 마운트 없는 경로를 받는다."""
    nodes.made["farm2"] = {(NS, "pod-being-created"): 3}
    with main.app.app_context():
        rec.reconcile_pod_nfs()
    assert nodes.made["farm2"] == {(NS, "pod-being-created"): 3}


def test_reconcile_does_nothing_when_the_pod_list_is_unknown(nodes, cluster):
    """Pod 목록을 모르는 채로 진행하면 살아 있는 Pod 의 것을 모두 고아로 본다."""
    nodes.made["farm2"] = {(NS, "pod-live"): 9000}
    cluster.error = RuntimeError("apiserver unavailable")
    with main.app.app_context():
        rec.reconcile_pod_nfs()
    assert nodes.calls == []


def test_reconcile_goes_on_when_one_node_does_not_answer(nodes, cluster):
    nodes.down.add("farm2")
    nodes.made["farm7"] = {(NS, "pod-gone"): 9000}
    with main.app.app_context():
        rec.reconcile_pod_nfs()
    assert nodes.made["farm7"] == {}


def test_reconcile_goes_on_when_the_node_keeps_it(nodes, cluster):
    nodes.made["farm2"] = {(NS, "pod-stuck"): 9000, (NS, "pod-gone"): 9000}
    nodes.held.add("pod-stuck")
    with main.app.app_context():
        rec.reconcile_pod_nfs()
    assert nodes.made["farm2"] == {(NS, "pod-stuck"): 9000}
    # 정리 작업은 기다리지 않고 한 번만 묻는다. 남은 것은 다음 주기에 다시 묻는다
    assert nodes.commands("nfs-detach").count(f"nfs-detach {NS} pod-stuck 600") == 1


def test_reconcile_is_off_without_kerberos(nodes, cluster, monkeypatch):
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "")
    nodes.made["farm2"] = {(NS, "pod-gone"): 9000}
    with main.app.app_context():
        rec.reconcile_pod_nfs()
    assert nodes.calls == [] and cluster.selectors == []


def test_revoke_left_busy_is_finished_by_the_next_reconcile(env, nodes):
    """회수 때 노드가 busy 로 답해 남은 것을, Pod 가 사라진 뒤의 정리 작업이 지운다."""
    e = env
    pod_name = _provision(e, "331", "exp-np-c1")["result"]["pod_name"]
    nodes.held.add(pod_name)
    assert _revoke(e, "331", pod_name)["phase"] == "SUCCESS"
    assert (NS, pod_name) in nodes.made["farm2"]
    nodes.held.clear()                       # NAS 가 돌아와 컨테이너가 끝남
    nodes.made["farm2"][(NS, pod_name)] = 9000
    with main.app.app_context():
        rec.reconcile_pod_nfs()
    assert nodes.made["farm2"] == {}
