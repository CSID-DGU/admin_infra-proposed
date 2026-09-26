"""E2E 실행기·정리기의 판정 규칙을 가짜 포트로 확인한다(클러스터 없음)."""
import re

import pytest

from e2e.resetter import Resetter
from e2e.runner import Run


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
        self.responses = responses or {}

    def call(self, method, path, *, as_user, body=None):
        self.calls.append((method, path, as_user))
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
