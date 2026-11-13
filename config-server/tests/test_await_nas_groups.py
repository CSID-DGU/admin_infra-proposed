"""#279 새 계정의 보조 그룹을 NAS 가 답할 때까지 기다린 뒤에 컨테이너를 만든다.

NAS 는 계정의 GSS 컨텍스트를 만들 때 그룹 목록을 한 번 정하고 캐시를 지울 때까지 다시 조회하지 않는다.
AD 그룹에 넣은 직후에 컨테이너가 뜨면 NAS 가 새 멤버십을 답하지 못하는 순간에 첫 GSS 컨텍스트가 생겨,
`id` 에는 그룹이 나오는데 group-dir-share 가 거절된다(2026-11-13 실측).
"""
import json

import pytest

import main
from lifecycle_steps import provision

# conftest 의 nas_groups_ready 가 시험마다 main._wait_nas_groups 를 바꿔 끼운다. 진짜 함수는 여기서 잡아 둔다.
real_wait = main._wait_nas_groups


class Clock:
    """time.sleep 을 실제로 쉬지 않고 시계만 앞으로 돌린다. 쉰 길이를 순서대로 남긴다."""

    def __init__(self, monkeypatch):
        self.now = 1000.0
        self.sleeps = []
        monkeypatch.setattr(main.time, "sleep", self.sleep)
        monkeypatch.setattr(main.time, "monotonic", lambda: self.now)

    def sleep(self, sec):
        self.sleeps.append(sec)
        self.now += sec


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(main, "NAS_GROUP_SETTLE_SEC", 10)
    monkeypatch.setattr(main, "NAS_GROUP_POLL_SEC", 20)
    monkeypatch.setattr(main, "NAS_GROUP_WAIT_SEC", 420)
    return Clock(monkeypatch)


def _nas_answers(monkeypatch, answers, clock=None):
    """NAS 조회를 가로챈다. answers 를 앞에서부터 하나씩 답하고, 다 쓰면 마지막 답을 되풀이한다.
    답이 None 이면 NAS 가 그 계정을 조회하지 못한 것(`id` 실패)이다. 반환: 조회 기록 [(이름들, 하한, 상한, 시각)]"""
    asked = []

    def fake(usernames, gid_min, gid_max):
        asked.append((list(usernames), gid_min, gid_max, clock.now if clock else None))
        answer = answers[min(len(asked), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return {} if answer is None else {u: set(answer) for u in usernames}

    monkeypatch.setattr(main, "nas_shared_gids_for_users", fake)
    return asked


# ---------- _wait_nas_groups ----------

def test_first_query_waits_for_ad_replication(clock, monkeypatch):
    """복제 전에 NAS 에 조회하면 그 조회 때문에 NAS 가 옛 답을 300초 유지한다 — 첫 조회 전에 먼저 쉰다."""
    asked = _nas_answers(monkeypatch, [{70000}], clock)
    assert real_wait("alice", [70000]) == 1
    assert clock.sleeps == [10]
    assert asked[0][3] == 1010.0          # 쉰 뒤에 조회했다


def test_keeps_asking_while_nas_gives_the_old_answer(clock, monkeypatch):
    """제일 중요한 시험 — NAS 가 그룹 없는 답을 유지하는 동안은 컨테이너를 만들러 넘어가지 않는다."""
    asked = _nas_answers(monkeypatch, [set(), set(), set(), {70000}], clock)
    assert real_wait("alice", [70000]) == 4
    assert clock.sleeps == [10, 20, 20, 20]
    assert len(asked) == 4


def test_unknown_account_is_not_an_answer(clock, monkeypatch):
    """NAS 가 계정을 조회하지 못하면(`id` 실패) 결과에서 이름이 빠진다. "그룹 없음"처럼 기다린다."""
    _nas_answers(monkeypatch, [None, None, {70000}], clock)
    assert real_wait("alice", [70000]) == 3


def test_every_group_must_be_there(clock, monkeypatch):
    _nas_answers(monkeypatch, [{70000}, {70000, 70001}], clock)
    assert real_wait("alice", [70000, 70001]) == 2


def test_other_groups_in_the_answer_do_not_matter(clock, monkeypatch):
    _nas_answers(monkeypatch, [{70000, 70005}], clock)
    assert real_wait("alice", [70000]) == 1


def test_gives_up_after_the_limit(clock, monkeypatch):
    asked = _nas_answers(monkeypatch, [set()], clock)
    with pytest.raises(main.NasGroupsTimeout) as e:
        real_wait("alice", [70000])
    # 한도 420초를 20초 간격으로: 첫 조회 + 21번 더.
    assert len(asked) == 22
    assert sum(clock.sleeps) == 10 + 420
    assert "alice" in str(e.value) and "70000" in str(e.value)


def test_limit_message_tells_unknown_account_from_missing_group(clock, monkeypatch):
    _nas_answers(monkeypatch, [None], clock)
    with pytest.raises(main.NasGroupsTimeout, match="계정을 조회하지 못함"):
        real_wait("alice", [70000])


def test_query_range_follows_the_wanted_gids(clock, monkeypatch):
    """조회 함수는 범위 밖 gid 를 걸러 낸다. 공용 대역 설정과 어긋난 gid 를 기다려도 걸러지지 않게 범위를 맞춘다."""
    monkeypatch.setattr(main, "SHARED_GID_MIN", 70000)
    monkeypatch.setattr(main, "SHARED_GID_MAX", 79999)
    asked = _nas_answers(monkeypatch, [{60010, 90001}], clock)
    assert real_wait("alice", [90001, 60010]) == 1
    assert asked[0][:3] == (["alice"], 60010, 90001)


def test_nothing_to_wait_for(clock, monkeypatch):
    asked = _nas_answers(monkeypatch, [set()], clock)
    assert real_wait("alice", []) == 0
    assert asked == [] and clock.sleeps == []


def test_nas_failure_is_passed_to_the_caller(clock, monkeypatch):
    _nas_answers(monkeypatch, [RuntimeError("ssh: connect to host nas port 22: timed out")], clock)
    with pytest.raises(RuntimeError, match="timed out"):
        real_wait("alice", [70000])


def test_range_filter_of_the_real_query_function(monkeypatch):
    """가짜 조회가 아니라 진짜 nas_shared_gids_for_users 의 범위 걸러내기와 맞물리는지 본다."""
    import utils

    class FakeSsh:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(utils, "_nas_ssh_client", lambda: FakeSsh())
    monkeypatch.setattr(utils, "_ssh_capture", lambda ssh, script: (0, "alice|50001 513 60010 80000 90001\n"))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    assert real_wait("alice", [90001, 60010]) == 1
    assert real_wait("alice", [80000]) == 1


# ---------- step_await_nas_groups ----------

@pytest.fixture
def ad_on(monkeypatch, pod_status):
    """AD 를 쓰는 환경으로 둔다. 단계가 남기는 진행 문구는 Redis 대신 pod_status 목록으로 받는다."""
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "TEST.REALM")


@pytest.fixture
def flush_calls(monkeypatch):
    import reconcile_krb5
    calls = []
    monkeypatch.setattr(reconcile_krb5, "trigger_nas_gss_flush_ondemand", lambda: calls.append(1) or True)
    return calls


def _ctx(**over):
    ctx = {"request_id": "r1", "name": "alice", "username": "alice",
           "supp_groups": [{"name": "teamx", "gid": 70000}, {"name": "teamy", "gid": "70001"}]}
    ctx.update(over)
    return ctx


def _rows(logs):
    return [(r["phase"].value, r.get("error_code")) for r in logs]


def test_step_waits_for_every_supp_group(ad_on, logs, nas_groups_ready, flush_calls):
    with main.app.app_context():
        provision.step_await_nas_groups(_ctx())
    assert nas_groups_ready == [("alice", [70000, 70001])]
    assert _rows(logs) == [("START", None), ("SUCCESS", None)]
    assert {r["resource_type"] for r in logs} == {"nas_groups"}
    assert json.loads(logs[-1]["error_detail"]) == {"nas_polls": 1}
    assert flush_calls == []              # NAS 가 답했으면 캐시를 지울 일이 없다


def test_step_tells_the_approval_screen_why_it_waits(ad_on, logs, pod_status, monkeypatch):
    """계정 단계들은 진행 문구를 남기지 않아 승인 화면의 "현재 단계"가 "요청 접수"에 머문다. 이 단계는 5분쯤
    기다릴 수 있으므로 기다리기 전에 이유를 적는다. stage 는 화면이 아는 값(started)이어야 한다."""
    seen_at_wait = []
    monkeypatch.setattr(main, "_wait_nas_groups", lambda u, g: seen_at_wait.append(list(pod_status)) or 1)
    with main.app.app_context():
        provision.step_await_nas_groups(_ctx())
    assert pod_status == [("r1", "started", "NAS 공유 그룹 반영 확인 중")]
    assert seen_at_wait == [[("r1", "started", "NAS 공유 그룹 반영 확인 중")]]      # 기다리기 전에 적었다


def test_step_is_skipped_without_supp_groups(ad_on, logs, nas_groups_ready, pod_status):
    with main.app.app_context():
        provision.step_await_nas_groups(_ctx(supp_groups=[]))
        provision.step_await_nas_groups({"request_id": "r1", "name": "alice"})
    assert nas_groups_ready == [] and logs == []
    assert pod_status == []               # 기다리지 않으면 문구도 바꾸지 않는다


def test_step_is_skipped_when_ad_is_disabled(logs, nas_groups_ready, pod_status, monkeypatch):
    monkeypatch.setitem(main.app.config, "KRB5_REALM", "")
    with main.app.app_context():
        provision.step_await_nas_groups(_ctx())
    assert nas_groups_ready == [] and logs == []
    assert pod_status == []


@pytest.mark.parametrize("failure", [
    main.NasGroupsTimeout("NAS 가 alice의 보조 그룹 [70000]을 420초 안에 답하지 않음"),
    RuntimeError("ssh: connect to host nas port 22: timed out"),
])
def test_unconfirmed_does_not_fail_the_job_and_starts_the_flush(ad_on, logs, flush_calls, monkeypatch, failure):
    """확인하지 못해도 계정과 컨테이너는 쓸 수 있다 — 이 단계가 없던 때와 같은 상태로 넘기고,
    30분 크론보다 먼저 고쳐지게 캐시 지우기를 띄운다."""
    def fail(username, gids):
        raise failure
    monkeypatch.setattr(main, "_wait_nas_groups", fail)
    with main.app.app_context():
        provision.step_await_nas_groups(_ctx())          # 예외가 밖으로 나오지 않는다
    assert _rows(logs) == [("START", None), ("FAIL", "NAS_GROUPS_UNCONFIRMED")]
    assert str(failure) in logs[-1]["error_detail"]
    assert flush_calls == [1]


def test_flush_start_failure_does_not_fail_the_job_either(ad_on, logs, monkeypatch):
    import reconcile_krb5

    def boom():
        raise RuntimeError("redis down")
    monkeypatch.setattr(reconcile_krb5, "trigger_nas_gss_flush_ondemand", boom)
    monkeypatch.setattr(main, "_wait_nas_groups", lambda u, g: (_ for _ in ()).throw(main.NasGroupsTimeout("x")))
    with main.app.app_context():
        provision.step_await_nas_groups(_ctx())


def test_step_is_the_last_account_step_and_safe_to_rerun():
    """AD 그룹에 넣은 뒤에 기다려야 답이 바뀐다. 컨테이너 단계보다 앞이어야 첫 GSS 컨텍스트가 그 뒤에 생긴다."""
    names = [s.__name__ for s in main.ACCOUNT_CREATE_STEPS]
    assert names.index("step_await_nas_groups") > names.index("step_sync_ad_groups")
    assert names[-1] == "step_await_nas_groups"
    assert "step_await_nas_groups" in main.RERUN_SAFE
    # 이미 있는 계정에 그룹만 더하는 경로는 기다리지 않는다 — 그 계정의 GSS 컨텍스트는 이미 있어서
    # 기다려도 바뀌지 않고, 캐시 지우기(step_trigger_nas_gss_flush)가 고친다.
    assert "step_await_nas_groups" not in [s.__name__ for s in main.SUPP_GROUPS_ONLY_STEPS]


def test_provision_job_puts_the_wait_before_the_pod_steps():
    job = {"account": {"name": "alice", "supp_groups": [{"name": "teamx", "gid": 70000}]}}
    names = [s.__name__ for s in main._job_steps("provision", job)]
    assert names.index("step_await_nas_groups") < names.index("step_fetch_user_config")
