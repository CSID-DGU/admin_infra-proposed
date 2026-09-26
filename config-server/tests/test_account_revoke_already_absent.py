"""계정 회수에서 계정이 이미 없을 때 (#213).

계정 회수(`delete_account=true`)는 admin_be가 사용자가 쓴 노드마다 하나씩 등록한다(admin_be #600). 계정 파일은
스택에 하나라, 첫 노드의 계정 회수가 계정을 지우면 나머지 노드의 계정 회수는 "이미 없음"을 만난다. 목표 상태
(계정 없음)에 이미 도달한 것이므로 NoProbe·Full은 성공으로 보고 그 노드의 Kerberos 정리까지 진행한다.
이미 없었다는 사실은 DELETE_ACCOUNT SUCCESS 행의 error_detail(already_absent)로 구분해 남긴다.

단, "계정 파일에 없음"만으로는 믿지 않는다. 마지막 계정 생성 이후에 계정 파일을 실제로 고친 삭제 기록
(지운 증거)이 있어야 성공으로 본다. 계정 파일이 비었거나, 없던 사용자를 회수하라는 요청이면 증거가 없어
기존처럼 USER_NOT_FOUND로 실패한다.
baseline은 운영 시절처럼 USER_NOT_FOUND로 실패한다. 컨테이너 회수는 바뀌지 않는다.
"""
import json
import subprocess

import pytest

import main
from test_e2e_virtual import env, full, PW, tick, rows, result, passwd_names  # noqa: F401  (env·full은 fixture)

USER = "exp-np-rv1"


def _provision_and_revoke_container(e, rid, **account):
    """생성 작업을 끝내고 그 컨테이너를 회수한다(계정은 남는다). 컨테이너가 떠 있던 노드를 돌려준다."""
    e.api.post("/operations/provision", json={"request_id": rid, "username": USER,
                                              "account": {"passwd_base64": PW, **account}})
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
    detail = json.loads(detail)
    assert detail["already_absent"] is True and detail["deleted_by"]["request_id"] == "502"   # 첫 노드가 지웠다


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


def test_half_deleted_account_leftovers_are_removed(env, monkeypatch):
    """삭제가 passwd만 쓰고 shadow 쓰기에서 실패 → 재시도는 passwd에 없음을 보고, 쓰다 만 삭제 기록을 증거로
    남은 shadow·group 흔적을 치운 뒤 성공한다."""
    e = env
    node = _provision_and_revoke_container(e, "521")
    real = main.write_shadow_lines
    state = {"n": 0}

    def fail_once(lines):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("nfs write error")
        real(lines)
    monkeypatch.setattr(main, "write_shadow_lines", fail_once)

    res = _revoke_account(e, "522", node)

    assert res["phase"] == "SUCCESS", rows(e, "522")
    assert not any(l.startswith(USER + ":") for l in _ledger("shadow"))
    assert not any(l.split(":")[0] == USER or USER in l.split(":")[3].split(",") for l in _ledger("group"))
    detail = json.loads(_delete_rows(e, "522")[-1][2])
    assert detail["already_absent"] is True and set(detail["leftovers_removed"]) >= {"shadow", f"group:{USER}"}
    assert detail["deleted_by"]["error_code"] == "ACCOUNT_FILE_WRITE_FAILED"


# ---------- 지운 증거가 없으면 이미 없음을 인정하지 않는다 ----------

def _drop_from_passwd(name):
    with main.app.app_context():
        main.write_passwd_lines([l for l in _ledger("passwd") if not l.startswith(name + ":")])


def test_absent_without_deletion_record_fails_like_before(env):
    """계정 파일에서 사라졌는데 누가 지운 기록이 없다(계정 파일 손상·잘못 읽음) — 회수 완료로 보지 않고
    기존처럼 USER_NOT_FOUND로 실패한다. Kerberos 정리도 하지 않는다."""
    e = env
    node = _provision_and_revoke_container(e, "571")
    _drop_from_passwd(USER)

    res = _revoke_account(e, "572", node)

    assert res["phase"] == "FAIL" and "not found" in res["error_code"], rows(e, "572")
    assert ("REMOVE_KRB5", "START") not in rows(e, "572")


def test_absent_record_is_not_evidence_for_the_next_absent(env):
    """증거 없이 실패한 "없음"(또는 이미 없음으로 넘긴 기록)은 다음 회수의 증거가 되지 않는다."""
    e = env
    node = _provision_and_revoke_container(e, "573")
    _drop_from_passwd(USER)
    assert _revoke_account(e, "574", node)["phase"] == "FAIL"

    res = _revoke_account(e, "575", "farm7")

    assert res["phase"] == "FAIL", rows(e, "575")


def test_never_existing_user_revoke_fails(env):
    """한 번도 만들어진 적 없는 사용자의 계정 회수 요청 — 성공으로 기록하지 않는다."""
    e = env

    e.api.post("/operations/revoke", json={"request_id": "576", "username": "exp-np-ghost", "node_name": "farm2",
                                           "delete_account": True})
    tick(e)

    res = result(e, "revoke", "576")
    assert res["phase"] == "FAIL" and "not found" in res["error_code"], rows(e, "576")


def test_deletion_before_recreation_is_not_evidence(env):
    """지웠다가 다시 만든 계정은, 다시 만든 뒤의 삭제 기록만 증거다 — 옛 삭제 기록으로 새 계정의 부재를 믿지 않는다."""
    e = env
    node = _provision_and_revoke_container(e, "581")
    old_uid = e.api.get("/operations/provision/581").get_json()["result"]["uid"]
    assert _revoke_account(e, "582", node)["phase"] == "SUCCESS"            # 정상 삭제(옛 증거)
    # 같은 사람이 돌아와 다시 생성 — admin_be처럼 예전 uid를 보내 보존된 홈을 이어받는다
    node = _provision_and_revoke_container(e, "583", expected_uid=old_uid)
    _drop_from_passwd(USER)

    res = _revoke_account(e, "584", node)

    assert res["phase"] == "FAIL", rows(e, "584")


def test_evidence_lookup_failure_does_not_accept_absence(env, monkeypatch):
    """작업 기록을 조회하지 못하면 판단하지 않고 기존처럼 실패로 남긴다."""
    from lifecycle_steps import revoke
    e = env
    node = _provision_and_revoke_container(e, "591")
    assert _revoke_account(e, "592", node)["phase"] == "SUCCESS"

    def boom(conn, username):
        raise RuntimeError("log db down")
    monkeypatch.setattr(revoke, "_query_deletion_evidence", boom)

    res = _revoke_account(e, "593", "farm7")

    assert res["phase"] == "FAIL", rows(e, "593")


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
