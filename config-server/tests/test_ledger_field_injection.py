"""원장(passwd/group/shadow) 한 칸에 구분자나 줄바꿈을 넣어 다른 줄을 끼워 넣지 못하게 한다.

gecos 는 admin_be 가입 때 사용자가 적은 이름이다. 이 값에 줄바꿈이 섞이면 원장에 uid 0 같은
임의의 계정 줄이 생기므로, 요청 입구(모델)와 원장 쓰기(formatter) 양쪽에서 막는다.
"""
import pytest
from pydantic import ValidationError

import main
import request_models as rm
import utils

INJECTIONS = [
    "a:/root:/bin/bash",              # 칸 밀기
    "a\npwn::0:0::/root:/bin/bash",   # 줄 끼워 넣기
    "a\rpwn",
    "a\u2028pwn",                     # str.splitlines 가 줄로 끊는 유니코드 줄 구분자
    "a\u2029pwn",
    "a\x85pwn",
    "a\x1epwn",
]


def _passwd(**over):
    entry = {"name": "u", "passwd": "x", "uid": 55001, "gid": 55001,
             "gecos": "홍길동", "home": "/home/u", "shell": "/bin/bash"}
    entry.update(over)
    return entry


@pytest.mark.parametrize("value", INJECTIONS)
def test_passwd_entry_refuses_separator_or_line_break_in_any_field(value):
    for field in ("name", "passwd", "gecos", "home", "shell"):
        with pytest.raises(ValueError):
            utils.format_passwd_entry(_passwd(**{field: value}))


@pytest.mark.parametrize("value", INJECTIONS)
def test_group_and_shadow_entries_refuse_injection(value):
    with pytest.raises(ValueError):
        utils.format_group_entry({"name": "g", "gid": 70001, "members": ["u", value]})
    with pytest.raises(ValueError):
        utils.format_shadow_entry({"name": "u", "passwd": value})


def test_normal_entries_keep_their_shape():
    line = utils.format_passwd_entry(_passwd(gecos="Hong Gil-dong, 컴퓨터공학과"))
    assert line == "u:x:55001:55001:Hong Gil-dong, 컴퓨터공학과:/home/u:/bin/bash"
    assert utils.parse_passwd_line(line)["gecos"] == "Hong Gil-dong, 컴퓨터공학과"
    assert utils.format_group_entry({"name": "g", "gid": 70001, "members": ["a", "b"]}) == "g:x:70001:a,b"
    assert utils.format_shadow_entry({"name": "u", "passwd": "!"}) == "u:!:0:0:99999:7:::"


@pytest.mark.parametrize("value", INJECTIONS + ["a\tb", "a\x00b"])
def test_provision_account_rejects_unsafe_gecos(value):
    with pytest.raises(ValidationError):
        rm.ProvisionAccount(passwd_base64="cHc=", gecos=value)


def test_provision_account_accepts_ordinary_names():
    for name in ("홍길동", "Hong Gil-dong", "O'Brien", "李小龍", ""):
        assert rm.ProvisionAccount(passwd_base64="cHc=", gecos=name).gecos == name


def test_provision_endpoint_answers_400_for_injected_gecos():
    r = main.app.test_client().post("/operations/provision", json={
        "request_id": "1", "username": "u",
        "account": {"passwd_base64": "cHc=", "gecos": "a\npwn::0:0::/root:/bin/bash"}})
    body = r.get_json()
    assert r.status_code == 400, body
    assert body["error"] == "INVALID_REQUEST"
    assert any(e["field"] == "account.gecos" for e in body["errors"])
