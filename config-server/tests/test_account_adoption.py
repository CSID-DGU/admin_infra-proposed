"""원장에 남은 본인 계정 이어받기 — admin_be 의 계정 기록만 빠졌을 때 USER_ALREADY_EXISTS 로 막히지 않게 한다."""
import pytest

import main

HASH = "$6$saltsalt$" + "a" * 86
OLD_HASH = "$6$oldsalt$" + "b" * 86


@pytest.fixture
def etc(tmp_path, monkeypatch):
    base = str(tmp_path / "etc")
    for k, val in list(main.app.config.items()):
        if isinstance(val, str) and val.startswith(main.BASE_ETC_DIR):
            monkeypatch.setitem(main.app.config, k, base + val[len(main.BASE_ETC_DIR):])
    with main.app.app_context():
        main.ensure_etc_layout()
        main.write_passwd_lines(main.read_passwd_lines() + [
            main.format_passwd_entry({"name": "alice", "passwd": "x", "uid": 55000, "gid": 55000,
                                      "gecos": "", "home": "/home/alice", "shell": "/bin/bash"})])
        main.write_shadow_lines(main.read_shadow_lines() + [
            main.format_shadow_entry({"name": "alice", "passwd": OLD_HASH, "lastchg": 1})])
        yield


@pytest.fixture
def registered(monkeypatch):
    """_register_job 을 가로챈다. 반환: 등록된 job 목록"""
    jobs = []

    def fake(kind, request_id, username, job):
        jobs.append(job)
        return main.jsonify({"request_id": request_id, "job_id": 1, "status": "accepted"}), 202
    monkeypatch.setattr(main, "_register_job", fake)
    return jobs


def _body(expected_uid):
    account = {"passwd_hash": HASH, "supplementary_groups": [{"name": "teamx", "gid": 70000}]}
    if expected_uid is not None:
        account["expected_uid"] = expected_uid
    return {"request_id": "41", "username": "alice", "account": account,
            "supplementary_groups": [{"name": "teamy", "gid": 70001}]}


def _shadow_hash():
    with main.app.app_context():
        return next(r for l in main.read_shadow_lines() if (r := main.parse_shadow_line(l)) and r["name"] == "alice")["passwd"]


def test_same_uid_is_adopted_as_existing_account(etc, api, registered):
    r = api.post("/operations/provision", json=_body(55000))
    assert r.status_code == 202
    job = registered[0]
    assert "account" not in job and job["adopted_uid"] == 55000
    assert {g["name"] for g in job["supp_groups_only"]} == {"teamx", "teamy"}
    assert _shadow_hash() == HASH


@pytest.mark.parametrize("expected_uid", [None, 55001])
def test_other_or_unknown_uid_still_tries_to_create(etc, api, registered, expected_uid):
    """같은 이름을 쓰던 다른 사람의 계정일 수 있다 — 이어받지 않고 새로 만들다 막히게 둔다."""
    r = api.post("/operations/provision", json=_body(expected_uid))
    assert r.status_code == 202
    assert "account" in registered[0] and "adopted_uid" not in registered[0]
    assert _shadow_hash() == OLD_HASH


def test_success_result_carries_ledger_uid_for_reused_account(etc, monkeypatch):
    from application import jobs
    saved = []
    monkeypatch.setattr(main, "save_job_result", lambda a, r, d: saved.append(d))
    monkeypatch.setattr(main, "log_operation", lambda **kw: None)
    ctx = {"request_id": "41", "username": "alice", "pod_name": "ailab-alice-1", "node": "farm2"}
    with main.app.app_context():
        jobs._finish_job("provision", "41", "alice", main.Phase.SUCCESS, ctx=ctx)
    assert saved and saved[0]["uid"] == 55000 and saved[0]["gid"] == 55000
