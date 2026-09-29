"""trial_runner.run_pair 에 꽂는 실스택 포트 묶음.

신청과 승인과 선언 조회와 회수는 실제 스택에 한다. 모든 SQL 과 HTTP 는 system.py 의 server-state
stack 명령을 거친다. 환경 판정은 resetter 가 한다. 수집기는 부르는 쪽이 넘기고(real_collector.RealCollector), 넘기지 않으면
UNKNOWN 을 돌려주는 자리만 둔다. 수집기를 admin_be 나 config-server 의 기록으로 채우면 측정 장치가 대상 시스템의 자기 선언을 정답으로 쓰게
된다 (ADR-004).

시계와 잠자기는 부르는 쪽이 넘긴다. 시험에서 가짜를 꽂기 위해서다.
"""
import base64
import hashlib
import hmac
import json

from pymysql.converters import escape_item

import resetter
import system
from sha512_crypt import sha512_crypt

TOKEN_TTL_SEC = 300
_ALLOWED = (str, int, float, type(None))


class StackSqlError(Exception):
    """원격 mysql 이 0 이 아닌 rc 로 끝났다. 빈 결과와 다르다."""


class MeasureStepFailed(Exception):
    """admin_be 가 2xx 가 아닌 상태를 돌려줬다."""

    def __init__(self, status, body):
        super().__init__(f"admin_be 가 {status} 를 돌려줬다: {body!r}")
        self.status, self.body = status, body


class StackSql:
    """trial.py 가 기대하는 연결 모양을 system.stack_sql 위에 만든다.

    호출마다 새 mysql 세션이고 자동 커밋이므로 commit 은 아무 일도 하지 않는다.
    """

    def __init__(self, host, namespace, database):
        self.host, self.namespace, self.database = host, namespace, database

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        pass

    def query(self, sql, params=()):
        """한 문장을 실행하고 행을 dict 목록으로 돌려준다."""
        with self.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def execute(self, sql, params=()):
        for v in params:
            if type(v) not in _ALLOWED:
                raise TypeError(f"SQL 인자로 받지 않는 타입이다: {type(v).__name__}")
        # pymysql 과 같이 인자가 있을 때만 % 로 채운다.
        if params:
            sql = sql % tuple(escape_item(v, "utf8mb4") for v in params)
        c = self.conn
        row = system.stack_sql(c.host, c.namespace, c.database, sql)
        if row.get("rc") != 0:
            raise StackSqlError(
                f"{c.namespace}/{c.database} SQL 이 rc={row.get('rc')} 로 끝났다: {row.get('stderr', '')}")
        cols = row.get("columns") or []
        self.description = tuple((name, None, None, None, None, None, None) for name in cols)
        self._rows = [tuple(r) for r in row.get("rows") or []]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class BeClient:
    """스택 admin_be 를 사용자 신분으로 부른다. 서명 키는 이 객체 안에만 둔다."""

    def __init__(self, host, namespace, jwt_secret, *, clock):
        self.host, self.namespace = host, namespace
        self._key = jwt_secret.encode()
        self._clock = clock

    def __repr__(self):
        return f"BeClient({self.host!r}, {self.namespace!r})"

    def token(self, user_id):
        now = int(self._clock())
        head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        claims = _b64(json.dumps({"sub": str(user_id), "iat": now, "exp": now + TOKEN_TTL_SEC,
                                  "token_type": "access"}).encode())
        sig = _b64(hmac.new(self._key, f"{head}.{claims}".encode(), hashlib.sha256).digest())
        return f"{head}.{claims}.{sig}"

    def call(self, method, path, *, as_user, body=None):
        row = system.stack_http(self.host, self.namespace, method, path,
                                token=self.token(as_user), body=body)
        return row.get("status"), row.get("body")


def create_user(sql, *, username, email, password, role):
    """web_admin.users 에 한 행을 넣고 user_id 를 돌려준다. 평문 비밀번호는 SQL 에 넣지 않는다."""
    sql.query(
        "INSERT INTO users (created_at, updated_at, department, email, is_active, name, password, phone, role,"
        " student_id, ubuntu_username, ubuntu_account_status, ubuntu_password_hash)"
        " VALUES (NOW(6), NOW(6), 'measure', %s, b'1', 'measure', 'x', '010-0000-0000', %s,"
        " '0000000000', %s, 'NONE', %s)",
        (email, role, username, sha512_crypt(password)))
    rows = sql.query("SELECT user_id FROM users WHERE email = %s", (email,))
    if len(rows) != 1:
        raise StackSqlError(f"{email} 사용자 행이 하나가 아니다: {len(rows)}")
    return int(rows[0]["user_id"])


def stack_defaults(sql):
    """신청 본문에 넣을 (resourceGroupId, imageId). E2E 도구의 prepare 와 같은 질의다."""
    rg = sql.query("SELECT rsgroup_id FROM resource_groups WHERE server_name = 'FARM'"
                   " ORDER BY rsgroup_id LIMIT 1")
    image = sql.query("SELECT image_id FROM container_image ORDER BY image_id LIMIT 1")
    if not rg or not image:
        raise StackSqlError("FARM 자원 그룹이나 컨테이너 이미지가 스택에 없다")
    return int(rg[0]["rsgroup_id"]), int(image[0]["image_id"])


def _ok(status, body):
    if not isinstance(status, int) or not 200 <= status < 300:
        raise MeasureStepFailed(status, body)
    return body


def real_ports(*, be, web_sql, user_id, admin_id, prefix, username, expires_at, sleep, clock, poll_sec,
               collect=None):
    """run_pair 의 포트를 dict 로 돌려준다. 신청 번호는 create_submit 이 정한다.

    expires_at 은 admin_be 가 받는 'YYYY-MM-DDTHH:MM:SS' 문자열이다. 시계를 여기서 읽지 않으려고
    부르는 쪽이 만든다.
    """
    state = {}
    resource_group, image = stack_defaults(web_sql)

    def create_submit():
        body = _ok(*be.call("POST", "/api/requests", as_user=user_id, body={
            "resourceGroupId": resource_group, "imageId": image, "usagePurpose": "measure",
            "formAnswers": {}, "expiresAt": expires_at}))
        request_id = str(int(body["data"]["requestId"]))
        _ok(*be.call("POST", f"/api/admin/requests/{request_id}/approval", as_user=admin_id, body={
            "imageId": image, "resourceGroupId": resource_group, "adminComment": "measure"}))
        state["request_id"] = request_id
        return request_id

    def status_if(expected):
        rows = web_sql.query("SELECT status FROM requests WHERE request_id = %s",
                             (int(state["request_id"]),))
        return expected if rows and rows[0]["status"] == expected else None

    def revoke_submit(request_id):
        _ok(*be.call("DELETE", f"/api/admin/users/{user_id}/ubuntu-account", as_user=admin_id))
        return request_id

    return {
        "create_submit": create_submit,
        "create_declaration": lambda: status_if("FULFILLED"),
        "revoke_submit": revoke_submit,
        "revoke_declaration": lambda: status_if("DELETED"),
        "advance": lambda: sleep(poll_sec),
        "clock": clock,
        "environment": resetter.environment_for(be.host, be.namespace, prefix, username=username),
        "collect": collect or (lambda name, target: ("UNKNOWN", {"reason": "실스택 수집기 미구현"})),
    }
