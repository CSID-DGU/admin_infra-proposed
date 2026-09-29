"""fault_injector v2 의 계약. 장전 표와 저널은 infra-sql 의 DDL 정의로 sqlite 에 세운다."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fault_injector  # noqa: E402
import scenario_spec  # noqa: E402
import system  # noqa: E402
import test_trial  # noqa: E402

HERE = Path(__file__).resolve().parent
USER = "exp-fu-m1"


@pytest.fixture
def db():
    db = sqlite3.connect(":memory:")
    db.execute(test_trial._sqlite_ddl("operation_log.sql", "fault_arming"))
    db.execute(test_trial._sqlite_ddl("operation_log.sql", "operation_log"))
    return db


def _view(scenario_id):
    return scenario_spec.load(HERE / "scenarios" / f"{scenario_id}.yaml").run_view()


def _hook(db, scenario_id="C3-KRB5", username=USER):
    return fault_injector.from_spec(_view(scenario_id), username=username, journal=test_trial.Sql(db))


def _arming(db):
    return db.execute("SELECT username, step_name, boundary, action, occurrence, fired_at FROM fault_arming").fetchall()


def _journal(db, phase, *, action="X", resource_type=None, at="2026-09-29 01:00:01", username=USER):
    db.execute("INSERT INTO operation_log (request_id, username, resource_type, action, phase, created_at)"
               " VALUES ('1', ?, ?, ?, ?, ?)", (username, resource_type, action, phase, at))


def _fire(db, at="2026-09-29 01:00:00"):
    db.execute("UPDATE fault_arming SET fired_at = ?", (at,))


# 코드 훅

def test_from_spec_picks_the_injector_by_kind(db):
    assert isinstance(_hook(db, "A3-KRB5"), fault_injector.CodeHookInjector)
    assert _hook(db, "N1") is None
    ext = fault_injector.from_spec(_view("D6-ENDPOINT"), username=USER, journal=None, host="h",
                                   namespace="ailab-full", run_id="r1")
    assert isinstance(ext, fault_injector.ExternalMutationInjector)


def test_from_spec_does_not_read_analysis(db):
    class NoAnalysis(dict):
        def __getitem__(self, key):
            if key == "analysis":
                raise AssertionError("analysis 를 읽었다")
            return super().__getitem__(key)

    raw = {**_view("A3-KRB5"), "analysis": {"recoverable": True}}
    assert fault_injector.from_spec(NoAnalysis(raw), username=USER, journal=test_trial.Sql(db)) is not None


def test_code_hook_arm_inserts_the_v2_row_once(db):
    inj = _hook(db)
    inj.arm()
    inj.arm()
    assert _arming(db) == [(USER, "step_remove_krb5", "X2", "fail_persistent", 1, None)]


def test_code_hook_without_firing_is_invalid_and_disarm_is_idempotent(db):
    inj = _hook(db, "A3-KRB5")
    inj.arm()
    inj.poll()
    out = inj.report()
    assert out["kind"] == "code_hook" and out["step"] == "step_create_krb5_principal"
    assert out["armed_at"] is not None and out["fired_at"] is None
    assert out["applied"] is False and out["verified"] is False and out["invalid_reason"]
    inj.disarm()
    inj.disarm()
    assert _arming(db) == []


def test_success_row_until_firing_verifies_a3(db):
    """응답 유실: 효과가 적용된 뒤(SUCCESS 행) 발동했으면 성립이다. 같은 시각도 앞으로 친다."""
    inj = _hook(db, "A3-KRB5")
    inj.arm()
    _fire(db)
    _journal(db, "SUCCESS", action="CREATE_KRB5_PRINCIPAL", at="2026-09-29 01:00:01")  # 발동 뒤의 행은 세지 않는다
    _journal(db, "SUCCESS", action="CREATE_SERVICE", at="2026-09-29 00:59:59")  # 다른 단계의 행도 세지 않는다
    assert inj.report()["verified"] is False
    _journal(db, "SUCCESS", action="CREATE_KRB5_PRINCIPAL", at="2026-09-29 01:00:00")
    out = inj.report()
    assert out["applied"] and out["verified"] and out["invalid_reason"] is None


@pytest.mark.parametrize("scenario_id,action", [("B4-KRB5", "CREATE_KRB5_PRINCIPAL"),
                                                ("C3-KRB5", "REMOVE_KRB5"), ("A1-POD", "CREATE_POD_K8S")])
def test_no_success_row_before_firing_verifies_the_cut(db, scenario_id, action):
    """기록 전 종료와 효과 없는 실패: 발동 전에 그 단계의 SUCCESS 행이 없으면 성립이다.
    발동 뒤에 이어하기로 생긴 SUCCESS 행은 비교군마다 다르므로 보지 않는다."""
    inj = _hook(db, scenario_id)
    inj.arm()
    _fire(db)
    _journal(db, "SUCCESS", action=action, at="2026-09-29 01:00:00")  # 같은 시각은 발동 뒤로 친다
    _journal(db, "SUCCESS", action=action, at="2026-09-29 01:00:05")
    assert inj.report()["verified"] is True
    _journal(db, "SUCCESS", action=action, at="2026-09-29 00:59:59")
    out = inj.report()
    assert out["verified"] is False and out["invalid_reason"]


def test_step_journal_actions_exist_in_the_target_action_enum():
    from adapters.operation_log import Action
    assert set(fault_injector.STEP_JOURNAL_ACTIONS.values()) <= {a.value for a in Action}


def test_every_wave1_code_hook_step_has_a_journal_action():
    for sid, spec in scenario_spec.load_all(HERE / "scenarios").items():
        inj = spec.run_view()["injection"]
        if inj["kind"] != "none" and inj["step"] is not None:
            assert inj["step"] in fault_injector.STEP_JOURNAL_ACTIONS, sid


def test_other_users_rows_are_left_alone(db):
    other = _hook(db, username="exp-fu-m2")
    other.arm()
    inj = _hook(db)
    inj.arm()
    inj.disarm()
    assert [r[0] for r in _arming(db)] == ["exp-fu-m2"]


# 외부 변경

class FakeStack:
    def __init__(self, monkeypatch, *, apply_rc=0, listed=True):
        self.calls = []
        self.apply_rc, self.listed = apply_rc, listed
        monkeypatch.setattr(system, "stack_mutate", self.mutate)
        monkeypatch.setattr(system, "stack_kube", self.kube)

    def mutate(self, host, namespace, action, *, template=None, target_user=None, run):
        self.calls.append((action, template, target_user, run))
        return {"rc": self.apply_rc if action == "apply" else 0, "stdout": "", "stderr": "boom"}

    def kube(self, host, namespace, args):
        self.calls.append(("kube", *args))
        names = [f"networkpolicy.networking.k8s.io/vasc-{t}-{r}" for a, t, _, r in self.calls[:-1] if a == "apply"]
        return {"rc": 0, "stdout": "\n".join(names) if self.listed else "", "stderr": ""}


def _ext(scenario_id, db, clock=lambda: 5.0):
    view = _view(scenario_id)
    return fault_injector.ExternalMutationInjector("h", "ailab-full", injection=view["injection"], username=USER,
                                                   run_id="r1", journal=test_trial.Sql(db), clock=clock)


def test_ad_block_applies_at_arm_and_clears_at_disarm(db, monkeypatch):
    stack = FakeStack(monkeypatch)
    inj = _ext("F1-ADBLOCK", db)
    inj.arm()
    inj.arm()
    inj.poll()
    out = inj.report()
    inj.disarm()
    inj.disarm()
    assert [c for c in stack.calls if c[0] != "kube"] == [("apply", "deny-egress-ad", None, "r1"),
                                                            ("clear", None, None, "r1")]
    assert out["applied"] and out["verified"] and out["invalid_reason"] is None
    assert out["boundary"] == "X0" and out["fired_at"] == 5.0


def test_endpoint_block_applies_once_after_the_service_success_row(db, monkeypatch):
    stack = FakeStack(monkeypatch)
    inj = _ext("D6-ENDPOINT", db)
    inj.arm()
    inj.poll()
    assert stack.calls == []
    _journal(db, "SUCCESS", action="CREATE_SERVICE", resource_type="service", username="someone-else")
    _journal(db, "START", action="CREATE_SERVICE", resource_type="service")
    inj.poll()
    assert stack.calls == []
    _journal(db, "SUCCESS", action="CREATE_SERVICE", resource_type="service")
    inj.poll()
    inj.poll()
    assert stack.calls == [("apply", "deny-ingress-user", USER, "r1")]
    assert inj.report()["verified"] is True


def test_endpoint_block_that_never_reached_the_boundary_is_invalid(db, monkeypatch):
    FakeStack(monkeypatch)
    inj = _ext("D6-ENDPOINT", db)
    inj.arm()
    inj.poll()
    out = inj.report()
    assert out["applied"] is False and out["verified"] is False and out["invalid_reason"]


def test_a_failed_mutate_is_recorded_as_invalid(db, monkeypatch):
    FakeStack(monkeypatch, apply_rc=1)
    inj = _ext("F1-ADBLOCK", db)
    inj.arm()
    out = inj.report()
    assert out["applied"] is False and out["verified"] is False
    assert "boom" in out["invalid_reason"]


def test_a_policy_missing_from_the_stack_is_unverified(db, monkeypatch):
    FakeStack(monkeypatch, listed=False)
    inj = _ext("F1-ADBLOCK", db)
    inj.arm()
    out = inj.report()
    assert out["applied"] is True and out["verified"] is False and out["invalid_reason"]
