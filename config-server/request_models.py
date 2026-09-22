"""config-server HTTP 요청 본문 모델과 검증 데코레이터.

경로마다 get_json 뒤에 필수 키·타입·목록 요소·base64를 손으로 검사하던 것을 모델로 모은다.

- 경로 함수는 ``@validate_body(모델)`` 로 검증이 끝난 모델을 ``body`` 인자로 받는다.
- 검증 실패는 경로와 무관하게 같은 형식의 400으로 응답한다:
  ``infra_error("VALIDATE_REQUEST", "INVALID_REQUEST", <첫 위반>, errors=[{field, message}, ...])``
- Swagger 요청 스키마는 ``swagger_definitions()`` 가 이 모델에서 만든다. 경로 docstring에는
  ``$ref: '#/definitions/<모델 이름>'`` 만 적는다.
"""
import base64
import re
import functools
from typing import List, Optional

from flask import jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from error import infra_error

POD_NAME_PREFIX = "ailab-"


class RequestBody(BaseModel):
    # 모르는 필드는 무시한다(admin_be가 필드를 먼저 늘려도 깨지지 않게). 문자열 앞뒤 공백은 지운다.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


def _request_id(value):
    """admin_be 신청 번호(양의 정수). 작업 이력을 신청 기록과 잇는 키라 숫자만 받는다(bool 거절)."""
    text = "" if value is None or isinstance(value, bool) else str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError("request_id는 admin_be 신청 번호(양의 정수)여야 합니다")
    return str(int(text))


_VALID_UNIX_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _optional_text(value):
    return None if value is None or value == "" else str(value)


def _pod_name(value):
    if value is not None and not str(value).startswith(POD_NAME_PREFIX):
        raise ValueError(f"pod_name은 {POD_NAME_PREFIX}로 시작해야 합니다")
    return value


class SupplementaryGroup(RequestBody):
    name: str = Field(min_length=1, examples=["ASCP"])
    gid: int = Field(examples=[20004])


class ProvisionAccount(RequestBody):
    """계정을 새로 만들 때 함께 보내는 값. 계정이 이미 있으면 account 자체를 뺀다."""
    passwd_base64: str = Field(description="UTF-8 평문 비밀번호를 base64로 인코딩한 값", examples=["cHc="])
    gecos: str = ""
    primary_group_name: Optional[str] = Field(default=None, description="생략하면 username")
    supplementary_groups: List[SupplementaryGroup] = []

    @field_validator("passwd_base64")
    @classmethod
    def _decodable(cls, value):
        try:
            base64.b64decode(value, validate=True).decode("utf-8")
        except Exception:
            raise ValueError("passwd_base64는 UTF-8 문자열을 base64로 인코딩한 값이어야 합니다")
        return value

    def plaintext_password(self) -> str:
        return base64.b64decode(self.passwd_base64, validate=True).decode("utf-8")


class ProvisionRequest(RequestBody):
    request_id: str = Field(description="admin_be 신청 번호(양의 정수)", examples=["4821"])
    username: str = Field(min_length=1, examples=["exp-np-001"])
    account: Optional[ProvisionAccount] = None
    supplementary_groups: List[SupplementaryGroup] = Field(
        default_factory=list,
        description="Pod 생성 후 사용자를 추가할 보조 그룹들. account가 없을 때도 사용 가능(기존 계정 재사용)"
    )

    @field_validator("request_id", mode="before")
    @classmethod
    def _rid(cls, value):
        return _request_id(value)


class RevokeRequest(RequestBody):
    request_id: str = Field(description="admin_be 신청 번호(양의 정수)", examples=["4821"])
    pod_name: Optional[str] = Field(default=None, examples=["ailab-exp-np-001-7f3a9c21"])
    username: Optional[str] = Field(default=None, description="pod_name이 없을 때 필요")
    node_name: Optional[str] = Field(default=None, description="keytab을 지울 노드. 없으면 지운 Pod의 노드")
    delete_account: bool = False

    @field_validator("request_id", mode="before")
    @classmethod
    def _rid(cls, value):
        return _request_id(value)

    @field_validator("pod_name", "username", "node_name", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        return _optional_text(value)

    @field_validator("pod_name")
    @classmethod
    def _pod(cls, value):
        return _pod_name(value)

    @model_validator(mode="after")
    def _target(self):
        if not self.pod_name and not (self.username and self.delete_account):
            raise ValueError("pod_name, 또는 delete_account와 username이 필요합니다")
        return self


class DeletePodRequest(RequestBody):
    pod_name: str = Field(examples=["ailab-exp-np-001-7f3a9c21"])
    request_id: Optional[str] = Field(default=None, description="이 Pod를 만든 신청 번호. 없으면 이 삭제 호출만 묶는 임시 키")

    @field_validator("request_id", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        return _optional_text(value)

    @field_validator("pod_name")
    @classmethod
    def _pod(cls, value):
        return _pod_name(value)


class MigrateRequest(RequestBody):
    request_id: str = Field(description="admin_be 신청 번호(양의 정수)", examples=["4821"])
    pod_name: Optional[str] = Field(default=None, description="옮길 Pod. 없으면 사용자의 실행 중인 Pod",
                                    examples=["ailab-exp-np-001-7f3a9c21"])
    username: str = Field(min_length=1, examples=["exp-np-001"])
    nodes: List[str] = Field(min_length=1, description="후보 노드 목록(현재 노드 포함)", examples=[["farm1", "farm2"]])
    min_improvement_ratio: Optional[float] = Field(default=None, ge=0, le=1, description="생략하면 기본값 0.2")
    force: Optional[bool] = Field(default=None, description="true면 개선 비율을 보지 않고 가장 여유 있는 노드로 이전")

    @field_validator("request_id", mode="before")
    @classmethod
    def _rid(cls, value):
        return _request_id(value)

    @field_validator("pod_name", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        return _optional_text(value)

    @field_validator("pod_name")
    @classmethod
    def _pod(cls, value):
        return _pod_name(value)


class AddGroupRequest(RequestBody):
    name: str = Field(min_length=1, examples=["developers"])

    @field_validator("name")
    @classmethod
    def _name(cls, value):
        # 이 이름은 AD DC 로 가는 SSH 명령 문자열에 그대로 들어가고 sAMAccountName 이 된다.
        # 원격 스크립트도 같은 규칙으로 막지만, 보내는 쪽에서 먼저 거른다(#146).
        if not _VALID_UNIX_NAME_RE.match(value):
            raise ValueError("그룹 이름은 [a-z_]로 시작하는 32자 이하의 소문자·숫자·_·- 여야 합니다")
        return value
    gid: Optional[int] = Field(default=None, description="생략하면 그룹 파일 기준으로 자동 할당")
    members: List[str] = []

    @field_validator("gid", mode="before")
    @classmethod
    def _gid(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ValueError("gid는 정수여야 합니다")
        return value


class AddUserGroupsRequest(RequestBody):
    groups: List[str] = Field(min_length=1, examples=[["developers"]])


REQUEST_MODELS = (ProvisionRequest, RevokeRequest, DeletePodRequest, MigrateRequest,
                  AddGroupRequest, AddUserGroupsRequest)


def _errors(exc: ValidationError):
    out = []
    for err in exc.errors(include_url=False):
        field = ".".join(str(part) for part in err.get("loc", ())) or "(본문)"
        message = str(err.get("msg", "")).removeprefix("Value error, ")
        out.append({"field": field, "message": message})
    return out


def check_values(model, data):
    """값을 model로 검증한다. (모델, None) 또는 (None, 400 응답)."""
    if not isinstance(data, dict):
        return None, (jsonify(infra_error("VALIDATE_REQUEST", "INVALID_REQUEST",
                                          "요청 본문은 JSON 객체여야 합니다", errors=[])), 400)
    try:
        return model.model_validate(data), None
    except ValidationError as exc:
        errors = _errors(exc)
        first = f"{errors[0]['field']}: {errors[0]['message']}" if errors else "잘못된 요청"
        return None, (jsonify(infra_error("VALIDATE_REQUEST", "INVALID_REQUEST", first, errors=errors)), 400)


def validate_body(model):
    """요청 본문을 model로 검증해 경로 함수에 body 인자로 넘긴다. 실패하면 경로 함수를 부르지 않고 400."""
    def decorate(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            body, error = check_values(model, request.get_json(silent=True))
            if error is not None:
                return error
            return view(*args, body=body, **kwargs)
        return wrapper
    return decorate


def _swagger2(node):
    """pydantic의 JSON 스키마(OpenAPI 3 계열)를 flasgger(Swagger 2.0)가 읽는 모양으로 바꾼다.
    Optional[X]가 만드는 anyOf [X, null]을 X로 펼친다."""
    if isinstance(node, dict):
        any_of = node.get("anyOf")
        if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
            other = next(s for s in any_of if s != {"type": "null"})
            node = {**{k: v for k, v in node.items() if k != "anyOf"}, **other}
        return {k: _swagger2(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_swagger2(v) for v in node]
    return node


def swagger_definitions():
    """요청 모델의 Swagger definitions. 중첩 모델(계정·보조 그룹)도 같은 곳에 둔다."""
    definitions = {}
    for model in REQUEST_MODELS:
        schema = model.model_json_schema(ref_template="#/definitions/{model}")
        definitions.update(schema.pop("$defs", {}))
        definitions[model.__name__] = schema
    return _swagger2(definitions)
