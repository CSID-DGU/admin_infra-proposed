"""요청 모델과 검증 데코레이터.

- 검증 실패 응답은 경로와 무관하게 같은 형식(400, INVALID_REQUEST, errors[field, message])이다.
- 검증을 통과한 값만 경로 함수에 들어간다.
- Swagger 요청 스키마는 모델에서 만들어지고 flasgger가 읽는 모양이다.
"""
import json

import pytest
from pydantic import ValidationError

import main
import request_models as rm


@pytest.fixture
def client():
    return main.app.test_client()


def _invalid(r):
    body = r.get_json()
    assert r.status_code == 400, (r.status_code, body)
    assert body["step"] == "VALIDATE_REQUEST" and body["error"] == "INVALID_REQUEST"
    assert isinstance(body["errors"], list)
    return body


# ---------- 검증 실패 응답 형식 ----------

@pytest.mark.parametrize("method,path,payload,field", [
    ("post", "/operations/provision", {"request_id": "1"}, "username"),
    ("post", "/operations/provision", {"request_id": True, "username": "exp-np-001"}, "request_id"),
    ("post", "/operations/provision", {"request_id": "1", "username": "exp-np-001", "account": {"passwd_base64": "%%"}},
     "account.passwd_base64"),
    ("post", "/operations/provision", {"request_id": "1", "username": "u",
                                       "account": {"passwd_base64": "cHc=", "supplementary_groups": [{"name": "g"}]}},
     "account.supplementary_groups.0.gid"),
    ("post", "/operations/revoke", {"request_id": "1", "pod_name": "other-pod"}, "pod_name"),
    ("delete", "/pods/other-pod", {}, "pod_name"),
    ("post", "/operations/migrate", {"request_id": "1", "username": "u", "nodes": []}, "nodes"),
    ("post", "/operations/migrate", {"request_id": "1", "username": "u", "nodes": ["farm1"], "min_improvement_ratio": 1.5}, "min_improvement_ratio"),
    ("post", "/operations/migrate", {"username": "u", "nodes": ["farm1"]}, "request_id"),
    ("post", "/groups", {"gid": 1}, "name"),
    ("post", "/users/u/groups", {"groups": []}, "groups"),
    ("post", "/groups", {"name": "g", "gid": True}, "gid"),
])
def test_invalid_bodies_share_one_error_shape(client, method, path, payload, field):
    body = _invalid(getattr(client, method)(path, json=payload))
    assert field in [e["field"] for e in body["errors"]]
    assert body["detail"].startswith(body["errors"][0]["field"])


def test_non_object_body_is_rejected(client):
    body = _invalid(client.post("/operations/provision", data=json.dumps([1, 2]), content_type="application/json"))
    assert body["detail"] == "요청 본문은 JSON 객체여야 합니다"


def test_missing_body_is_rejected(client):
    _invalid(client.post("/operations/migrate"))


def test_korean_value_error_message_is_kept_without_prefix(client):
    body = _invalid(client.post("/operations/revoke", json={"request_id": "0", "pod_name": "ailab-u-1"}))
    assert body["errors"][0]["message"] == "request_id는 admin_be 신청 번호(양의 정수)여야 합니다"


# ---------- 모델 규칙 ----------

def test_request_id_accepts_positive_number_or_digit_string_only():
    assert rm.ProvisionRequest(request_id=7, username="u").request_id == "7"
    assert rm.ProvisionRequest(request_id=" 012 ", username="u").request_id == "12"
    for bad in (None, 0, "-3", "smoke-1", True, 1.5):
        with pytest.raises(ValidationError):
            rm.ProvisionRequest(request_id=bad, username="u")


def test_account_password_is_decoded_from_base64():
    account = rm.ProvisionAccount(passwd_base64="cHc=")
    assert account.plaintext_password() == "pw"
    assert account.gecos == "" and account.primary_group_name is None and account.supplementary_groups == []


def test_revoke_needs_pod_or_account_target():
    assert rm.RevokeRequest(request_id="5", pod_name="ailab-u-abc").delete_account is False
    assert rm.RevokeRequest(request_id="5", username="u", delete_account=True, pod_name="").pod_name is None
    with pytest.raises(ValidationError):
        rm.RevokeRequest(request_id="5", username="u")          # 계정 회수 표시 없이 사용자만
    with pytest.raises(ValidationError):
        rm.RevokeRequest(request_id="5", delete_account=True)   # 누구를 회수할지 없음


def test_group_gid_rules():
    assert rm.AddGroupRequest(name="g", gid="12").gid == 12
    assert rm.AddGroupRequest(name="g", gid="").gid is None
    assert rm.AddGroupRequest(name="g").members == []
    for bad in (True, "abc", 1.5):
        with pytest.raises(ValidationError):
            rm.AddGroupRequest(name="g", gid=bad)


def test_migrate_dump_omits_absent_ratio_so_default_applies():
    dumped = rm.MigrateRequest(request_id=1, username="u", nodes=["farm1"]).model_dump(exclude_none=True)
    assert dumped == {"request_id": "1", "username": "u", "nodes": ["farm1"]}


def test_unknown_fields_are_ignored():
    assert rm.DeletePodRequest(pod_name="ailab-u-1", surprise="x").pod_name == "ailab-u-1"


# ---------- Swagger ----------

def test_swagger_definitions_come_from_models_in_swagger2_shape():
    defs = rm.swagger_definitions()
    for model in rm.REQUEST_MODELS:
        assert model.__name__ in defs
    assert {"ProvisionAccount", "SupplementaryGroup"} <= set(defs)   # 중첩 모델도 함께
    text = json.dumps(defs)
    assert "$defs" not in text and '"type": "null"' not in text
    assert defs["ProvisionRequest"]["properties"]["account"]["$ref"] == "#/definitions/ProvisionAccount"


def test_apispec_serves_model_definitions(client):
    r = client.get("/apispec_1.json")
    assert r.status_code == 200
    spec = r.get_json()
    assert "ProvisionRequest" in spec["definitions"] and "CreatePodRequest" not in spec["definitions"]
    assert "/create-pod" not in spec["paths"] and "/operations/provision" in spec["paths"]


def test_migrate_rejects_negative_ratio_and_accepts_force():
    import pydantic
    try:
        rm.MigrateRequest(request_id=1, username="u", nodes=["farm1"], min_improvement_ratio=-1000)
        raise AssertionError("음수 비율이 통과함")
    except pydantic.ValidationError:
        pass
    dumped = rm.MigrateRequest(username="u", nodes=["farm1"], force=True, pod_name="ailab-u-abc", request_id=3).model_dump(exclude_none=True)
    assert dumped == {"request_id": "3", "pod_name": "ailab-u-abc", "username": "u", "nodes": ["farm1"], "force": True}
