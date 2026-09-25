"""새 계정명이 이미지 그룹·공용 그룹과 겹치면 작업 등록 전에 400 으로 거절한다."""
import pytest

import main

HASH = "$6$saltsalt$" + "a" * 86


@pytest.fixture
def etc(tmp_path, monkeypatch):
    base = str(tmp_path / "etc")
    for k, val in list(main.app.config.items()):
        if isinstance(val, str) and val.startswith(main.BASE_ETC_DIR):
            monkeypatch.setitem(main.app.config, k, base + val[len(main.BASE_ETC_DIR):])
    with main.app.app_context():
        main.ensure_etc_layout()
        main.write_group_lines(main.read_group_lines() + [
            main.format_group_entry({"name": "teamx", "passwd": "x", "gid": main.SHARED_GID_MIN, "members": []}),
            main.format_group_entry({"name": "leftover", "passwd": "x", "gid": main.UID_MIN + 5, "members": []}),
        ])
        yield


@pytest.fixture
def registered(monkeypatch):
    jobs = []

    def fake(kind, request_id, username, job):
        jobs.append(job)
        return main.jsonify({"request_id": request_id, "job_id": 1, "status": "accepted"}), 202
    monkeypatch.setattr(main, "_register_job", fake)
    return jobs


def _body(username):
    return {"request_id": "41", "username": username, "account": {"passwd_hash": HASH}}


@pytest.mark.parametrize("username", ["docker", "video", "teamx"])
def test_username_matching_image_or_shared_group_is_rejected(etc, api, registered, username):
    r = api.post("/operations/provision", json=_body(username))
    assert r.status_code == 400
    assert r.get_json()["error"] == "USERNAME_CONFLICTS_GROUP"
    assert registered == []


def test_leftover_personal_group_in_uid_range_is_not_a_conflict(etc, api, registered):
    r = api.post("/operations/provision", json=_body("leftover"))
    assert r.status_code == 202
    assert registered[0]["account"]["pg_name"] == "leftover"


def test_unrelated_username_is_registered(etc, api, registered):
    assert api.post("/operations/provision", json=_body("alice")).status_code == 202
