"""sha512_crypt 가 명세와 glibc crypt 와 같은 값을 내는지 본다."""
import re
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sha512_crypt import sha512_crypt  # noqa: E402

_CRYPT_HASH = re.compile(r"^\$6\$[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}$")


def test_spec_vector():
    assert sha512_crypt("Hello world!", "saltstring") == (
        "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJuesI68u4OTLiBFdcbYEdFCoEOfaS35inz1")


def test_matches_system_crypt():
    """게이트의 Linux 컨테이너에서만 돈다. macOS crypt 는 $6$ 를 만들지 못한다."""
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import crypt
    except ImportError:
        pytest.skip("crypt 모듈 없음")
    if not (crypt.crypt("x", "$6$ab") or "").startswith("$6$ab$"):
        pytest.skip("이 환경의 crypt 는 SHA-512 를 지원하지 않는다")
    for _ in range(20):
        pw = secrets.token_urlsafe(secrets.randbelow(40) + 1)
        salt = sha512_crypt("", None).split("$")[2][:secrets.randbelow(16) + 1]
        assert sha512_crypt(pw, salt) == crypt.crypt(pw, "$6$" + salt)


def test_random_salt_shape_and_uniqueness():
    a, b = sha512_crypt("pw"), sha512_crypt("pw")
    assert _CRYPT_HASH.match(a) and _CRYPT_HASH.match(b)
    assert a.split("$")[2] != b.split("$")[2]
