"""config-server HTTP 요청 본문 모델과 검증 데코레이터.

경로마다 get_json 뒤에 필수 키·타입·목록 요소·base64를 손으로 검사하던 것을 모델로 모은다.

- 경로 함수는 ``@validate_body(모델)`` 로 검증이 끝난 모델을 ``body`` 인자로 받는다.
- 검증 실패는 경로와 무관하게 같은 형식의 400으로 응답한다:
  ``infra_error("VALIDATE_REQUEST", "INVALID_REQUEST", <첫 위반>, errors=[{field, message}, ...])``
- Swagger 요청 스키마는 ``swagger_definitions()`` 가 이 모델에서 만든다. 경로 docstring에는
  ``$ref: '#/definitions/<모델 이름>'`` 만 적는다.
"""
import base64
import crypt
import re
import functools
import unicodedata
from typing import Annotated, List, Literal, Optional

from flask import jsonify, request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from error import infra_error

# SHA-512 crypt($6$[rounds=N$]salt$hash). 이미지 entrypoint.sh 의 USER_PW_HASH_RE 와 같은 규칙이다.
SHA512_CRYPT_RE = re.compile(r"^\$6\$(rounds=[0-9]+\$)?[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}$")

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


def is_valid_unix_name(value: str) -> bool:
    """본문이 아닌 경로로 받은 사용자·그룹 이름을 같은 규칙으로 거를 때 쓴다."""
    return bool(_VALID_UNIX_NAME_RE.match(value))


def _unix_name(value: str) -> str:
    if not _VALID_UNIX_NAME_RE.match(value):
        raise ValueError("이름은 [a-z_]로 시작하는 32자 이하의 소문자·숫자·_·- 여야 합니다")
    return value


# 사용자·그룹 이름. 원장(passwd·group) 칸, 셸 명령, AD sAMAccountName 에 그대로 들어가므로
# 본문으로 받는 이름은 모두 이 타입으로 받는다(경로로 받는 이름은 is_valid_unix_name).
UnixName = Annotated[str, AfterValidator(_unix_name)]


def _optional_text(value):
    return None if value is None or value == "" else str(value)


def _pod_name(value):
    if value is not None and not str(value).startswith(POD_NAME_PREFIX):
        raise ValueError(f"pod_name은 {POD_NAME_PREFIX}로 시작해야 합니다")
    return value


class SupplementaryGroup(RequestBody):
    name: UnixName = Field(examples=["ascp"])
    # 비어 있으면 아직 인프라에 없는 새 공유 그룹이다(admin_be 의 승인 대기 그룹). 생성 작업이 이 이름으로
    # gid 를 정해 그룹을 만들고, 정한 gid 를 작업 결과(result.groups)로 돌려준다.
    gid: Optional[int] = Field(default=None, ge=1, examples=[20004],
                               description="생략하면 새 그룹 — 생성 작업이 gid 를 발급한다")


class ProvisionAccount(RequestBody):
    """계정을 새로 만들 때 함께 보내는 값. 계정이 이미 있으면 account 자체를 뺀다.
    비밀번호는 해시(passwd_hash)와 평문(passwd_base64, 해시 도입 전 admin_be) 중 하나만 보낸다."""
    passwd_hash: Optional[str] = Field(default=None, description="SHA-512 crypt 해시($6$...)",
                                       examples=["$6$saltsalt$" + "a" * 86])
    passwd_base64: Optional[str] = Field(default=None, description="UTF-8 평문 비밀번호를 base64로 인코딩한 값(구 방식)",
                                         examples=["cHc="])
    gecos: str = Field(default="", max_length=256, description="사람 이름. 원장 칸 구분자(:)·제어 문자·줄바꿈 불가")
    primary_group_name: Optional[UnixName] = Field(default=None, description="생략하면 username")
    supplementary_groups: List[SupplementaryGroup] = []
    expected_uid: Optional[int] = Field(
        default=None, ge=1,
        description="이 사용자가 예전에 쓰던 UID. 원장에 같은 이름·UID의 계정이 남아 있으면 새로 만들지 않고 이어받는다. "
                    "원장에 없으면 NAS 홈 소유자가 이 값과 같을 때 새 번호 대신 이 번호로 계정을 만든다")

    @field_validator("passwd_hash")
    @classmethod
    def _sha512_crypt(cls, value):
        if value is not None and not SHA512_CRYPT_RE.match(value):
            raise ValueError("passwd_hash는 SHA-512 crypt 해시($6$...)여야 합니다")
        return value

    @field_validator("gecos")
    @classmethod
    def _ledger_safe_gecos(cls, value):
        # 이 값은 passwd 원장의 한 칸이 된다. 구분자나 줄바꿈이 섞이면 호출자가 원장에 임의의 줄을 끼워 넣는다.
        if ":" in value or any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in value):
            raise ValueError("gecos에는 콜론(:)·제어 문자·줄바꿈을 쓸 수 없습니다")
        return value

    @field_validator("passwd_base64")
    @classmethod
    def _decodable(cls, value):
        if value is None:
            return value
        try:
            base64.b64decode(value, validate=True).decode("utf-8")
        except Exception:
            raise ValueError("passwd_base64는 UTF-8 문자열을 base64로 인코딩한 값이어야 합니다")
        return value

    @model_validator(mode="after")
    def _exactly_one_password(self):
        if (self.passwd_hash is None) == (self.passwd_base64 is None):
            raise ValueError("passwd_hash와 passwd_base64 중 하나만 보내야 합니다")
        return self

    def password_hash(self) -> str:
        if self.passwd_hash is not None:
            return self.passwd_hash
        plaintext = base64.b64decode(self.passwd_base64, validate=True).decode("utf-8")
        return crypt.crypt(plaintext, crypt.mksalt(crypt.METHOD_SHA512))


class ProvisionRequest(RequestBody):
    request_id: str = Field(description="admin_be 신청 번호(양의 정수)", examples=["4821"])
    username: UnixName = Field(examples=["exp-np-001"])
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
    username: Optional[UnixName] = Field(default=None, description="pod_name이 없을 때 필요")
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
    username: UnixName = Field(examples=["exp-np-001"])
    recreate: Optional[bool] = Field(default=None, description="true면 현재 노드에서 Pod를 다시 만든다")
    keep_changes: Optional[bool] = Field(default=None, description="recreate일 때 컨테이너 변경분을 구워 유지(기본 true). "
                                                                   "false면 기본 이미지로 초기화")
    nodes: List[str] = Field(default_factory=list, validate_default=True,
                             description="후보 노드 목록(현재 노드 포함). recreate면 쓰지 않는다",
                             examples=[["farm1", "farm2"]])
    min_improvement_ratio: Optional[float] = Field(default=None, ge=0, le=1, description="생략하면 기본값 0.2")
    force: Optional[bool] = Field(default=None, description="true면 개선 비율을 보지 않고 가장 여유 있는 노드로 이전")

    @field_validator("nodes")
    @classmethod
    def _nodes_unless_recreate(cls, value, info):
        if not value and not info.data.get("recreate"):
            raise ValueError("nodes must not be empty unless recreate is true")
        return value

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


class GroupJobRequest(RequestBody):
    """공용 그룹 작업 등록. op 에 따라 쓰는 필드가 다르다.

    - create: name(그룹 이름), 선택 gid·members
    - add: username, groups(더할 그룹 이름들)
    - remove: username, name(뺄 그룹 이름)
    """
    request_id: str = Field(description="admin_be 그룹 작업 번호(양의 정수)", examples=["7"])
    op: Literal["create", "add", "remove"]
    # 이름들은 AD DC 로 가는 SSH 명령 문자열에 그대로 들어가고 sAMAccountName 이 된다.
    # 원격 스크립트도 같은 규칙으로 막지만, 보내는 쪽에서 먼저 거른다(#146).
    username: Optional[UnixName] = Field(default=None, examples=["exp-np-001"])
    name: Optional[UnixName] = Field(default=None, examples=["developers"])
    gid: Optional[int] = Field(default=None, description="create 전용. 생략하면 그룹 파일 기준으로 자동 할당")
    members: List[UnixName] = []
    groups: List[UnixName] = []

    @field_validator("request_id", mode="before")
    @classmethod
    def _rid(cls, value):
        return _request_id(value)

    @field_validator("gid", mode="before")
    @classmethod
    def _gid(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ValueError("gid는 정수여야 합니다")
        return value

    @model_validator(mode="after")
    def _fields_for_op(self):
        required = {"create": ("name",), "add": ("username", "groups"), "remove": ("username", "name")}[self.op]
        missing = [field for field in required if not getattr(self, field)]
        if missing:
            raise ValueError(f"op={self.op}에는 {', '.join(missing)}가 필요합니다")
        return self


class PasswordChangeRequest(RequestBody):
    """로그인 비밀번호 교체 작업 등록. admin_be가 해시한 값만 받는다(평문은 받지 않는다)."""
    request_id: str = Field(description="admin_be 비밀번호 재설정 신청 번호(양의 정수)", examples=["12"])
    username: UnixName = Field(examples=["exp-np-001"])
    passwd_hash: str = Field(description="SHA-512 crypt 해시($6$...)", examples=["$6$saltsalt$" + "a" * 86])

    @field_validator("request_id", mode="before")
    @classmethod
    def _rid(cls, value):
        return _request_id(value)

    @field_validator("passwd_hash")
    @classmethod
    def _sha512_crypt(cls, value):
        if not SHA512_CRYPT_RE.match(value):
            raise ValueError("passwd_hash는 SHA-512 crypt 해시($6$...)여야 합니다")
        return value


class HomeDeleteRequest(RequestBody):
    """보존 기간이 지난 홈 삭제 작업 등록. 홈 소유자가 expected_uid 와 다르면 지우지 않는다."""
    request_id: str = Field(description="admin_be 홈 정리 번호(양의 정수)", examples=["7"])
    username: UnixName = Field(examples=["exp-np-001"])
    expected_uid: int = Field(gt=0, description="admin_be 가 아는 이 계정의 uid", examples=[50001])

    @field_validator("request_id", mode="before")
    @classmethod
    def _rid(cls, value):
        return _request_id(value)


# 포트 변경이 건드리지 않는 포트: 모든 Pod 의 기본 포트(ssh·jupyter)와 Pod 기동 때만 켤 수 있는 noVNC.
PROTECTED_PORTS = frozenset({22, 8888, 6080})
# 용도 이름으로 기본 포트를 찾고(ssh·jupyter) noVNC 를 켜므로(novnc·vnc), 추가 포트의 용도로는 받지 않는다.
RESERVED_PURPOSES = frozenset({"ssh", "jupyter", "novnc", "vnc"})
MAX_EXTRA_PORTS = 10


class PortSpec(RequestBody):
    internal_port: int = Field(ge=1, le=65535, examples=[3000])
    # nodeport_allocations.purpose 가 255자다.
    usage_purpose: str = Field(default="custom", min_length=1, max_length=255, examples=["웹 서버"])

    @field_validator("internal_port", mode="before")
    @classmethod
    def _port(cls, value):
        if isinstance(value, bool):
            raise ValueError("internal_port는 정수여야 합니다")
        return value

    @field_validator("usage_purpose")
    @classmethod
    def _purpose(cls, value):
        if value.lower() in RESERVED_PURPOSES:
            raise ValueError(f"usage_purpose로 쓸 수 없는 이름입니다: {value}")
        return value


class PortChangeRequest(RequestBody):
    """떠 있는 Pod 의 추가 포트 변경 작업 등록. ports 는 바뀐 뒤 추가 포트 전체 목록이다(빈 목록이면 모두 뺀다)."""
    request_id: str = Field(description="admin_be 포트 작업 번호(양의 정수)", examples=["7"])
    username: UnixName = Field(examples=["exp-np-001"])
    pod_name: str = Field(examples=["ailab-exp-np-001-7f3a9c21"])
    ports: List[PortSpec] = Field(max_length=MAX_EXTRA_PORTS)

    @field_validator("request_id", mode="before")
    @classmethod
    def _rid(cls, value):
        return _request_id(value)

    @model_validator(mode="after")
    def _ports(self):
        if not self.pod_name.startswith(POD_NAME_PREFIX):
            raise ValueError(f"pod_name은 {POD_NAME_PREFIX}로 시작해야 합니다")
        numbers = [p.internal_port for p in self.ports]
        if len(set(numbers)) != len(numbers):
            raise ValueError("같은 internal_port를 두 번 보낼 수 없습니다")
        reserved = sorted(PROTECTED_PORTS.intersection(numbers))
        if reserved:
            raise ValueError(f"기본 포트·noVNC 포트는 바꿀 수 없습니다: {reserved}")
        return self


REQUEST_MODELS = (ProvisionRequest, RevokeRequest, DeletePodRequest, MigrateRequest,
                  GroupJobRequest, PasswordChangeRequest, HomeDeleteRequest, PortChangeRequest)


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
