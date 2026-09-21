"""#148 공용 gid 대역 분리 — 개인 그룹(gid=uid)과 팀 그룹이 같은 번호를 두고 다투지 않는지."""
import pytest

import main
from lifecycle_steps import provision


@pytest.fixture
def etc(tmp_path, monkeypatch):
    """계정 원장을 임시 폴더의 실제 파일로 바꾼다(test_e2e_virtual.py와 같은 방식).
    반환한 함수로 passwd/group 초기 내용을 넣는다."""
    base = str(tmp_path / "etc")
    for k, val in list(main.app.config.items()):
        if isinstance(val, str) and val.startswith(main.BASE_ETC_DIR):
            monkeypatch.setitem(main.app.config, k, base + val[len(main.BASE_ETC_DIR):])
    monkeypatch.setattr(main, "UID_MIN", 21000)
    monkeypatch.setattr(main, "UID_MAX", 49999)
    monkeypatch.setattr(main, "SHARED_GID_MIN", 70000)
    monkeypatch.setattr(main, "SHARED_GID_MAX", 79999)
    with main.app.app_context():        # utils 의 app.config 는 current_app 프록시다
        main.ensure_etc_layout()

        def seed(passwd=(), group=()):
            main.write_passwd_lines(main.read_passwd_lines() + list(passwd))
            main.write_group_lines(main.read_group_lines() + list(group))
        yield seed


def _gids():
    return {r["name"]: r["gid"] for l in main.read_group_lines() if (r := main.parse_group_line(l))}


def test_new_group_gets_first_shared_band_number(etc, api):
    etc(group=["yoon6yo:x:21000:"])          # uid 대역의 개인 그룹은 후보가 되면 안 된다
    r = api.post("/groups", json={"name": "teamx"})
    assert r.status_code == 201 and r.get_json()["group"]["gid"] == 70000


def test_explicit_gid_must_be_inside_shared_band(etc, api):
    etc()
    r = api.post("/groups", json={"name": "teamx", "gid": 21003})
    assert r.status_code == 400 and r.get_json()["error"] == "GID_OUT_OF_RANGE"

    r = api.post("/groups", json={"name": "teamx", "gid": 70001})
    assert r.status_code == 201 and _gids()["teamx"] == 70001


def test_shared_group_does_not_steal_the_next_uid(etc):
    etc(passwd=["yoon6yo:x:21000:21000::/home/yoon6yo:/bin/bash",
                "csuhyeon:x:21001:21001::/home/csuhyeon:/bin/bash",
                "dongmin0204:x:21002:21002::/home/dongmin0204:/bin/bash"],
        group=["yoon6yo:x:21000:", "csuhyeon:x:21001:", "dongmin0204:x:21002:",
               "testgroup:x:70000:yoon6yo"])
    assert provision._allocate_next_uid(main.read_passwd_lines(), min_uid=21000) == 21003
    assert provision._allocate_next_gid(main.read_group_lines(), min_gid=70000) == 70001


def test_primary_group_name_collision_fails_and_rolls_back(etc, logs):
    etc(passwd=["yoon6yo:x:21000:21000::/home/yoon6yo:/bin/bash"],
        group=["yoon6yo:x:21000:", "newbie:x:21099:"])   # 이름만 같고 gid가 다른 줄
    ctx = {"request_id": "r1", "name": "newbie", "pg_name": "newbie",
           "supp_groups": [], "gecos": "", "plaintext_pw": "pw"}
    with pytest.raises(main.StepFailed):
        provision.step_create_account(ctx)
    assert "newbie" not in {r["name"] for l in main.read_passwd_lines() if (r := main.parse_passwd_line(l))}
    assert [r["error_code"] for r in logs if r.get("phase") == main.Phase.FAIL] == ["GROUP_WRITE_FAILED"]
