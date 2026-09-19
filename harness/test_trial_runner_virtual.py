"""가상 계층의 실제 대상 시스템에 trial_runner 를 붙여서 trial 한 번이 끝까지 도는지 본다.

표는 손으로 베껴 쓰지 않고 infra-sql 의 DDL 파일에서 읽어 온다. 대상 시스템이 쓰는
operation_log 도 같은 정의로 다시 세워서, 저널 스키마가 시험과 운영 두 곳으로 갈라지지
않게 한다. 두 표가 한 연결 위에 있어야 manifest 의 시간창으로 저널을 회수할 수 있다.

가상 계층에는 실제로 흐르는 600초가 없으므로 시간은 걸음 수로 만든다. advance 한 번이
제어기를 한 바퀴 돌리는 일이면서 동시에 가짜 시계를 정해진 초만큼 미는 일이다.

어긋남은 평가 결과를 손으로 바꿔 넣어서 만들지 않는다. 대상 시스템 몰래 Pod 를 없애서
실제로 접근할 수 없는 상태를 만들고, 수집기가 그 상태를 읽게 둔다.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluator  # noqa: E402
import test_trial  # noqa: E402
import trial  # noqa: E402
import trial_runner  # noqa: E402
from test_evaluator_fixtures import USER, _provision  # noqa: E402
from virtual_probe import virtual_collector  # noqa: E402

e2e = sys.modules["cs_e2e"]

HORIZON_SEC = 600.0
STEP_SEC = 200.0  # 한 걸음이 미는 초. 관측 구간이 세 걸음 만에 끝난다.


@pytest.fixture
def conn(env):
    """가상 계층이 쓰는 그 연결 위에 두 표를 DDL 정의로 세운다.

    가상 계층은 operation_log 를 자기 손으로 만들어 두지만, 그 정의를 그대로 쓰면 저널
    스키마가 운영 DDL 과 갈라져도 시험이 통과한다. 그래서 같은 표를 DDL 정의로 다시 세운다.
    """
    env.db.execute("DROP TABLE operation_log")
    env.db.execute(test_trial._sqlite_ddl("operation_log.sql", "operation_log"))
    env.db.execute(test_trial._sqlite_ddl("trial_manifest.sql", "trial_manifest"))
    env.db.commit()
    return test_trial.Sql(env.db)


class VirtualPorts:
    """포트 다섯 개를 가상 계층에 잇는다.

    sabotage_after 는 몇 번째 걸음 끝에서 Pod 를 없앨지 정한다. 1 이면 선언이 잡히기
    직전이고 2 면 선언이 잡힌 뒤다. 이 숫자 하나가 잘못된 완료 선언과 선언 뒤 붕괴를 가른다.
    """

    def __init__(self, e, *, kind, request_id, body, sabotage_after=None):
        self.e = e
        self.kind = kind
        self.request_id = request_id
        self.body = body
        self.sabotage_after = sabotage_after
        self.now = 1000.0
        self.steps = 0
        self.collect = virtual_collector(e)

    def clock(self):
        return self.now

    def submit(self):
        r = self.e.api.post(f"/operations/{self.kind}", json=self.body)
        assert r.status_code == 202, r.get_json()
        return self.request_id

    def advance(self):
        e2e.tick(self.e)
        self.steps += 1
        if self.steps == self.sabotage_after:
            self.e.v1.pods.clear()  # 대상 시스템 몰래 자원을 없앤다
        self.now += STEP_SEC

    def declaration(self):
        phase = e2e.result(self.e, self.kind, self.request_id)["phase"]
        return phase if phase == "SUCCESS" else None


def _create_ports(e, request_id, sabotage_after=None):
    return VirtualPorts(e, kind="provision", request_id=request_id,
                        body={"request_id": request_id, "username": USER,
                              "account": {"passwd_base64": e2e.PW}},
                        sabotage_after=sabotage_after)


def _revoke_ports(e, request_id, pod_name):
    return VirtualPorts(e, kind="revoke", request_id=request_id,
                        body={"request_id": request_id, "username": USER,
                              "pod_name": pod_name, "delete_account": True})


def _run(conn, ports, *, trial_id, operation):
    return trial_runner.run_trial(
        conn, trial_id=trial_id, method="full", server_group="A", operation=operation,
        horizon_sec=HORIZON_SEC, repetition=1, revisions={"config-server": "virtual"},
        username=USER, submit=ports.submit, advance=ports.advance,
        declaration=ports.declaration, collect=ports.collect, clock=ports.clock)


def test_normal_creation_trial_verifies_at_the_declaration(conn, env):
    """정상 생성: 네 시각이 모두 채워지고 두 판정이 모두 PASS 다."""
    out = _run(conn, _create_ports(env, "801"), trial_id="trial-normal", operation="CREATE")

    ts = out["timestamps"]
    assert ts["submitted"] is not None and ts["horizon_end"] is not None
    assert ts["declared"] is not None and ts["verified"] is not None
    assert out["independent_verdict"]["at_declaration"]["verdict"] == evaluator.PASS
    assert out["independent_verdict"]["at_horizon"]["verdict"] == evaluator.PASS
    # 선언 시점에 이미 접근이 확인되었으므로 검증 시각이 H 끝까지 미뤄지지 않는다.
    assert ts["verified"] == ts["declared"]


def test_wrong_declaration_keeps_both_axes_as_observed(conn, env):
    """잘못된 완료 선언: 선언이 잡히기 직전에 Pod 를 없애면 선언은 성공인데 판정은 FAIL 이다."""
    out = _run(conn, _create_ports(env, "802", sabotage_after=1),
               trial_id="trial-wrong", operation="CREATE")

    assert out["system_declaration"]["value"] == "SUCCESS"
    assert out["independent_verdict"]["at_declaration"]["verdict"] == evaluator.FAIL
    assert out["timestamps"]["verified"] is None
    # 같은 시점에 대상 시스템은 여전히 성공을 선언해 두었다. 어느 칸도 다른 칸을 덮어쓰지 않는다.
    assert e2e.result(env, "provision", "802")["phase"] == "SUCCESS"
    assert out["system_declaration"]["at"] == out["timestamps"]["declared"]


def test_collapse_after_declaration_does_not_erase_the_verification(conn, env):
    """선언 뒤 붕괴: 선언이 잡힌 뒤에 Pod 를 없애면 앞서 성립한 확인이 그대로 남는다."""
    out = _run(conn, _create_ports(env, "803", sabotage_after=2),
               trial_id="trial-collapse", operation="CREATE")

    assert out["independent_verdict"]["at_declaration"]["verdict"] == evaluator.PASS
    assert out["independent_verdict"]["at_horizon"]["verdict"] == evaluator.FAIL
    assert out["timestamps"]["verified"] == out["timestamps"]["declared"]


def test_revoke_trial_binds_manifest_and_journal_under_one_trial_id(conn, env):
    """회수 trial: 두 판정이 PASS 이고, 같은 trial_id 로 저널 행이 회수된다."""
    pod_name = _provision(env, "804")
    out = _run(conn, _revoke_ports(env, "804", pod_name),
               trial_id="trial-revoke", operation="REVOKE")

    assert out["independent_verdict"]["at_declaration"]["verdict"] == evaluator.PASS
    assert out["independent_verdict"]["at_horizon"]["verdict"] == evaluator.PASS

    # manifest 와 저널과 독립 판정이 하나의 trial_id 아래 묶였다는 뜻이다. sqlite 의 시각은
    # 1초 단위여서 준비 단계의 생성 행이 같은 창에 들 수 있으므로, 회수 행이 들어 있는지를 본다.
    events = trial.events_of(conn, "trial-revoke")
    assert events
    assert "REVOKE" in {e["action"] for e in events}
