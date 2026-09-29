"""가상 계층의 실제 대상 시스템에 trial_runner 를 붙여서 trial 한 번이 끝까지 도는지 본다.

표는 손으로 베껴 쓰지 않고 infra-sql 의 DDL 파일에서 읽어 온다. 대상 시스템이 쓰는
operation_log 도 같은 정의로 다시 세워서, 저널 스키마가 시험과 운영 두 곳으로 갈라지지
않게 한다. 두 표가 한 연결 위에 있어야 manifest 의 시간창으로 저널을 회수할 수 있다.

가상 계층에는 실제로 흐르는 600초가 없으므로 시간은 걸음 수로 만든다. advance 한 번이
제어기를 한 바퀴 돌리는 일이면서 동시에 가짜 시계를 정해진 초만큼 미는 일이다.

어긋남은 평가 결과를 손으로 바꿔 넣어서 만들지 않는다. 대상 시스템 몰래 Pod 를 없애서
실제로 접근할 수 없는 상태를 만들고, 수집기가 그 상태를 읽게 둔다.
"""
import functools
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluator  # noqa: E402
import fault_injector  # noqa: E402
import scenario_spec  # noqa: E402
import test_trial  # noqa: E402
import trial  # noqa: E402
import trial_results  # noqa: E402
import trial_runner  # noqa: E402
from test_evaluator_fixtures import USER, _provision  # noqa: E402
from virtual_probe import virtual_collector  # noqa: E402

e2e = sys.modules["cs_e2e"]

HERE = Path(__file__).resolve().parent
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

    def __init__(self, e, *, kind, request_id, body, sabotage_after=None, gpu_usable=True):
        self.e = e
        self.kind = kind
        self.request_id = request_id
        self.body = body
        self.sabotage_after = sabotage_after
        self.now = 1000.0
        self.steps = 0
        self.collect = virtual_collector(e, gpu_usable=gpu_usable)
        self.saved = []

    def save(self, record):
        self.saved.append(record)

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


def _create_ports(e, request_id, sabotage_after=None, gpu_usable=True):
    return VirtualPorts(e, kind="provision", request_id=request_id,
                        body={"request_id": request_id, "username": USER,
                              "account": {"passwd_base64": e2e.PW}},
                        sabotage_after=sabotage_after, gpu_usable=gpu_usable)


def _revoke_ports(e, request_id, pod_name):
    return VirtualPorts(e, kind="revoke", request_id=request_id,
                        body={"request_id": request_id, "username": USER,
                              "pod_name": pod_name, "delete_account": True})


def _run(conn, ports, *, trial_id, operation, save=None, injector=None, method="full"):
    return trial_runner.run_trial(
        conn, trial_id=trial_id, method=method, server_group="A", operation=operation,
        horizon_sec=HORIZON_SEC, repetition=1, revisions={"config-server": "virtual"},
        username=USER, submit=ports.submit, advance=ports.advance,
        declaration=ports.declaration, collect=ports.collect, clock=ports.clock,
        save=save or ports.save, environment=lambda: (trial_runner.CLEAN, {}), injector=injector)


# 흐름 시험은 가상 GPU 가 쓸 수 있다고 정한 세계(VirtualPorts 의 gpu_usable=True)에서 돈다.
# GPU 가 없는 세계의 판정(UNKNOWN)은 test_gpu_less_world_stays_unknown 이 따로 본다.


def _only_gpu_unknown(verdict):
    results = {name: d["result"] for name, d in verdict["domains"].items()}
    return (verdict["verdict"] == evaluator.UNKNOWN
            and results.pop("compute_gpu") == evaluator.UNKNOWN
            and set(results.values()) == {evaluator.PASS})


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


def test_gpu_less_world_stays_unknown(conn, env):
    """GPU 가 없는 세계: GPU 를 뺀 검사가 전부 PASS 여도 판정은 UNKNOWN 이고 검증 시각이 서지 않는다."""
    out = _run(conn, _create_ports(env, "811", gpu_usable=None), trial_id="trial-nogpu", operation="CREATE")

    assert out["timestamps"]["verified"] is None
    assert _only_gpu_unknown(out["independent_verdict"]["at_declaration"])


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


def test_collapse_after_declaration_does_not_erase_the_earlier_observation(conn, env):
    """선언 뒤 붕괴: 선언이 잡힌 뒤에 Pod 를 없애면 H 끝 판정만 FAIL 이 되고 앞선 관측은 그대로 남는다."""
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


def test_saved_record_round_trips_through_trial_results(conn, env, tmp_path):
    """생산자(run_trial)와 소비자(trial_results)의 계약을 한곳에서 교차 확인한다."""
    out = _run(conn, _create_ports(env, "809"), trial_id="trial-saved", operation="CREATE",
               save=functools.partial(trial_results.save, tmp_path, secrets=()))

    loaded = trial_results.load(tmp_path, "trial-saved")
    assert loaded.pop("schema_version") == trial_results.SCHEMA_VERSION
    assert loaded == json.loads(json.dumps(out))


def test_pair_runs_revoke_right_after_creation_on_the_same_request(conn, env):
    """R2: 정상 생성에 이어 같은 신청으로 회수 trial 을 돌리면 회수 기록이 생성 판정을 물려받는다."""
    create = _create_ports(env, "810")
    revoke = None

    def revoke_submit(request_id):
        # 회수 신청에는 Pod 이름이 필요하므로 생성 trial 이 끝난 뒤에 가상 계층에서 찾는다.
        nonlocal revoke
        revoke = _revoke_ports(env, request_id, next(iter(env.v1.pods)))
        return revoke.submit()

    created, revoked = trial_runner.run_pair(
        conn, create_trial_id="trial-pair-c", revoke_trial_id="trial-pair-r", method="full",
        server_group="A", horizon_sec=HORIZON_SEC, repetition=1,
        revisions={"config-server": "virtual"}, username=USER,
        create_submit=create.submit, create_declaration=create.declaration,
        revoke_submit=revoke_submit,
        revoke_declaration=lambda: revoke.declaration(),
        advance=create.advance, collect=create.collect, clock=create.clock,
        environment=lambda: (trial_runner.CLEAN, {}), save=create.save)

    assert revoked["request_id"] == created["request_id"] == "810"
    assert revoked["start_state"] == {"creation_trial_id": "trial-pair-c",
                                      "creation_verdict_at_horizon": evaluator.PASS}
    assert revoked["independent_verdict"]["at_declaration"]["verdict"] == evaluator.PASS
    assert revoked["independent_verdict"]["at_horizon"]["verdict"] == evaluator.PASS
    assert "REVOKE" in {e["action"] for e in trial.events_of(conn, "trial-pair-r")}


@pytest.fixture
def armed(conn, env, monkeypatch):
    """장전 표를 DDL 정의로 같은 연결 위에 세우고, 대상 시스템의 주입 훅이 그 연결을 읽게 한다."""
    env.db.execute(test_trial._sqlite_ddl("operation_log.sql", "fault_arming"))
    env.db.commit()
    from adapters import fault_injection
    from test_fault_injection import Sql as ArmingSql  # 훅이 쓰는 rowcount 와 close 까지 흉내 낸다
    monkeypatch.setattr(fault_injection, "get_log_db_connection", lambda **kw: ArmingSql(env.db))
    monkeypatch.setattr(fault_injection, "_seen", {})  # 발동 횟수는 행 id 로 세므로 시험마다 비운다
    monkeypatch.setenv("FAULT_INJECTION", "1")
    return conn


def _injector(conn, scenario_id):
    view = scenario_spec.load(HERE / "scenarios" / f"{scenario_id}.yaml").run_view()
    return fault_injector.from_spec(view, username=USER, journal=conn)


METHODS = ("baseline", "noprobe", "full")


def _method(env, monkeypatch, method):
    """대상 시스템을 그 비교군으로 돌린다. full 은 가상 계층의 접근 시험 대역까지 건다."""
    if method == "full":
        getattr(e2e.full, "__wrapped__", e2e.full)(env, monkeypatch)
    else:
        monkeypatch.setattr(sys.modules["main"], "VERIFY_MODE", method)


def _keytab_secret_too(env, monkeypatch):
    """principal 생성 대역이 keytab Secret 도 남겨야 관찰기가 효과를 확인할 수 있다."""
    def create(name, uid, gid):
        env.calls.append(("krb5_principal", (name,)))
        env.v1.secrets[f"krb5-keytab-{name}"] = {"data": {}, "owners": []}
    monkeypatch.setattr(sys.modules["main"], "_create_krb5_principal_and_secret", create)


@pytest.mark.parametrize("method", METHODS)
def test_a3_injection_is_verified_in_every_method(armed, env, monkeypatch, method):
    """A3-KRB5(C06): 효과가 적용된 뒤 응답을 잃었다는 사실은 비교군의 반응과 무관하게 성립한다."""
    _method(env, monkeypatch, method)
    _keytab_secret_too(env, monkeypatch)

    out = _run(armed, _create_ports(env, "820"), trial_id=f"trial-a3-{method}", operation="CREATE",
               injector=_injector(armed, "A3-KRB5"), method=method)

    assert out["injection"]["boundary"] == "X4" and out["injection"]["fired_at"] is not None
    assert out["injection"]["verified"] is True, out["injection"]
    assert out["injection"]["invalid_reason"] is None
    assert env.db.execute("SELECT COUNT(*) FROM fault_arming").fetchone()[0] == 0


class Killed(BaseException):
    """os._exit 대역. except Exception 에 잡히지 않아야 실제 kill 처럼 제어기를 빠져나간다."""


@pytest.mark.parametrize("method", METHODS)
def test_b4_injection_is_verified_in_every_method(armed, env, lease, monkeypatch, method):
    """B4-KRB5(C08): 저널에 기록하기 전에 끊었다는 사실은 재시작 뒤에 이어 하는지와 무관하게 성립한다."""
    main = sys.modules["main"]
    from adapters import fault_injection, operation_log
    from test_fault_hooks import _NullConn
    _method(env, monkeypatch, method)
    # X6 훅은 실제 log_operation 안에 있다. 그것을 먼저 부른 뒤 가상 계층의 기록기로 행을 남긴다.
    fake = main.log_operation
    monkeypatch.setattr(operation_log, "get_log_db_connection", lambda **kw: _NullConn())
    monkeypatch.setattr(main, "log_operation", lambda **kw: (operation_log.log_operation(**kw), fake(**kw))[1])

    def kill():
        raise Killed()
    monkeypatch.setattr(fault_injection, "kill_process", kill)

    ports = _create_ports(env, "822")
    step = ports.advance

    def advance():
        # 제어기가 죽으면 그 걸음을 끝내고, 죽은 제어기의 lease 를 만료시켜 다음 걸음이 재시작이 되게 한다.
        try:
            step()
        except Killed:
            for row in lease.values():
                row.update(owner="dead-controller", alive=False)
            ports.now += STEP_SEC
    ports.advance = advance

    out = _run(armed, ports, trial_id=f"trial-b4-{method}", operation="CREATE",
               injector=_injector(armed, "B4-KRB5"), method=method)

    assert out["injection"]["boundary"] == "X6" and out["injection"]["fired_at"] is not None
    assert out["injection"]["verified"] is True, out["injection"]
    assert out["injection"]["invalid_reason"] is None


def test_c3_pair_records_the_injection_on_the_revoke_trial(armed, env):
    """C3-KRB5(C12): 짝 실행에서 장애는 회수 trial 에만 걸리고, 회수 기록에 확인된 주입이 남는다."""
    create = _create_ports(env, "821")
    revoke = None

    def revoke_submit(request_id):
        nonlocal revoke
        revoke = _revoke_ports(env, request_id, next(iter(env.v1.pods)))
        return revoke.submit()

    created, revoked = trial_runner.run_pair(
        armed, create_trial_id="trial-c12-c", revoke_trial_id="trial-c12-r", method="full",
        server_group="A", horizon_sec=HORIZON_SEC, repetition=1,
        revisions={"config-server": "virtual"}, username=USER,
        revoke_scenario_id="C3-KRB5",
        revoke_injector=_injector(armed, "C3-KRB5"),
        create_submit=create.submit, create_declaration=create.declaration,
        revoke_submit=revoke_submit, revoke_declaration=lambda: revoke.declaration(),
        advance=create.advance, collect=create.collect, clock=create.clock,
        environment=lambda: (trial_runner.CLEAN, {}), save=create.save)

    assert created["injection"] is None
    assert created["system_declaration"]["value"] == "SUCCESS"
    assert revoked["scenario_id"] == "C3-KRB5"
    assert revoked["injection"]["fired_at"] is not None
    assert revoked["injection"]["verified"] is True, revoked["injection"]
    assert env.db.execute("SELECT COUNT(*) FROM fault_arming").fetchone()[0] == 0
