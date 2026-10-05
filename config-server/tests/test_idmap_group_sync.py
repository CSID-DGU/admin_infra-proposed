"""공유 그룹을 farm 노드의 NFS 이름 변환(ailab-krb5-admin idmap-group-sync)에 등록한다.

AD 멤버 노드는 chgrp 때 그룹 이름을 FARM\\ 없이 NAS 에 보내 거부된다. 노드 액션이 그룹마다
/etc/idmapd.conf 에 정식 이름 한 줄을 넣고, config-server 는 새 공유 그룹 직후(백그라운드)와
30분 크론에서 공유 그룹 이름을 넘긴다. 등록이 실패해도 그룹 작업은 성공으로 둔다.
"""
import pathlib

import pytest

import main
import reconcile_krb5 as rec
from test_e2e_virtual import env, tick, rows, result, PW  # noqa: F401  (env는 pytest fixture)

_REAL_TRIGGER = rec.trigger_idmap_sync_ondemand   # 대역으로 바꾼 시험에서도 진짜를 다시 끼울 수 있게

NODES = [{"name": "farm1", "host": "h1", "port": "8081"},
         {"name": "farm2", "host": "h2", "port": "8082"},
         {"name": "farm8", "host": "h8", "port": "8088"}]


@pytest.fixture
def farm(monkeypatch):
    """노드 SSH 대역. 반환: (호출 목록 [(host, 명령, 표준입력)], 노드별 응답·예외를 정하는 dict)"""
    calls, replies = [], {}
    monkeypatch.setitem(main.app.config, "FARM_NODES", NODES)

    def fake(host, port, cmd, stdin_data=""):
        calls.append((host, cmd, stdin_data))
        r = replies.get(host, "")
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(rec, "_farm_ssh", fake)
    return calls, replies


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """계정 대장을 임시 파일로 돌린다. 반환: group 줄을 쓰는 함수"""
    base = str(tmp_path / "etc")
    for k, val in list(main.app.config.items()):
        if isinstance(val, str) and val.startswith(main.BASE_ETC_DIR):
            monkeypatch.setitem(main.app.config, k, base + val[len(main.BASE_ETC_DIR):])
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "TEST.REALM")
    monkeypatch.setattr(rec, "SHARED_GID_MIN", 70000)
    monkeypatch.setattr(rec, "SHARED_GID_MAX", 79999)
    with main.app.app_context():
        main.ensure_etc_layout()

        def write_group(*lines):
            main.write_group_lines(list(lines))
        yield write_group


@pytest.fixture
def sync_thread(monkeypatch):
    """백그라운드 스레드를 즉시 실행으로 바꾼다. 반환: 띄운 스레드의 (name, daemon) 목록"""
    started = []

    class _Now:
        def __init__(self, target, daemon=False, name=None, **kw):
            self.target, self.daemon, self.name = target, daemon, name

        def start(self):
            started.append((self.name, self.daemon))
            self.target()
    monkeypatch.setattr(rec.threading, "Thread", _Now)
    return started


@pytest.fixture
def warnings(monkeypatch):
    """경고·정보 로그를 모은다. 반환: (level, 메시지) 목록"""
    seen = []
    monkeypatch.setattr(main.app.logger, "warning", lambda msg, *a, **k: seen.append(("warning", msg)))
    monkeypatch.setattr(main.app.logger, "info", lambda msg, *a, **k: seen.append(("info", msg)))
    return seen


# ---------- 대장 → 노드 (30분 크론) ----------

def test_reconcile_sends_only_shared_band_groups_to_every_node(ledger, farm):
    calls, _ = farm
    ledger("root:x:0:", "alice:x:21000:", "team_b:x:70001:alice", "team_a:x:70000:",
           "over_band:x:80000:", "below_band:x:69999:")
    with main.app.app_context():
        rec.reconcile_idmap_groups()
    assert calls == [(h, "idmap-group-sync", "team_a\nteam_b\n") for h in ("h1", "h2", "h8")]


def test_reconcile_without_upper_bound_takes_every_group_above_the_minimum(ledger, farm, monkeypatch):
    calls, _ = farm
    monkeypatch.setattr(rec, "SHARED_GID_MAX", None)
    ledger("alice:x:21000:", "team_a:x:70000:", "far:x:95000:")
    with main.app.app_context():
        rec.reconcile_idmap_groups()
    assert {c[2] for c in calls} == {"far\nteam_a\n"}


@pytest.mark.parametrize("lines", [(), ("alice:x:21000:", "root:x:0:")])
def test_reconcile_without_shared_groups_does_not_ssh(ledger, farm, lines):
    calls, _ = farm
    ledger(*lines)
    with main.app.app_context():
        rec.reconcile_idmap_groups()
    assert calls == []


def test_reconcile_does_nothing_when_ad_is_off(ledger, farm, monkeypatch):
    calls, _ = farm
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "")
    ledger("team_a:x:70000:")
    with main.app.app_context():
        rec.reconcile_idmap_groups()
    assert calls == []


def test_cron_runs_idmap_sync_last():
    """노드가 응답하지 않아도 앞의 재조정(keytab 정리·NAS 캐시)은 이미 끝나 있어야 한다."""
    src = pathlib.Path(rec.__file__).read_text(encoding="utf-8")
    main_block = src[src.index('if __name__ == "__main__":'):]
    order = [line.strip() for line in main_block.splitlines() if line.strip().startswith("reconcile_")]
    assert order == ["reconcile_krb5_cleanup_pending()", "reconcile_krb5_orphans()",
                     "reconcile_nas_gss_cache()", "reconcile_idmap_groups()"]


# ---------- 노드 호출 ----------

def test_invalid_names_are_not_sent_and_are_reported(farm, warnings):
    """노드는 이름 하나만 틀려도 종료 코드 1을 내고, 그러면 같은 묶음의 결과를 볼 수 없다 — 미리 거른다."""
    calls, _ = farm
    with main.app.app_context():
        rec.sync_idmap_groups(["team_a", "Bad Name", "x;rm -rf /", "UPPER", "team_a"])
    assert {c[2] for c in calls} == {"team_a\n"}
    assert any(level == "warning" and "Bad Name" in msg for level, msg in warnings)


def test_only_invalid_names_means_no_ssh(farm):
    calls, _ = farm
    with main.app.app_context():
        rec.sync_idmap_groups(["Bad Name", ""])
    assert calls == []


def test_one_node_failing_does_not_stop_the_others(farm, warnings):
    calls, replies = farm
    replies["h1"] = RuntimeError("farm SSH 실패 (h1:8081): " + "debug1: " * 400 + "끝")
    with main.app.app_context():
        rec.sync_idmap_groups(["team_a"])          # 예외가 밖으로 나오지 않는다
    assert [c[0] for c in calls] == ["h1", "h2", "h8"]
    failed = [msg for level, msg in warnings if level == "warning" and "farm1" in msg]
    assert len(failed) == 1 and failed[0].endswith("끝") and len(failed[0]) < 700   # 긴 ssh -v 출력은 잘라 남긴다


def test_only_changes_and_problems_are_logged(farm, warnings):
    _, replies = farm
    replies["h1"] = ("idmap_group_state=added group=team_a\n"
                     "idmap_group_state=present group=team_b\n")
    replies["h2"] = "idmap_group_state=skipped\n"
    replies["h8"] = "idmap_group_state=conflict group=team_c\n"
    with main.app.app_context():
        rec.sync_idmap_groups(["team_a", "team_b", "team_c"])
    logged = [msg for level, msg in warnings if "[IDMAP]" in msg]
    assert logged == ["[IDMAP] farm1: idmap_group_state=added group=team_a",
                      "[IDMAP] farm8: idmap_group_state=conflict group=team_c"]


def _failed_with_output(stdout):
    """_farm_ssh 가 원격 종료 코드 1일 때 내는 것과 같은 예외."""
    err = RuntimeError("farm SSH 실패 (h1:8081): debug1: Exit status 1")
    err.stdout = stdout
    return err


def test_conflict_with_exit_code_1_still_shows_which_group(farm, warnings):
    """노드는 conflict 가 있으면 종료 코드 1로 끝나지만 그룹별 결과는 출력한다 — 어느 그룹인지 로그에 남아야 한다."""
    _, replies = farm
    replies["h1"] = _failed_with_output("idmap_group_state=added group=team_a\n"
                                        "idmap_group_state=present group=team_b\n"
                                        "idmap_group_state=conflict group=docker\n")
    with main.app.app_context():
        rec.sync_idmap_groups(["team_a", "team_b", "docker"])
    farm1 = [msg for level, msg in warnings if "farm1" in msg]
    assert any(msg.startswith("[IDMAP] farm1 공유 그룹 등록 실패") for msg in farm1)
    assert "[IDMAP] farm1: idmap_group_state=conflict group=docker" in farm1
    assert "[IDMAP] farm1: idmap_group_state=added group=team_a" in farm1
    assert not any("present" in msg for msg in farm1)


@pytest.mark.parametrize("stdout", [None, b"idmap_group_state=added group=team_a\n", ""])
def test_failures_without_text_output_are_still_handled(farm, warnings, stdout):
    """시간 초과(TimeoutExpired)의 stdout 은 bytes·None 일 수 있다 — 읽다가 터지지 않고 다음 노드로 간다."""
    import subprocess
    calls, replies = farm
    err = subprocess.TimeoutExpired(cmd="ssh", timeout=150)
    err.stdout = stdout
    replies["h1"] = err
    with main.app.app_context():
        rec.sync_idmap_groups(["team_a"])
    assert [c[0] for c in calls] == ["h1", "h2", "h8"]
    assert not any(msg.startswith("[IDMAP] farm1: ") for _, msg in warnings)


def test_farm_ssh_keeps_the_message_and_attaches_stdout_on_failure(monkeypatch):
    """예외 메시지는 예전과 같고(다른 호출자 영향 없음), 원격 표준출력만 예외에 붙는다."""
    import types
    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=1, stdout="idmap_group_state=conflict group=docker\n", stderr="debug1: Exit status 1\n"))
    with main.app.app_context(), pytest.raises(RuntimeError) as exc:
        main._farm_ssh("farm1", "8081", "idmap-group-sync", stdin_data="docker\n")
    assert str(exc.value) == "farm SSH 실패 (farm1:8081): debug1: Exit status 1"
    assert exc.value.stdout == "idmap_group_state=conflict group=docker\n"


def test_farm_ssh_success_is_unchanged(monkeypatch):
    import types
    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=0, stdout="idmap_group_state=present group=team_a\n", stderr=""))
    with main.app.app_context():
        assert main._farm_ssh("farm1", "8081", "idmap-group-sync") == "idmap_group_state=present group=team_a\n"


# ---------- 백그라운드 실행 ----------

def test_trigger_runs_the_sync_in_a_daemon_thread(farm, sync_thread):
    calls, _ = farm
    with main.app.app_context():
        assert rec.trigger_idmap_sync_ondemand(["team_b", "team_a", "team_b"]) is True
    assert sync_thread == [("idmap-group-sync", True)]
    assert {c[2] for c in calls} == {"team_a\nteam_b\n"}


@pytest.mark.parametrize("names,nodes", [([], NODES), (None, NODES), (["team_a"], [])])
def test_trigger_does_nothing_without_names_or_nodes(farm, sync_thread, monkeypatch, names, nodes):
    calls, _ = farm
    monkeypatch.setitem(main.app.config, "FARM_NODES", nodes)
    with main.app.app_context():
        assert rec.trigger_idmap_sync_ondemand(names) is False
    assert sync_thread == [] and calls == []


def test_errors_inside_the_thread_are_swallowed(farm, sync_thread, monkeypatch):
    def boom(names):
        raise RuntimeError("예상 못 한 오류")
    monkeypatch.setattr(rec, "sync_idmap_groups", boom)
    with main.app.app_context():
        assert rec.trigger_idmap_sync_ondemand(["team_a"]) is True   # 스레드 안 예외가 새지 않는다


def test_trigger_returns_without_waiting_for_slow_nodes(farm, monkeypatch):
    """진짜 스레드로 띄우고, 노드가 끝나기 전에 돌아오는지 본다."""
    import threading
    release, entered = threading.Event(), threading.Event()

    def slow(host, port, cmd, stdin_data=""):
        entered.set()
        release.wait(5)
        return ""
    monkeypatch.setattr(rec, "_farm_ssh", slow)
    import time
    t0 = time.monotonic()
    with main.app.app_context():
        assert rec.trigger_idmap_sync_ondemand(["team_a"]) is True
    returned_in = time.monotonic() - t0
    try:
        assert entered.wait(5)          # 스레드는 노드 호출에 들어가 있는데
        assert returned_in < 1.0        # 호출자는 노드를 기다리지 않고 이미 돌아와 있다
    finally:
        release.set()


# ---------- 그룹 작업에서 부르기 ----------

@pytest.fixture
def group_env(ledger, monkeypatch):
    """관리자 그룹 생성 작업 대역. 반환: (AD 로 나간 명령, 등록에 넘긴 이름 목록)"""
    sent, registered = [], []
    monkeypatch.setattr(main, "_farm_ad_ssh", lambda cmd, stdin_data="": sent.append(cmd) or "")
    monkeypatch.setattr(main, "SHARED_GID_MIN", 70000)
    monkeypatch.setattr(main, "SHARED_GID_MAX", 79999)
    monkeypatch.setattr(main, "UID_MIN", 21000)
    monkeypatch.setattr(main, "UID_MAX", 49999)
    monkeypatch.setattr(rec, "trigger_idmap_sync_ondemand", lambda names: registered.append(list(names)) or True)
    return sent, registered


def test_admin_group_create_registers_the_group_after_ad(group_env, group_job, monkeypatch):
    sent, registered = group_env
    order = []
    monkeypatch.setattr(main, "_farm_ad_ssh", lambda cmd, stdin_data="": order.append(("ad", cmd)) or "")
    monkeypatch.setattr(rec, "trigger_idmap_sync_ondemand", lambda names: order.append(("idmap", list(names))) or True)
    r = group_job("create", **{"name": "teamx", "gid": 70000, "members": []})
    assert r.phase == "SUCCESS"
    assert order == [("ad", "group-create teamx 70000"), ("idmap", ["teamx"])]


def test_admin_group_create_does_not_register_when_ad_fails(group_env, group_job, monkeypatch):
    _, registered = group_env

    def boom(cmd, stdin_data=""):
        raise RuntimeError("AD DC 접속 실패")
    monkeypatch.setattr(main, "_farm_ad_ssh", boom)
    r = group_job("create", **{"name": "teamx", "gid": 70000})
    assert r.phase != "SUCCESS"
    assert registered == []


def test_registration_failure_does_not_fail_the_group_job(group_env, group_job, monkeypatch):
    def boom(names):
        raise RuntimeError("스레드를 띄울 수 없음")
    monkeypatch.setattr(rec, "trigger_idmap_sync_ondemand", boom)
    r = group_job("create", **{"name": "teamx", "gid": 70000})
    assert r.phase == "SUCCESS"


def test_no_registration_when_ad_is_off(group_env, group_job, monkeypatch):
    _, registered = group_env
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "")
    r = group_job("create", **{"name": "teamx", "gid": 70000})
    assert r.phase == "SUCCESS"
    assert registered == []


def test_admin_group_create_reaches_every_node(group_env, group_job, farm, sync_thread, monkeypatch):
    """대역 없이 실제 트리거부터 노드 호출까지 이어서 본다."""
    calls, _ = farm
    monkeypatch.setattr(rec, "trigger_idmap_sync_ondemand", _REAL_TRIGGER)
    r = group_job("create", **{"name": "teamx", "gid": 70000})
    assert r.phase == "SUCCESS"
    assert calls == [(h, "idmap-group-sync", "teamx\n") for h in ("h1", "h2", "h8")]
    assert sync_thread == [("idmap-group-sync", True)]


# ---------- 신청에서 새 공유 그룹 ----------

@pytest.fixture
def new_group_env(env, monkeypatch):
    """신청 경로 대역. 반환: 등록에 넘긴 이름 목록"""
    registered = []
    monkeypatch.setattr(main, "_farm_ad_ssh", lambda cmd, stdin_data="": "")
    monkeypatch.setattr(rec, "trigger_nas_gss_flush_ondemand", lambda: True)
    monkeypatch.setattr(rec, "trigger_idmap_sync_ondemand", lambda names: registered.append(sorted(names)) or True)
    monkeypatch.setattr(main, "SHARED_GID_MIN", 70000)
    monkeypatch.setattr(main, "SHARED_GID_MAX", 79999)
    return registered


def _provision(env, rid, username, groups, was_groups):
    env.was = lambda url: env.Resp(200, {"image": "dguailab/decs:1", "passwd_base64": PW, "groups": was_groups,
                                         "gpu_nodes": [{"node_name": "farm2", "num_gpu": 1, "gpu_models": ["RTX A5000"],
                                                        "cpu_limit": "4", "memory_limit": "16Gi"}]})
    return env.api.post("/operations/provision", json={
        "request_id": rid, "username": username,
        "account": {"passwd_base64": PW, "supplementary_groups": groups}})


def _seed_group(line):
    with main.app.app_context():
        main.ensure_etc_layout()
        main.write_group_lines(main.read_group_lines() + [line])


def test_new_group_in_a_request_registers_only_the_new_group(env, new_group_env):
    _seed_group("ailab:x:70001:")
    assert _provision(env, "901", "exp-np-ng", [{"name": "ailab", "gid": 70001}, {"name": "vision-lab"}],
                      [{"gid": 70001, "name": "ailab"}, {"gid": None, "name": "vision-lab"}]).status_code == 202
    tick(env)
    assert result(env, "provision", "901")["phase"] == "SUCCESS", rows(env, "901")
    assert new_group_env == [["vision-lab"]]


def test_request_with_only_existing_groups_registers_nothing(env, new_group_env):
    _seed_group("ailab:x:70001:")
    _provision(env, "902", "exp-np-ng", [{"name": "ailab", "gid": 70001}], [{"gid": 70001, "name": "ailab"}])
    tick(env)
    assert result(env, "provision", "902")["phase"] == "SUCCESS", rows(env, "902")
    assert new_group_env == []


def test_rejected_new_group_name_registers_nothing(env, new_group_env):
    """이미지 예약 이름이라 그룹을 만들지 못하면 노드에도 올리지 않는다."""
    _provision(env, "903", "exp-np-ng", [{"name": "video"}], [{"gid": None, "name": "video"}])
    tick(env)
    assert result(env, "provision", "903")["phase"] != "SUCCESS"
    assert new_group_env == []


def test_registration_failure_does_not_fail_the_request(env, new_group_env, monkeypatch):
    def boom(names):
        raise RuntimeError("스레드를 띄울 수 없음")
    monkeypatch.setattr(rec, "trigger_idmap_sync_ondemand", boom)
    _provision(env, "904", "exp-np-ng", [{"name": "vision-lab"}], [{"gid": None, "name": "vision-lab"}])
    tick(env)
    assert result(env, "provision", "904")["phase"] == "SUCCESS", rows(env, "904")
