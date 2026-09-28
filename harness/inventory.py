"""실험 스택 하나에서 측정용 사용자들의 자원을 모으는 읽기 전용 목록.

평가자의 대상 측정과 Environment Resetter 의 잔재 검사가 같은 측정이므로 이 한 곳에서 한다.
자원은 대상 시스템이 신청과 자원을 이어 둔 기록이 아니라 실제 상태에서 모은다 (ADR-004).
쿠버네티스 객체는 라벨과 이름으로, DB 표는 username 칸으로 찾는다.

범위는 <스택 접두어>m 으로 시작하는 사용자다. E2E 도구의 <스택 접두어>e2e 사용자는 모으지 않고
개수만 센다. 한 종류를 조회하지 못하면 그 종류를 errors 에 적고 빈 목록으로 채우지 않는다.
못 본 것을 없는 것으로 적으면 잔재 검사가 깨끗하다고 잘못 판정하기 때문이다.

operation_log 는 측정 원자료라서 잔재로 세지 않는다. 원장·NAS 홈·AD·노드 keytab 은 노드 단위
명령이 생긴 뒤에 더한다.
"""

import system

KEYTAB_SECRET_PREFIX = "krb5-keytab-"

# 이름과 username 라벨만 받는다. -o json 이면 Secret 의 data(비밀번호 해시, DB 비밀번호)까지 딸려 온다.
COLUMNS = ["-o", "custom-columns=NAME:.metadata.name,USER:.metadata.labels.username", "--no-headers"]
# kind -> kubectl get 인자. 각 객체에서 사용자 이름을 꺼내는 방법은 _kube_owner 가 정한다.
KUBE_KINDS = {
    "pod": ["get", "pods", "-l", "app=ailab-guest", *COLUMNS],
    "service": ["get", "services", "-l", "app=ailab-nodeport", *COLUMNS],
    "account_secret": ["get", "secrets", "-l", "app=ailab-account", *COLUMNS],
    "keytab_secret": ["get", "secrets", *COLUMNS],
}

# kind -> (데이터베이스, 문장). 첫 칸이 username, 둘째 칸이 목록에 적을 이름이다.
# ponytail: 표 전체를 읽고 파이썬에서 거른다. 실험 스택 표는 작고, LIKE 이스케이프를 피한다.
SQL_KINDS = {
    "nodeport_allocation": ("pod_port_db", "SELECT username, node_port FROM nodeport_allocations"),
    "krb5_cleanup_pending": ("pod_port_db", "SELECT username, node_name FROM krb5_cleanup_pending"),
}


def _kube_owner(kind, name, label):
    if kind == "keytab_secret":
        return name[len(KEYTAB_SECRET_PREFIX):] if name.startswith(KEYTAB_SECRET_PREFIX) else None
    return None if label == "<none>" else label


def _kube_items(host, namespace, kind):
    """(사용자, 이름) 목록. 조회하지 못하면 오류 메시지 문자열."""
    try:
        row = system.stack_kube(host, namespace, KUBE_KINDS[kind])
    except system.SystemCallFailed as e:
        return str(e)
    if row.get("rc") != 0:
        return f"rc={row.get('rc')}: {row.get('stderr', '')}"
    items = []
    for line in (row.get("stdout") or "").splitlines():
        cols = line.split()
        if not cols:
            continue
        if len(cols) != 2:
            return f"kubectl 출력 줄이 이름과 라벨 두 칸이 아니다: {line[:200]!r}"
        items.append((_kube_owner(kind, *cols), cols[0]))
    return items


def _sql_items(host, namespace, kind):
    database, statement = SQL_KINDS[kind]
    try:
        row = system.stack_sql(host, namespace, database, statement)
    except system.SystemCallFailed as e:
        return str(e)
    if row.get("rc") != 0:
        return f"rc={row.get('rc')}: {row.get('stderr', '')}"
    return [(r[0], str(r[1])) for r in row.get("rows") or []]


def collect_inventory(host, namespace, prefix):
    """{"by_user": {username: {kind: [이름...]}}, "e2e_users": n, "errors": {kind: 메시지}}."""
    scope, e2e = prefix + "m", prefix + "e2e"
    by_user, e2e_seen, errors = {}, set(), {}
    fetches = [(k, _kube_items) for k in KUBE_KINDS] + [(k, _sql_items) for k in SQL_KINDS]
    for kind, fetch in fetches:
        items = fetch(host, namespace, kind)
        if isinstance(items, str):
            errors[kind] = items
            continue
        for user, name in items:
            if not user:
                continue
            if user.startswith(scope):
                by_user.setdefault(user, {}).setdefault(kind, []).append(name)
            elif user.startswith(e2e):
                e2e_seen.add(user)
    return {"by_user": by_user, "e2e_users": len(e2e_seen), "errors": errors}
