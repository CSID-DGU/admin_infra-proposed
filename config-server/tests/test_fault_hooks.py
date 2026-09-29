"""장애 주입 훅(X4 response_loss=C06, X6 sigkill_before_journal=C08, X2 fail_persistent=C12)을 가상 계층에서 실제 단계 함수와 재시도 엔진으로 돌려 본다.
장전 표는 가상 E2E 의 sqlite 에 함께 세우고, fault_injection 이 그 연결을 쓰게 한다."""
import pytest

import main
from adapters import fault_injection, operation_log
from test_e2e_virtual import env, PW, tick, rows, result, passwd_names  # noqa: F401
from test_fault_injection import Sql as ArmingSql, fault_arming_ddl


class Killed(BaseException):
    """os._exit 대역. except Exception 에 잡히지 않아야 실제 kill 처럼 제어기를 빠져나간다."""


class _NullConn:
    """실제 log_operation 을 돌리되 쓰기는 가상 E2E 의 가짜 기록기에 맡긴다."""
    lastrowid = None

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def execute(self, *a):
        pass

    def fetchone(self):
        return None

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def faults(env, lease_env, monkeypatch):
    e = env
    e.db.execute(fault_arming_ddl())
    e.arming_connects = 0
    monkeypatch.setattr(fault_injection, "_seen", {})

    def connect(**kw):
        e.arming_connects += 1
        return ArmingSql(e.db)
    monkeypatch.setattr(fault_injection, "get_log_db_connection", connect)
    monkeypatch.setenv("FAULT_INJECTION", "1")
    # C08 훅은 실제 log_operation 안에 있다. 그것을 먼저 부른 뒤 가짜 기록기로 행을 남긴다.
    fake = main.log_operation
    monkeypatch.setattr(operation_log, "get_log_db_connection", lambda **kw: _NullConn())
    monkeypatch.setattr(main, "log_operation", lambda **kw: (operation_log.log_operation(**kw), fake(**kw))[1])
    return e


# 옛 시나리오 ID -> (boundary, action)
POINTS = {"C06": ("X4", "response_loss"), "C08": ("X6", "sigkill_before_journal"), "C12": ("X2", "fail_persistent")}


def arm(e, username, step, scenario):
    e.db.execute("INSERT INTO fault_arming (username, boundary, step_name, action) VALUES (?,?,?,?)",
                 (username, POINTS[scenario][0], step, POINTS[scenario][1]))
    e.db.commit()


def fired_at(e, scenario):
    return e.db.execute("SELECT fired_at FROM fault_arming WHERE boundary=? AND action=?",
                        POINTS[scenario]).fetchone()[0]


def provision(e, rid, username):
    r = e.api.post("/operations/provision", json={"request_id": rid, "username": username,
                                                  "account": {"passwd_base64": PW}})
    assert r.status_code == 202
    return r.get_json()["job_id"]


def keytab_secret_too(e, monkeypatch):
    """principal 생성 대역이 keytab Secret 도 남기게 해서 관찰기가 효과를 확인할 수 있게 한다."""
    def create(name, uid, gid):
        e.calls.append(("krb5_principal", (name,)))
        e.v1.secrets[f"krb5-keytab-{name}"] = {"data": {}, "owners": []}
    monkeypatch.setattr(main, "_create_krb5_principal_and_secret", create)


def test_switch_off_ignores_armed_rows(faults, monkeypatch):
    e = faults
    monkeypatch.delenv("FAULT_INJECTION")
    arm(e, "exp-np-f0", "step_create_account", "C12")
    arm(e, "exp-np-f0", "step_create_home", "C06")
    arm(e, "exp-np-f0", "step_create_krb5_principal", "C08")
    provision(e, "1000", "exp-np-f0")
    tick(e)
    assert result(e, "provision", "1000")["phase"] == "SUCCESS", rows(e, "1000")
    assert e.arming_connects == 0
    assert e.db.execute("SELECT COUNT(*) FROM fault_arming WHERE fired_at IS NOT NULL").fetchone()[0] == 0


def test_c06_effect_applied_response_lost_is_observed_as_success(faults, monkeypatch):
    e = faults
    keytab_secret_too(e, monkeypatch)
    arm(e, "exp-np-f6", "step_create_krb5_principal", "C06")
    provision(e, "1006", "exp-np-f6")
    tick(e)
    assert result(e, "provision", "1006")["phase"] == "SUCCESS", rows(e, "1006")
    assert [c[0] for c in e.calls].count("krb5_principal") == 1     # 관찰기가 확인해서 다시 실행하지 않았다
    assert fired_at(e, "C06") is not None


def test_c06_in_baseline_fails_the_job(faults, monkeypatch):
    e = faults
    monkeypatch.setattr(main, "VERIFY_MODE", "baseline")
    keytab_secret_too(e, monkeypatch)
    arm(e, "exp-np-f6b", "step_create_krb5_principal", "C06")
    provision(e, "1016", "exp-np-f6b")
    tick(e)
    assert result(e, "provision", "1016")["phase"] == "FAIL", rows(e, "1016")
    assert fired_at(e, "C06") is not None


def test_c08_kills_before_success_row_and_resumes_without_dying_again(faults, lease_env, monkeypatch):
    e = faults

    def kill():
        # 이 시점에 발동 표시가 이미 커밋되어 있어야 한다
        e.fired_before_kill = fired_at(e, "C08") is not None
        raise Killed()
    monkeypatch.setattr(fault_injection, "kill_process", kill)
    arm(e, "exp-np-f8", "step_create_krb5_principal", "C08")
    jid = provision(e, "1008", "exp-np-f8")

    with pytest.raises(Killed):
        tick(e)
    assert e.fired_before_kill
    assert ("CREATE_KRB5_PRINCIPAL", "START") in rows(e, "1008")
    assert ("CREATE_KRB5_PRINCIPAL", "SUCCESS") not in rows(e, "1008")

    lease_env[jid].update(owner="dead-controller", alive=False)    # 죽은 제어기의 lease 가 만료됐다
    tick(e)
    assert result(e, "provision", "1008")["phase"] == "SUCCESS", rows(e, "1008")
    assert "exp-np-f8" in passwd_names() and len(e.v1.pods) == 1


def test_c12_keeps_principal_after_pod_and_account_are_gone(faults):
    e = faults
    provision(e, "1012", "exp-np-f12")
    tick(e)
    assert result(e, "provision", "1012")["phase"] == "SUCCESS", rows(e, "1012")
    pod_name = next(iter(e.v1.pods))

    arm(e, "exp-np-f12", "step_remove_krb5", "C12")
    assert e.api.post("/operations/revoke", json={"request_id": "1012", "pod_name": pod_name,
                                                  "delete_account": True}).status_code == 202
    tick(e)

    assert e.v1.pods == {} and "exp-np-f12" not in passwd_names()
    assert "krb5_principal_delete" not in [c[0] for c in e.calls]
    assert fired_at(e, "C12") is not None
    res = result(e, "revoke", "1012")
    assert res["phase"] == "FAIL", rows(e, "1012")
    detail = e.db.execute("SELECT error_detail FROM operation_log WHERE request_id='1012'"
                          " AND action='REVOKE' AND phase='FAIL'").fetchone()[0]
    assert "RETRIES_EXHAUSTED" in detail and "fault X2 fail_persistent" in detail
