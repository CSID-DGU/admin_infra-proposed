"""로그인 비밀번호가 없으면 컨테이너를 만들지 않는다(시작 스크립트의 공개 기본 비밀번호 방지).
마이그레이션은 옛 Pod의 비밀번호 Secret을 이어받는다."""
import base64
import types

import pytest
from kubernetes.client.exceptions import ApiException

import main
from lifecycle_steps import provision


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def test_empty_or_invalid_password_is_rejected():
    for bad in (None, "", _b64(""), "%%not-base64%%"):
        with pytest.raises(provision.LoginPasswordMissing):
            provision.decode_login_password(bad)
    assert provision.decode_login_password(_b64("pw1234!")) == "pw1234!"


class _SecretsOnly:
    def __init__(self, data=None):
        self.data = data

    def read_namespaced_secret(self, name, ns):
        if self.data is None:
            raise ApiException(status=404, reason="Not Found")
        return types.SimpleNamespace(data=self.data)


def test_recreate_prefers_old_pod_secret_over_request_config():
    v1 = _SecretsOnly({"USER_PW": _b64("from-old-pod")})
    assert provision.login_password_for_recreate(v1, "ns", "ailab-u-old", {"passwd_base64": _b64("")}) == _b64("from-old-pod")


def test_recreate_falls_back_to_request_config_when_no_old_secret():
    v1 = _SecretsOnly(None)
    assert provision.login_password_for_recreate(v1, "ns", "ailab-u-old", {"passwd_base64": _b64("cfg")}) == _b64("cfg")


def test_recreate_refuses_when_no_password_anywhere():
    with pytest.raises(provision.LoginPasswordMissing):
        provision.login_password_for_recreate(_SecretsOnly(None), "ns", "ailab-u-old", {"passwd_base64": _b64("")})
