"""E2E 도구가 바깥과 닿는 자리 전부: 클러스터(SSH+kubectl), 스택 DB, admin_be API.

다른 모듈은 이 클래스들만 부른다. 시험에서는 같은 메서드를 가진 가짜로 바꿔 끼운다.

공개 저장소라 이 모듈은 인프라 주소·계정·비밀값을 출력하지 않는다. 접속 정보는 저장소 밖의 파일
(~/.ailab-exp/e2e.env, E2E_ENV로 경로 변경)에서 읽고, 비밀값은 명령 문자열에 끼우지 않고
원격 셸 안에서 Secret을 읽어 바로 쓴다.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request

# 이 도구가 SQL·셸에 끼워 넣는 값은 전부 스스로 만든 이름·번호다. 그래도 형식을 좁혀 둬야
# 실수로 바깥 값이 들어왔을 때 문법을 깨지 못한다.
_SAFE = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")


def safe(value) -> str:
    text = str(value)
    if not _SAFE.match(text):
        raise ValueError(f"안전하지 않은 값: {text!r}")
    return text


def load_env(path=None) -> dict:
    path = path or os.environ.get("E2E_ENV") or os.path.expanduser("~/.ailab-exp/e2e.env")
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"')
    return env


class Cluster:
    """스택 네임스페이스 하나에 대한 kubectl·DB 조작. 원격 셸은 표준입력으로 받는다."""

    def __init__(self, env: dict, stack: str):
        self.stack = safe(stack)
        self.ns = f"ailab-{self.stack}"
        self._ssh = ["ssh", "-i", os.path.expanduser(env["SSH_KEY"]), "-o", "BatchMode=yes",
                     "-o", "IdentitiesOnly=yes", "-p", env.get("SSH_PORT", "22"), env["SSH_TARGET"]]
        self._kubeconfig = env.get("KUBECONFIG", "")

    def sh(self, script: str, timeout=300) -> str:
        """원격에서 bash 스크립트를 돌리고 표준출력을 돌려준다. 실패하면 표준오류와 함께 예외."""
        prelude = f"set -euo pipefail\nexport KUBECONFIG={self._kubeconfig}\nNS={self.ns}\n" if self._kubeconfig \
            else f"set -euo pipefail\nNS={self.ns}\n"
        proc = subprocess.run(self._ssh + ["bash -s"], input=prelude + script, capture_output=True,
                              text=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"원격 명령 실패({proc.returncode}): {proc.stderr.strip()[-500:]}")
        return proc.stdout

    def sql(self, statement: str, database="web_admin") -> list:
        """스택 MySQL에서 SQL을 실행하고 행 목록(탭 구분 문자열 튜플)을 돌려준다."""
        script = (
            'PW=$(kubectl -n "$NS" get secret stack-db -o jsonpath="{.data.root}" | base64 -d)\n'
            f'kubectl -n "$NS" exec -i mysql-0 -- mysql -uroot -p"$PW" -N -B {safe(database)} 2>/dev/null <<"SQLEOF"\n'
            f"{statement}\nSQLEOF\n")
        out = self.sh(script)
        return [tuple(line.split("\t")) for line in out.splitlines() if line]

    def secret_value(self, key: str) -> str:
        return self.sh(f'kubectl -n "$NS" get secret stack-db -o jsonpath="{{.data.{safe(key)}}}" | base64 -d')

    def config_server_python(self, code: str, timeout=300) -> str:
        """config-server Pod 안에서 앱 컨텍스트를 연 채 파이썬을 돌린다(NAS·원장 접근용)."""
        body = "import main as _m\nwith _m.app.app_context():\n" + "\n".join("    " + l for l in code.splitlines())
        script = (f'POD=$(kubectl -n "$NS" get pods -l app=containerssh-config-server '
                  f'--field-selector=status.phase=Running -o jsonpath="{{.items[0].metadata.name}}")\n'
                  f'kubectl -n "$NS" exec -i "$POD" -- python - <<"PYEOF"\n{body}\nPYEOF\n')
        return self.sh(script, timeout=timeout)


class BeApi:
    """스택 admin_be를 사용자·관리자 신분으로 부른다. JWT는 스택의 서명 키로 직접 만든다."""

    def __init__(self, base_url: str, jwt_secret: str, *, ca_file=None, insecure_tls=False):
        self.base = base_url.rstrip("/")
        self._key = jwt_secret.encode()
        # 기본은 인증서를 검증한다. 스택 입구는 자체 서명 인증서라, 그 인증서 자체를 CA_FILE로 고정해 믿는다
        # (다른 인증서를 내미는 중간자는 여기서 막힌다). 그 인증서는 이름 대신 IP로 발급돼 nip.io 호스트
        # 이름과 맞지 않으므로 고정한 경우에만 호스트 이름 비교를 끈다. 검증 자체를 끄는 것은
        # INSECURE_TLS=1을 명시한 경우뿐이고, 그래도 토큰 수명은 5분으로 짧게 둔다.
        self._ctx = ssl.create_default_context(cafile=os.path.expanduser(ca_file)) if ca_file \
            else ssl.create_default_context()
        if ca_file:
            self._ctx.check_hostname = False
        if insecure_tls:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def _token(self, user_id: int) -> str:
        def b64(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        now = int(time.time())
        head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        claims = b64(json.dumps({"sub": str(user_id), "iat": now, "exp": now + 300}).encode())
        sig = b64(hmac.new(self._key, f"{head}.{claims}".encode(), hashlib.sha256).digest())
        return f"{head}.{claims}.{sig}"

    def call(self, method: str, path: str, *, as_user: int, body=None) -> tuple:
        """(상태 코드, 응답 JSON) — 오류 응답도 예외 없이 돌려준다. 판정은 부르는 쪽이 한다.
        승인·회수는 작업만 등록하고 202로 돌아오므로 응답 대기는 짧게 둔다. 결과는 wait 단계가 DB로 본다."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token(as_user), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ctx) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw) if raw else {}
            except ValueError:
                return e.code, {"message": raw[:200].decode(errors="replace")}
