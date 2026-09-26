"""계정 회수에서 계정이 이미 없을 때 (#213).

계정 회수(`delete_account=true`)는 admin_be가 사용자가 쓴 노드마다 하나씩 등록한다(admin_be #600). 계정 파일은
스택에 하나라, 첫 노드의 계정 회수가 계정을 지우면 나머지 노드의 계정 회수는 "이미 없음"을 만난다. 목표 상태
(계정 없음)에 이미 도달한 것이므로 NoProbe·Full은 성공으로 보고 그 노드의 Kerberos 정리까지 진행한다.
이미 없었다는 사실은 DELETE_ACCOUNT SUCCESS 행의 error_detail(already_absent)로 구분해 남긴다.
baseline은 운영 시절처럼 USER_NOT_FOUND로 실패한다. 컨테이너 회수는 바뀌지 않는다.
"""
import json
import subprocess

import pytest

import main
from test_e2e_virtual import env, full, PW, tick, rows, result, passwd_names  # noqa: F401  (env·full은 fixture)

USER = "exp-np-rv1"


def _provision_and_revoke_container(e, rid):
    """생성 작업을 끝내고 그 컨테이너를 회수한다(계정은 남는다). 컨테이너가 떠 있던 노드를 돌려준다."""
    e.api.post("/operations/provision", json={"request_id": rid, "username": USER,
                                              "account": {"passwd_base64": PW}})
    tick(e)
    assert result(e, "provision", rid)["phase"] == "SUCCESS", rows(e, rid)
    pod = next(iter(e.v1.pods))
    node = e.v1.pods[pod].spec.node_name
    e.api.post("/operations/revoke", json={"request_id": rid, "pod_name": pod})
    tick(e)
    assert result(e, "revoke", rid)["phase"] == "SUCCESS", rows(e, rid)
    assert USER in passwd_names()                                    # 컨테이너 회수는 계정을 건드리지 않는다
    return node


def _revoke_account(e, rid, node):
    e.api.post("/operations/revoke", json={"request_id": rid, "username": USER, "node_name": node,
                                           "delete_account": True})
    tick(e)
    return result(e, "revoke", rid)


def _delete_rows(e, rid):
    return [(p, c, d) for p, c, d in e.db.execute(
        "SELECT phase, error_code, error_detail FROM operation_log WHERE request_id=? AND action='DELETE_ACCOUNT'"
        " ORDER BY id", (rid,))]


def _ledger(kind):
    with main.app.app_context():
        return {"passwd": main.read_passwd_lines, "shadow": main.read_shadow_lines,
                "group": main.read_group_lines}[kind]()


def test_second_node_account_revoke_succeeds_and_cleans_that_nodes_keytab(env):
    """이슈 재현 A: farm2에서 계정을 지운 뒤 farm7의 계정 회수 — 성공하고 farm7 keytab을 정리한다."""
    e = env
    node = _provision_and_revoke_container(e, "501")
    assert _revoke_account(e, "502", node)["phase"] == "SUCCESS", rows(e, "502")
    assert USER not in passwd_names()

    res = _revoke_account(e, "503", "farm7")

    assert res["phase"] == "SUCCESS", rows(e, "503")
    assert ("krb5_remove", (USER, "farm7")) in e.calls              # 그 노드의 keytab 정리까지 갔다
    (_s, _c, _d), (phase, code, detail) = _delete_rows(e, "503")
    assert phase == "SUCCESS" and code is None
    assert json.loads(detail)["already_absent"] is True


def test_account_revoke_retry_after_lost_response_succeeds(env, monkeypatch):
    """이슈 재현 B: 계정 삭제는 끝났는데 응답이 끊김 → 재시도가 이미 없음을 보고 성공으로 끝낸다."""
    e = env
    node = _provision_and_revoke_container(e, "511")
    real = main.write_group_lines
    state = {"n": 0}

    def write_then_timeout(lines):
        real(lines)
        state["n"] += 1
        if state["n"] == 1:
            raise subprocess.TimeoutExpired("nfs-write", 30)
    monkeypatch.setattr(main, "write_group_lines", write_then_timeout)

    res = _revoke_account(e, "512", node)

    assert res["phase"] == "SUCCESS", rows(e, "512")
    phases = [(p, c) for p, c, _d in _delete_rows(e, "512")]
    assert ("FAIL", "ACCOUNT_FILE_WRITE_FAILED") in phases and phases[-1] == ("SUCCESS", None)
    assert ("REMOVE_KRB5", "SUCCESS") in rows(e, "512")


def test_half_deleted_account_leftovers_are_removed(env):
    """passwd에서만 지워지고 shadow·group 줄이 남은 상태 — 남은 흔적을 치우고 성공한다."""
    e = env
    node = _provision_and_revoke_container(e, "521")
    with main.app.app_context():
        main.write_passwd_lines([l for l in _ledger("passwd") if not l.startswith(USER + ":")])
    assert any(l.startswith(USER + ":") for l in _ledger("shadow"))

    res = _revoke_account(e, "522", node)

    assert res["phase"] == "SUCCESS", rows(e, "522")
    assert not any(l.startswith(USER + ":") for l in _ledger("shadow"))
    assert not any(l.split(":")[0] == USER or USER in l.split(":")[3].split(",") for l in _ledger("group"))
    detail = json.loads(_delete_rows(e, "522")[-1][2])
    assert detail["already_absent"] is True and set(detail["leftovers_removed"]) >= {"shadow", f"group:{USER}"}


def test_full_mode_runs_account_revoke_check_when_already_absent(full, lease_env):
    """Full은 이미 없어도 회수 확인 시험(계정·keytab Secret 부재)까지 실행해 통과해야 완료다."""
    e = full
    e.api.post("/operations/provision", json={"request_id": "531", "username": USER,
                                              "account": {"passwd_base64": PW}})
    tick(e)
    pod = next(iter(e.v1.pods))
    node = e.v1.pods[pod].spec.node_name
    e.api.post("/operations/revoke", json={"request_id": "531", "pod_name": pod})
    tick(e)
    assert _revoke_account(e, "532", node)["phase"] == "SUCCESS", rows(e, "532")

    res = _revoke_account(e, "533", "farm7")

    assert res["phase"] == "SUCCESS", rows(e, "533")
    assert ("VERIFY_REVOKED", "SUCCESS") in rows(e, "533")


def test_absent_account_with_remaining_container_is_still_held(env):
    """보류 규칙은 그대로다 — 계정이 없어도 같은 사용자의 컨테이너가 남아 있으면 회수하지 않는다."""
    e = env
    e.api.post("/operations/provision", json={"request_id": "541", "username": USER,
                                              "account": {"passwd_base64": PW}})
    tick(e)
    with main.app.app_context():
        main.write_passwd_lines([l for l in _ledger("passwd") if not l.startswith(USER + ":")])

    res = _revoke_account(e, "542", "farm2")

    assert res["phase"] == "FAIL" and res["error_code"] == "ACCOUNT_IN_USE", rows(e, "542")
    assert len(e.v1.pods) == 1


def test_baseline_keeps_failing_on_absent_account(env, monkeypatch):
    """baseline은 운영 시절 동작(404로 실패, Kerberos 정리 없음)을 그대로 재현한다."""
    e = env
    monkeypatch.setattr(main, "VERIFY_MODE", "baseline")
    node = _provision_and_revoke_container(e, "551")
    assert _revoke_account(e, "552", node)["phase"] == "SUCCESS"

    res = _revoke_account(e, "553", "farm7")

    assert res["phase"] == "FAIL" and "not found" in res["error_code"], rows(e, "553")
    assert ("krb5_remove", (USER, "farm7")) not in e.calls


@pytest.mark.parametrize("mode", ["noprobe", "baseline"])
def test_present_account_revoke_is_unchanged(env, monkeypatch, mode):
    """계정이 있으면 이전과 같이 지우고 성공한다(표시 없음)."""
    e = env
    monkeypatch.setattr(main, "VERIFY_MODE", mode)
    node = _provision_and_revoke_container(e, "561")

    assert _revoke_account(e, "562", node)["phase"] == "SUCCESS", rows(e, "562")
    assert USER not in passwd_names()
    phase, code, detail = _delete_rows(e, "562")[-1]
    assert phase == "SUCCESS" and not detail
