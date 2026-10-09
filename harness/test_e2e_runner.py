"""E2E 실행기·정리기의 판정 규칙을 가짜 포트로 확인한다(클러스터 없음)."""
import re

import pytest

from e2e.resetter import Resetter
from e2e.runner import Context, Run, StepFailed


class FakeCluster:
    """SQL·셸을 기록하고, 등록한 규칙(정규식 → 결과)으로 답한다."""

    def __init__(self, stack="full"):
        self.stack = stack
        self.calls = []
        self.rules = []

    def on(self, pattern, result):
        self.rules.append((re.compile(pattern, re.S), result))

    def _answer(self, text):
        self.calls.append(text)
        for pattern, result in self.rules:
            if pattern.search(text):
                return result(text) if callable(result) else result
        return None

    def sql(self, statement, database="web_admin"):
        return self._answer(statement) or []

    def sh(self, script, timeout=300):
        return self._answer(script) or ""

    def config_server_python(self, code, timeout=300):
        return self._answer(code) or ""


class FakeApi:
    def __init__(self, responses=None):
        self.calls = []
        self.bodies = []
        self.responses = responses or {}

    def call(self, method, path, *, as_user, body=None):
        self.calls.append((method, path, as_user))
        self.bodies.append(body)
        return self.responses.get((method, path), (200, {"data": {"requestId": 101}}))


class FakeFaults:
    def __init__(self):
        self.healed = 0

    def heal_all(self):
        self.healed += 1


def make_run(cluster, api, faults=None):
    run = Run(cluster, api, faults or FakeFaults(), stack_prefix="exp-fu-", run_id="abc12", interval=0,
              wait_timeout=0)
    run.admin_id, run.resource_group, run.image = 1, 1, 46
    return run


def test_names_are_scoped_to_the_run_and_case():
    cluster, api = FakeCluster(), FakeApi()
    cluster.on(r"SELECT user_id FROM users WHERE email", [("7",)])
    run = make_run(cluster, api)
    result = run.run_case({"id": "C02", "steps": [{"user": "a"}]}, allow_faults=False)
    assert result["result"] == "PASS"
    assert any("'exp-fu-e2eabc12c02a'" in c for c in cluster.calls)


def test_failed_step_is_reported_and_faults_are_always_healed():
    cluster, api, faults = FakeCluster(), FakeApi(), FakeFaults()
    cluster.on(r"SELECT user_id FROM users WHERE email", [("7",)])
    cluster.on(r"SELECT status, IFNULL\(node_name", [("PENDING", "", "")])
    run = make_run(cluster, api, faults)
    case = {"id": "C09", "steps": [{"user": "a"}, {"apply": {"user": "a", "as": "r1"}}, {"expect_status": {"r1": "DENIED"}}]}
    result = run.run_case(case, allow_faults=False)
    assert result["result"] == "FAIL" and result["step"] == 3
    assert "DENIED" in result["error"]
    assert faults.healed == 1


def test_each_step_is_reported_before_it_runs():
    cluster, api = FakeCluster(), FakeApi()
    cluster.on(r"SELECT user_id FROM users WHERE email", [("7",)])
    cluster.on(r"SELECT status, IFNULL\(node_name", [("PENDING", "", "")])
    run = make_run(cluster, api)
    lines = []
    run.progress = lines.append
    case = {"id": "C09", "steps": [{"user": "a"}, {"apply": {"user": "a", "as": "r1"}}, {"expect_status": {"r1": "DENIED"}}]}
    run.run_case(case, allow_faults=False)
    # 실패한 단계까지 알린다 — 어디서 멈췄는지 끝나기 전에도 보여야 한다.
    assert [line.split()[1] for line in lines] == ["[1/3]", "[2/3]", "[3/3]"]
    assert "expect_status" in lines[-1]


def test_wait_job_tells_degraded_apart_from_plain_failure():
    cluster, api = FakeCluster(), FakeApi()
    cluster.on(r"SELECT user_id FROM users WHERE email", [("7",)])
    cluster.on(r"SELECT phase, IFNULL\(error_code", [("FAIL", "DEGRADED")])
    cluster.on(r"SELECT DISTINCT error_code", [("DEGRADED",), ("NAS_SSH_FAILED",)])
    run = make_run(cluster, api)
    steps = [{"user": "a"}, {"apply": {"user": "a", "as": "r1"}}]
    ok = run.run_case({"id": "C09", "steps": steps + [{"wait_job": {"r1": "DEGRADED"}}]}, allow_faults=False)
    assert ok["result"] == "PASS"
    bad = run.run_case({"id": "C10", "steps": steps + [{"wait_job": {"r1": "FAIL"}}]}, allow_faults=False)
    assert bad["result"] == "FAIL" and "DEGRADED" in bad["error"]


def test_api_error_fails_unless_error_was_expected():
    cluster = FakeCluster()
    cluster.on(r"SELECT user_id FROM users WHERE email", [("7",)])
    api = FakeApi({("DELETE", "/api/admin/requests/101/container"): (500, {"message": "config-server down"})})
    run = make_run(cluster, api)
    base = [{"user": "a"}, {"apply": {"user": "a", "as": "r1"}}]
    assert run.run_case({"id": "X1", "steps": base + [{"reclaim_container": "r1"}]}, allow_faults=True)["result"] == "FAIL"
    assert run.run_case({"id": "X2", "steps": base + [{"reclaim_container": {"req": "r1", "expect": "error"}}]},
                        allow_faults=True)["result"] == "PASS"


def test_fault_case_is_skipped_without_permission():
    run = make_run(FakeCluster(), FakeApi())
    result = run.run_case({"id": "F09", "steps": [{"fault": {"kill": "controller"}}]}, allow_faults=False)
    assert result["result"] == "SKIP"


def test_uid_must_stay_with_the_person():
    cluster = FakeCluster()
    cluster.on(r"SELECT user_id FROM users WHERE email", [("7",)])
    uids = iter([("55003", "55003", "1"), ("55004", "55004", "1")])
    cluster.on(r"SELECT IFNULL\(ubuntu_uid", lambda _: [next(uids)])
    run = make_run(cluster, FakeApi())
    case = {"id": "C06", "steps": [{"user": "a"}, {"remember_uid": {"user": "a", "as": "u"}},
                                   {"expect_user": {"a": {"uid": {"same_as": "u"}}}}]}
    result = run.run_case(case, allow_faults=False)
    assert result["result"] == "FAIL" and "55003" in result["error"]


def _fulfilled_cluster():
    """컨테이너가 떠 있는 사용자 하나."""
    cluster = FakeCluster()
    cluster.on(r"SELECT user_id FROM users WHERE email", [("7",)])
    cluster.on(r"SELECT status, IFNULL\(node_name", [("FULFILLED", "farm1", "ailab-exp-fu-e2eabc12c09a-1")])
    return cluster


PASSWORD_STEPS = [{"user": "a"}, {"apply": {"user": "a", "as": "r1"}}, {"reset_password": "a"},
                  {"wait_password": {"a": "FULFILLED"}}]


def test_password_reset_passes_when_pod_and_record_agree_on_a_new_hash():
    cluster, api = _fulfilled_cluster(), FakeApi()
    recorded = iter(["OLD", "NEW"])
    cluster.on(r"SELECT IFNULL\(ubuntu_password_hash", lambda _: [(next(recorded),)])
    cluster.on(r"FROM password_reset_requests", [("FULFILLED",)])
    cluster.on(r"getent shadow", "NEW\n")
    run = make_run(cluster, api)

    result = run.run_case({"id": "C09", "steps": PASSWORD_STEPS + [{"expect_password": {"r1": "changed"}}]},
                          allow_faults=False)

    assert result["result"] == "PASS"
    assert ("PUT", "/api/admin/users/7/password", 1) in api.calls
    assert any("getent shadow exp-fu-e2eabc12c09a" in c for c in cluster.calls)
    # 해시는 관측 기록에 남기지 않는다(공개 저장소의 실행 결과에 실린다).
    assert "NEW" not in str(result["observed"]) and "OLD" not in str(result["observed"])


def test_password_reset_fails_when_the_pod_kept_the_old_hash():
    """웹 계정 기록만 바뀌고 컨테이너가 옛 비밀번호로 남는 것이 이 사례가 잡으려는 결함이다."""
    cluster = _fulfilled_cluster()
    recorded = iter(["OLD", "NEW"])
    cluster.on(r"SELECT IFNULL\(ubuntu_password_hash", lambda _: [(next(recorded),)])
    cluster.on(r"FROM password_reset_requests", [("FULFILLED",)])
    cluster.on(r"getent shadow", "OLD\n")

    result = make_run(cluster, FakeApi()).run_case(
        {"id": "C09", "steps": PASSWORD_STEPS + [{"expect_password": {"r1": "changed"}}]}, allow_faults=False)

    assert result["result"] == "FAIL" and "기록과 다름" in result["error"]
    assert "OLD" not in result["error"] and "NEW" not in result["error"]


def test_password_reset_that_never_applies_is_a_failure():
    cluster = _fulfilled_cluster()
    cluster.on(r"SELECT IFNULL\(ubuntu_password_hash", [("OLD",)])
    cluster.on(r"FROM password_reset_requests", [("PENDING",)])

    result = make_run(cluster, FakeApi()).run_case({"id": "C09", "steps": PASSWORD_STEPS}, allow_faults=False)

    assert result["result"] == "FAIL" and result["step"] == 4
    assert "FULFILLED" in result["error"] and "PENDING" in result["error"]


def test_rejected_password_reset_must_leave_the_password_alone():
    cluster = _fulfilled_cluster()
    cluster.on(r"SELECT IFNULL\(ubuntu_password_hash", [("OLD",)])
    cluster.on(r"getent shadow", "OLD\n")
    api = FakeApi({("PUT", "/api/admin/users/7/password"): (409, {"message": "컨테이너를 만드는 중"})})
    steps = [{"user": "a"}, {"apply": {"user": "a", "as": "r1"}}, {"reset_password": {"user": "a", "expect": "error"}}]

    kept = make_run(cluster, api).run_case(
        {"id": "C11", "steps": steps + [{"expect_password": {"r1": "unchanged"}}]}, allow_faults=False)
    assert kept["result"] == "PASS"
    wrong = make_run(cluster, api).run_case(
        {"id": "C11", "steps": steps + [{"expect_password": {"r1": "changed"}}]}, allow_faults=False)
    assert wrong["result"] == "FAIL" and "그대로" in wrong["error"]


# ---------- 공용 그룹: 승인 대기 그룹과 홈 아래 폴더 공유 ----------

def test_new_group_has_no_job_and_the_application_selects_it_by_group_id():
    """admin_be 는 새 그룹을 자기 기록에만 만든다(작업 번호 없음, gid 없음). 그 그룹을 고른 신청이 인프라에 만든다."""
    cluster = _fulfilled_cluster()
    made = iter([[], [("3", "")]])
    cluster.on(r"FROM `groups` WHERE group_name='exp-fu-e2e-team'", lambda _: next(made, [("3", "")]))
    api = FakeApi({("POST", "/api/groups"): (201, {"data": {"groupId": 3, "ubuntuGid": None}})})
    steps = [{"user": "a"}, {"create_group": {"user": "a", "as": "g"}}, {"wait_group": {"g": "APPLIED"}},
             {"apply": {"user": "a", "as": "r1", "group": True}}]

    result = make_run(cluster, api).run_case({"id": "C12", "steps": steps}, allow_faults=False)

    assert result["result"] == "PASS"
    assert api.bodies[api.calls.index(("POST", "/api/requests", 7))]["groupIds"] == [3]


def test_change_request_for_a_group_that_is_still_pending_is_reported_as_such():
    cluster = _fulfilled_cluster()
    cluster.on(r"FROM `groups` WHERE group_name", [("3", "")])
    steps = [{"user": "a"}, {"apply": {"user": "a", "as": "r1"}}, {"request_group": {"req": "r1", "as": "c1"}}]
    result = make_run(cluster, FakeApi()).run_case({"id": "C12", "steps": steps}, allow_faults=False)
    assert result["result"] == "FAIL" and "승인 대기" in result["error"]


def _share_context(*, shared="SHARED\n", visitor="", back=""):
    """주인 a(신청 r1)와 방문자 b(신청 r2). 컨테이너 안 명령의 출력만 정한다."""
    cluster = FakeCluster()
    cluster.on(r"WHERE request_id=101;", [("FULFILLED", "farm1", "pod-a")])
    cluster.on(r"WHERE request_id=102;", [("FULFILLED", "farm2", "pod-b")])
    cluster.on(r"group-dir-share", shared)
    cluster.on(r"echo READ", visitor)
    cluster.on(r"echo BACK", back)
    ctx = Context(make_run(cluster, FakeApi()), "C14")
    ctx.users = {"a": {"id": 7, "name": "exp-fu-a"}, "b": {"id": 8, "name": "exp-fu-b"}}
    ctx.requests = {"r1": 101, "r2": 102}
    ctx.memo = {"r1.owner": 7, "r2.owner": 8}
    return ctx, cluster


def test_owner_shares_a_home_folder_as_the_account_not_as_root():
    ctx, cluster = _share_context()
    ctx.do_share_dir("r1")
    script = next(c for c in cluster.calls if "group-dir-share" in c)
    assert "exec -i pod-a -- su -l exp-fu-a" in script
    assert "group-dir-share ~/e2e-share exp-fu-e2e-team" in script


def test_share_step_fails_when_the_share_command_never_succeeds():
    ctx, _ = _share_context(shared="")
    with pytest.raises(StepFailed, match="공유하지 못함"):
        ctx.do_share_dir("r1")


def test_teammate_reads_writes_and_the_owner_reads_the_teammates_file():
    ctx, cluster = _share_context(visitor="READ\nWRITE\n", back="BACK\n")
    ctx.do_expect_share({"of": "r1", "r2": "open"})
    visit = next(c for c in cluster.calls if "echo READ" in c)
    assert "exec -i pod-b -- su -l exp-fu-b" in visit and "/home/exp-fu-a/e2e-share/owner.txt" in visit


def test_open_share_fails_when_the_owner_cannot_read_what_the_teammate_wrote():
    """폴더 그룹만 바꾸고 setgid 를 빼면 팀원이 만든 파일이 팀원 개인 그룹으로 생겨 주인이 읽지 못한다."""
    ctx, _ = _share_context(visitor="READ\nWRITE\n", back="")
    with pytest.raises(StepFailed, match="owner_reads_back"):
        ctx.do_expect_share({"of": "r1", "r2": "open"})


def test_open_share_fails_when_the_teammate_cannot_read_the_owners_file():
    ctx, _ = _share_context(visitor="WRITE\n", back="BACK\n")
    with pytest.raises(StepFailed, match="open"):
        ctx.do_expect_share({"of": "r1", "r2": "open"})


def test_closed_share_passes_only_when_reading_and_writing_are_both_refused():
    ctx, _ = _share_context(visitor="")
    ctx.do_expect_share({"of": "r1", "r2": "closed"})
    ctx, _ = _share_context(visitor="READ\n")
    with pytest.raises(StepFailed, match="closed"):
        ctx.do_expect_share({"of": "r1", "r2": "closed"})


def test_a_listable_home_fails_the_share_check_even_for_a_teammate():
    """홈은 지나가기만 되고 목록은 보이면 안 된다."""
    ctx, _ = _share_context(visitor="READ\nWRITE\nLIST\n", back="BACK\n")
    with pytest.raises(StepFailed, match="list_home"):
        ctx.do_expect_share({"of": "r1", "r2": "open"})


def test_resetter_refuses_operation_stack():
    with pytest.raises(ValueError):
        Resetter(FakeCluster(stack="operation"), FakeApi(), "", "abc12")


def test_resetter_on_operation_needs_explicit_opt_in_and_stays_within_run_prefix():
    resetter = Resetter(FakeCluster(stack="operation"), FakeApi(), "", "abc12", allow_operation=True)
    assert resetter.prefix == "e2eabc12"
    with pytest.raises(ValueError):
        resetter._delete_homes(["yoon6yo"])


def test_resetter_never_deletes_homes_outside_the_run_prefix():
    resetter = Resetter(FakeCluster(), FakeApi(), "exp-fu-", "abc12")
    with pytest.raises(ValueError):
        resetter._delete_homes(["exp-fu-yoon6yo"])


def test_resetter_reclaims_accounts_through_the_product_and_reports_residue():
    cluster, api = FakeCluster(), FakeApi()
    state = {"reset": False}
    cluster.on(r"SELECT user_id, IFNULL\(ubuntu_username", lambda _: [] if state["reset"] else
               [("7", "exp-fu-e2eabc12c01a", "ACTIVE"), ("1", "", "NONE")])
    cluster.on(r"START TRANSACTION", lambda _: state.update(reset=True))
    cluster.on(r"FROM nodeport_allocations", [("exp-fu-e2eabc12c01a",)])
    cluster.on(r"/kube_share/passwd", "홈 exp-fu-e2eabc12c01a\n")
    residue = Resetter(cluster, api, "exp-fu-", "abc12", wait_timeout=0, interval=0).reset(admin_id=1)
    assert ("DELETE", "/api/admin/users/7/ubuntu-account", 1) in api.calls
    assert any("delete_user_home_directory" in c and "exp-fu-e2eabc12c01a" in c for c in cluster.calls)
    assert residue == ["포트 배정 exp-fu-e2eabc12c01a", "홈 exp-fu-e2eabc12c01a"]


def test_resetter_revokes_directly_when_job_finished_but_request_stuck_in_processing():
    """DEGRADED로 끝난 생성 작업은 신청을 PROCESSING에 남긴다. 제품에 되돌릴 경로가 없으므로 회수 작업을 직접 건다."""
    cluster, api = FakeCluster(), FakeApi()
    state = {"revoked": False}
    cluster.on(r"r\.status IN", lambda _: [] if state["revoked"] else [("56", "PROCESSING", "7")])
    cluster.on(r"action='PROVISION'", [("FAIL", "farm8", "exp-fu-e2eabc12c04a")])
    cluster.on(r"operations/revoke", lambda _: state.update(revoked=True))
    Resetter(cluster, api, "exp-fu-", "abc12", wait_timeout=0, interval=0).reset(admin_id=1)
    revoke = next(c for c in cluster.calls if "operations/revoke" in c)
    assert "'exp-fu-e2eabc12c04a'" in revoke and "'farm8'" in revoke and "'delete_account': True" in revoke


def test_resetter_deletes_password_reset_rows_before_users_only_where_the_table_exists():
    """재설정 신청은 users를 가리켜, 먼저 지우지 않으면 사용자 행이 지워지지 않는다. 표가 없는 옛 스택에서는 건드리지 않는다."""
    for table_exists in (True, False):
        cluster = FakeCluster()
        cluster.on(r"information_schema\.tables", [("1",)] if table_exists else [])
        Resetter(cluster, FakeApi(), "exp-fu-", "abc12", wait_timeout=0, interval=0).reset(admin_id=1)
        deletes = next(c for c in cluster.calls if "START TRANSACTION" in c)
        if table_exists:
            assert deletes.index("DELETE FROM password_reset_requests") < deletes.index("DELETE FROM users")
            # 상태·검토자는 변경 요청(종류 PASSWORD, 대상 신청 없음)에 있다. 재설정 행이 그 요청을 가리키므로 뒤에 지운다.
            password_changes = "DELETE FROM change_request WHERE request_id IS NULL"
            assert deletes.index("DELETE FROM password_reset_requests") < deletes.index(password_changes)
            assert deletes.index(password_changes) < deletes.index("DELETE FROM users")
        else:
            assert "password_reset_requests" not in deletes
