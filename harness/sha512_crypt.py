"""SHA-512 crypt($6$) 해시를 표준 라이브러리로 직접 계산한다.

config-server 는 로그인 비밀번호를 SHA-512 crypt 해시로만 받는다. 실행 도구는 Mac 에서
도는데 macOS 의 crypt 는 SHA-512 를 만들지 못하고, crypt 모듈은 3.13 에서 사라졌으며,
passlib 같은 외부 패키지는 들이지 않기로 했다. 그래서 Ulrich Drepper 의 SHA-crypt 명세
(https://www.akkadia.org/drepper/SHA-crypt.txt) 를 그대로 옮긴다. rounds 는 기본값 5000
이고 해시에 rounds= 를 적지 않는다.
"""
import hashlib
import secrets

ROUNDS = 5000
_B64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
# 명세의 출력 바이트 배열 순서. 세 바이트씩 묶어 네 글자로 적고 마지막 한 바이트는 두 글자.
_ORDER = [(0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26),
          (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32),
          (12, 33, 54), (34, 55, 13), (56, 14, 35), (15, 36, 57), (37, 58, 16), (59, 17, 38),
          (18, 39, 60), (40, 61, 19), (62, 20, 41)]


def _b64(value, n):
    out = ""
    for _ in range(n):
        out += _B64[value & 0x3F]
        value >>= 6
    return out


def _repeat(digest, length):
    return digest * (length // 64) + digest[:length % 64]


def sha512_crypt(password, salt=None):
    if salt is None:
        salt = "".join(secrets.choice(_B64) for _ in range(16))
    p = password.encode()
    s = salt.encode()[:16]

    b = hashlib.sha512(p + s + p).digest()
    a = hashlib.sha512(p + s + _repeat(b, len(p)))
    n = len(p)
    while n:
        a.update(b if n & 1 else p)
        n >>= 1
    a = a.digest()

    dp = _repeat(hashlib.sha512(p * len(p)).digest(), len(p))
    ds = _repeat(hashlib.sha512(s * (16 + a[0])).digest(), len(s))

    c = a
    for i in range(ROUNDS):
        h = hashlib.sha512(dp if i & 1 else c)
        if i % 3:
            h.update(ds)
        if i % 7:
            h.update(dp)
        h.update(c if i & 1 else dp)
        c = h.digest()

    out = "".join(_b64((c[x] << 16) | (c[y] << 8) | c[z], 4) for x, y, z in _ORDER)
    out += _b64(c[63], 2)
    return f"$6${s.decode()}${out}"
