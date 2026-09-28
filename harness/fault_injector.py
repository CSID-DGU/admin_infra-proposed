"""하네스 쪽 Fault Injector. trial 앞에서 장전하고, trial 뒤에 발동 여부를 읽고 장전을 푼다.

장전은 대상 시스템의 operation_state_db 에 있는 fault_arming 표에 행 하나를 넣는 일이다. 대상 시스템
쪽 훅(config-server/adapters/fault_injection.py)이 FAULT_INJECTION=1 일 때만 그 행을 읽고, 장애를
일으키기 전에 fired_at 을 먼저 커밋한다. 하네스는 fired_at 을 읽어서 기록의 fault 칸에 옮기기만 한다.

fault 칸은 장애를 걸었다는 사실이지 접근 가능성의 판정이 아니다. 평가에 쓰지 않는다 (ADR-004).
장전했는데 발동하지 않은 trial 을 어떻게 셀지는 Metrics Analyzer 가 정한다.

연결은 %s 자리표시자를 받는 DB-API 모양이면 된다. 실스택의 measure_ports.StackSql 과 가상 계층의
sqlite 흉내를 똑같이 받는다.
"""

# 시나리오 -> (그 장애를 거는 trial 의 operation, config-server application/jobs.py 의 단계 함수 이름)
SCENARIOS = {
    "C06": ("CREATE", "step_create_krb5_principal"),
    "C08": ("CREATE", "step_create_krb5_principal"),
    "C12": ("REVOKE", "step_remove_krb5"),
}

_WHERE = " WHERE username = %s AND step_name = %s AND scenario = %s"


class Fault:
    def __init__(self, conn, *, scenario, username):
        if scenario not in SCENARIOS:
            raise ValueError(f"scenario 는 {sorted(SCENARIOS)} 중 하나여야 한다: {scenario!r}")
        self.conn = conn
        self.scenario = scenario
        self.username = username
        self.operation, self.step_name = SCENARIOS[scenario]

    def _execute(self, sql, fetch=False):
        with self.conn.cursor() as cur:
            cur.execute(sql, (self.username, self.step_name, self.scenario))
            row = cur.fetchone() if fetch else None
        self.conn.commit()
        return row

    def arm(self):
        self._execute("INSERT INTO fault_arming (username, step_name, scenario) VALUES (%s, %s, %s)")

    def report(self):
        """장전 행을 읽어 {"scenario", "step_name", "armed_at", "fired_at"} 로 돌려주고 행을 지운다.

        행이 없으면 armed_at 도 None 이다. 시각은 JSON 으로 옮길 수 있게 문자열로 둔다.
        """
        row = self._execute("SELECT created_at, fired_at FROM fault_arming" + _WHERE
                            + " ORDER BY id DESC LIMIT 1", fetch=True)
        armed_at, fired_at = (None if v is None else str(v) for v in (row or (None, None)))
        self.disarm()
        return {"scenario": self.scenario, "step_name": self.step_name,
                "armed_at": armed_at, "fired_at": fired_at}

    def disarm(self):
        """장전 행을 지운다. 여러 번 불러도 된다."""
        self._execute("DELETE FROM fault_arming" + _WHERE)
