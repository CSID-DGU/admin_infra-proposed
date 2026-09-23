"""#180 계정을 회수해도 공용 그룹 줄은 남긴다 — 지우면 AD·NAS·admin_be 와 어긋나 승인이
GROUP_NOT_FOUND 로 실패하고, 빈 gid 가 다음 그룹 생성 때 다시 배정된다."""
import pytest

import main
from lifecycle_steps import revoke


@pytest.fixture
def etc(tmp_path, monkeypatch, logs):
    base = str(tmp_path / "etc")
    for k, val in list(main.app.config.items()):
        if isinstance(val, str) and val.startswith(main.BASE_ETC_DIR):
            monkeypatch.setitem(main.app.config, k, base + val[len(main.BASE_ETC_DIR):])
    with main.app.app_context():
        main.ensure_etc_layout()
        main.write_passwd_lines([
            "alice:x:21000:21000::/home/alice:/bin/bash",
            "bob:x:21001:21001::/home/bob:/bin/bash",
        ])
        main.write_group_lines([
            "alice:x:21000:",
            "bob:x:21001:",
            "solo:x:70000:alice",
            "shared:x:70001:alice,bob",
        ])
        yield


def _groups():
    return {r["name"]: r["members"] for l in main.read_group_lines() if (r := main.parse_group_line(l))}


def _revoke(username):
    with main.app.app_context():
        revoke.step_delete_account({"request_id": "r1", "username": username})
        return _groups()


def test_revoke_drops_the_personal_group(etc):
    assert "alice" not in _revoke("alice")


def test_revoke_keeps_a_shared_group_even_when_it_becomes_empty(etc):
    groups = _revoke("alice")
    assert groups["solo"] == []


def test_revoke_removes_the_user_from_shared_group_members(etc):
    groups = _revoke("alice")
    assert groups["shared"] == ["bob"]
    assert groups["bob"] == []
