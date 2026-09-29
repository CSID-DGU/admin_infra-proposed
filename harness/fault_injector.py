"""하네스 쪽 주입 포트 v2. 코드 훅과 외부 상태 변경을 같은 모양(arm·poll·report·disarm)으로 다룬다.

근거는 docs/domains/scenario-alignment.md 의 "주입 포트 v2" 다. trial_runner 가 arm 은 submit 앞,
poll 은 advance 뒤마다, report 는 close_trial 뒤, disarm 은 finally 에서 부른다.

주입기는 시나리오 명세의 run_view 로만 만든다(from_spec). 기대값은 모른다.

report 는 {kind, boundary, step, action, armed_at, fired_at, applied, verified, verify_evidence,
invalid_reason} 이다. 주입을 걸었다는 사실과 그것이 성립했는지의 확인이지 접근 가능성의 판정이
아니다. 평가에 쓰지 않는다 (ADR-004). 저널은 주입이 성립했는지 확인하고 경계 도달을 감지하는 데만
쓴다. 적용하지 못했거나 확인하지 못한 주입은 invalid_reason 에 이유를 적고, trial 은 끝까지 돈다.
invalid injection 으로 세는 일은 Metrics Analyzer 가 한다.

연결은 %s 자리표시자를 받는 DB-API 모양이면 된다. 실스택의 measure_ports.StackSql 과 가상 계층의
sqlite 흉내를 똑같이 받는다.
"""
import time

import system

# 단계 함수 -> 그 단계 본문이 log_operation 에 넘기는 자원 action (config-server/lifecycle_steps/*.py).
# 저널의 자원 행은 단계 이름을 적지 않으므로 이 표로 가린다.
# ponytail: 1차 파동 명세가 쓰는 단계만 둔다. 모르는 단계는 확인하지 못한 주입(invalid)이 된다.
STEP_JOURNAL_ACTIONS = {
    "step_create_krb5_principal": "CREATE_KRB5_PRINCIPAL",
    "step_create_pod_k8s": "CREATE_POD_K8S",
    "step_create_services": "CREATE_SERVICE",
    "step_remove_krb5": "REMOVE_KRB5",
}
# 외부 변경 동작 -> server-state stack mutate 템플릿
TEMPLATES = {"ad_block": "deny-egress-ad", "endpoint_block": "deny-ingress-user"}


def _query(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def _stamp(value):
    return None if value is None else str(value)


def _success_rows(conn, username, step, bound=None):
    """그 사용자의 그 단계 자원 SUCCESS 저널 행 수. bound 는 (연산자, 시각) 이고 비면 시각을 보지 않는다."""
    sql = "SELECT COUNT(*) FROM operation_log WHERE username = %s AND phase = %s AND action = %s"
    args = (username, "SUCCESS", STEP_JOURNAL_ACTIONS[step])
    if bound:
        sql += f" AND created_at {bound[0]} %s"
        args += (bound[1],)
    return _query(conn, sql, args)[0][0]


def _report(inj, **fields):
    return {"kind": inj["kind"], "boundary": inj["boundary"], "step": inj["step"], "action": inj["action"],
            **fields}


class CodeHookInjector:
    """대상 쪽 fault_arming 표에 장전 행을 넣고, 발동 표시와 저널로 성립을 확인한다."""

    def __init__(self, conn, *, injection, username):
        self.conn, self.inj, self.username = conn, injection, username
        self._key = (username, injection["step"], injection["boundary"], injection["action"])
        self._armed = False

    def arm(self):
        if self._armed:
            return
        _query(self.conn, "INSERT INTO fault_arming (username, step_name, boundary, action, occurrence)"
                          " VALUES (%s, %s, %s, %s, %s)", (*self._key, self.inj["occurrence"]))
        self._armed = True

    def poll(self):
        """코드 훅은 대상 시스템 안에서 발동하므로 하네스가 할 일이 없다."""

    def report(self):
        rows = _query(self.conn, "SELECT created_at, fired_at FROM fault_arming WHERE username = %s"
                                 " AND step_name = %s AND boundary = %s AND action = %s ORDER BY id DESC LIMIT 1",
                      self._key)
        armed_at, fired_at = (_stamp(v) for v in (rows[0] if rows else (None, None)))
        applied = fired_at is not None
        verified, evidence, reason = False, None, None
        if not applied:
            reason = "장전했지만 발동 표시(fired_at)가 없다"
        else:
            verified, evidence, reason = self._verify(fired_at)
        return _report(self.inj, armed_at=armed_at, fired_at=fired_at, applied=applied, verified=verified,
                       verify_evidence=evidence, invalid_reason=reason)

    def _verify(self, fired_at):
        """(verified, evidence, invalid_reason). 주입 순간의 사실만 본다: 발동 시각까지 그 단계의 자원
        SUCCESS 행이 있었는가. 발동 뒤의 행(재시도, 관찰기, 이어하기)은 비교군마다 다르므로 보지 않는다 (ADR-000)."""
        method, step = self.inj["verify"], self.inj["step"]
        if method not in ("fired_after_success", "fired_before_success"):
            return False, None, f"확인 방법 {method!r} 를 모른다"
        if step not in STEP_JOURNAL_ACTIONS:
            return False, None, f"{step} 의 저널 action 을 모른다"
        if method == "fired_after_success":
            # 응답 유실은 단계가 SUCCESS 행을 쓴 바로 뒤에 발동하므로 같은 시각도 앞으로 친다.
            n = _success_rows(self.conn, self.username, step, ("<=", fired_at))
            return n > 0, {"success_rows_until_fire": n}, None if n else f"발동 시각까지 {step} 의 SUCCESS 행이 없다"
        # 발동 뒤에 이어하기로 생긴 행이 같은 시각으로 찍혀도 앞으로 치지 않는다.
        n = _success_rows(self.conn, self.username, step, ("<", fired_at))
        return n == 0, {"success_rows_before_fire": n}, None if n == 0 else f"발동 전에 {step} 의 SUCCESS 행이 있다"

    def disarm(self):
        """장전 행을 지운다. 여러 번 불러도 된다."""
        _query(self.conn, "DELETE FROM fault_arming WHERE username = %s AND step_name = %s AND boundary = %s"
                          " AND action = %s", self._key)
        self._armed = False


class ExternalMutationInjector:
    """server-state stack mutate 로 라벨 vasc-run=<run> 이 붙은 NetworkPolicy 를 적용하고 지운다."""

    def __init__(self, host, namespace, *, injection, username, run_id, journal, clock=time.time):
        if injection["action"] not in TEMPLATES:
            raise ValueError(f"외부 변경 동작은 {sorted(TEMPLATES)} 중 하나여야 한다: {injection['action']!r}")
        self.host, self.namespace, self.inj = host, namespace, injection
        self.username, self.run_id, self.journal, self.clock = username, run_id, journal, clock
        self.template = TEMPLATES[injection["action"]]
        self.armed_at = self.fired_at = None
        self.applied = None  # None: 아직 부르지 않음
        self.mutate_error = None

    def _apply(self):
        self.fired_at = self.clock()
        target = self.username if self.template == "deny-ingress-user" else None
        try:
            row = system.stack_mutate(self.host, self.namespace, "apply", template=self.template,
                                      target_user=target, run=self.run_id)
        except system.SystemCallFailed as e:
            self.applied, self.mutate_error = False, str(e)
            return
        self.applied = row.get("rc") == 0
        if not self.applied:
            self.mutate_error = f"rc={row.get('rc')} {row.get('stderr', '')}".strip()

    def arm(self):
        if self.armed_at is not None:
            return
        self.armed_at = self.clock()
        if self.inj["action"] == "ad_block":  # X0: 제출 앞
            self._apply()

    def poll(self):
        """endpoint_block: 그 사용자의 Service 자원 SUCCESS 행이 보이면 한 번만 적용한다 (X6 뒤).
        저널 행은 적용 계기로만 쓴다."""
        if self.inj["action"] != "endpoint_block" or self.applied is not None:
            return
        if _success_rows(self.journal, self.username, "step_create_services"):
            self._apply()

    def report(self):
        verified, evidence, reason = False, None, None
        if self.applied is None:
            reason = "경계에 도달하지 않아 적용하지 않았다"
        elif not self.applied:
            reason = f"stack mutate 가 실패했다: {self.mutate_error}"
        else:
            name = f"vasc-{self.template}-{self.run_id}"
            try:
                row = system.stack_kube(self.host, self.namespace,
                                        ["get", "networkpolicy", "-l", f"vasc-run={self.run_id}", "-o", "name"])
                evidence = {"rc": row.get("rc"), "stdout": row.get("stdout", "")}
                verified = row.get("rc") == 0 and name in row.get("stdout", "")
            except system.SystemCallFailed as e:
                evidence = {"error": str(e)}
            if not verified:
                reason = f"NetworkPolicy {name} 를 확인하지 못했다"
        return _report(self.inj, armed_at=self.armed_at, fired_at=self.fired_at, applied=bool(self.applied),
                       verified=verified, verify_evidence=evidence, invalid_reason=reason)

    def disarm(self):
        """이 run 의 정책을 지운다. 여러 번 불러도 된다. 지우지 못하면 올린다: 남은 정책은 다음 trial 을 오염시킨다."""
        if self.armed_at is None:
            return
        row = system.stack_mutate(self.host, self.namespace, "clear", run=self.run_id)
        if row.get("rc") != 0:
            raise system.SystemCallFailed(
                f"NetworkPolicy vasc-run={self.run_id} 를 지우지 못했다: {row.get('stderr', '')}."
                f" 확인: server-state stack kube --namespace {self.namespace} -- get networkpolicy -l vasc-run={self.run_id}")
        self.armed_at = None


def from_spec(view, *, username, journal, host=None, namespace=None, run_id=None):
    """명세의 run_view 로 주입기를 만든다. 주입이 없는 명세면 None. 주입기를 만드는 유일한 자리다."""
    inj = view["injection"]
    if inj["kind"] == "none":
        return None
    if inj["kind"] == "code_hook":
        return CodeHookInjector(journal, injection=inj, username=username)
    if inj["kind"] == "external_mutation":
        return ExternalMutationInjector(host, namespace, injection=inj, username=username, run_id=run_id,
                                        journal=journal)
    raise ValueError(f"주입 kind 를 모른다: {inj['kind']!r}")
