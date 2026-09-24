"""로그인 비밀번호가 없으면 컨테이너를 만들지 않는다(시작 스크립트의 공개 기본 비밀번호 방지).
Pod에는 해시만 넘기고, 마이그레이션은 옛 Pod의 비밀번호 Secret을 이어받는다(평문 Secret은 해시로 바꾼다)."""
import base64
import crypt
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


def _is_hash_of(h, plaintext):
    return provision.SHA512_CRYPT_RE.match(h) and crypt.crypt(plaintext, h) == h


HASH = provision.hash_login_password("pw1234!")


def test_login_password_hash_prefers_hash_and_hashes_legacy_plaintext():
    assert provision.login_password_hash({"passwd_hash": HASH, "passwd_base64": _b64("other")}) == HASH
    assert _is_hash_of(provision.login_password_hash({"passwd_base64": _b64("legacy")}), "legacy")


def test_login_password_hash_rejects_empty_or_malformed_hash():
    for info in (None, {}, {"passwd_hash": "", "passwd_base64": ""}, {"passwd_hash": "$6$x"},
                 {"passwd_hash": HASH + ":0:0"}, {"passwd_hash": "$1$abc$def"}):
        with pytest.raises(provision.LoginPasswordMissing):
            provision.login_password_hash(info)


def test_recreate_prefers_old_pod_hash_secret_over_request_config():
    v1 = _SecretsOnly({"USER_PW_HASH": _b64(HASH)})
    assert provision.login_password_for_recreate(v1, "ns", "ailab-u-old", {"passwd_hash": ""}) == HASH


def test_recreate_hashes_legacy_plaintext_secret():
    v1 = _SecretsOnly({"USER_PW": _b64("from-old-pod")})
    assert _is_hash_of(provision.login_password_for_recreate(v1, "ns", "ailab-u-old", {"passwd_hash": ""}), "from-old-pod")


def test_recreate_falls_back_to_request_config_when_no_old_secret():
    v1 = _SecretsOnly(None)
    assert provision.login_password_for_recreate(v1, "ns", "ailab-u-old", {"passwd_hash": HASH}) == HASH


def test_recreate_refuses_when_no_password_anywhere():
    with pytest.raises(provision.LoginPasswordMissing):
        provision.login_password_for_recreate(_SecretsOnly(None), "ns", "ailab-u-old", {"passwd_hash": ""})


def test_account_secret_holds_only_the_hash():
    class _V1:
        def create_namespaced_secret(self, namespace, body):
            self.body = body
    v1 = _V1()
    provision.ensure_account_secret(v1, "ns", "ailab-u-1", "u", HASH)
    assert v1.body.string_data == {"USER_PW_HASH": HASH}
    with pytest.raises(provision.LoginPasswordMissing):
        provision.ensure_account_secret(v1, "ns", "ailab-u-1", "u", "plaintext")
