"""스택의 실제 상태를 읽기만 하는 조회 모음. 판정은 runner가 한다."""
import time

from .ports import safe


def request_row(cluster, request_id):
    rows = cluster.sql(f"SELECT status, IFNULL(node_name,''), IFNULL(pod_name,'') FROM requests "
                       f"WHERE request_id={int(request_id)};")
    if not rows:
        return None
    status, node, pod = rows[0]
    return {"status": status, "node": node or None, "pod": pod or None}


def user_row(cluster, user_id):
    rows = cluster.sql(f"SELECT IFNULL(ubuntu_uid,''), IFNULL(ubuntu_gid,''), ubuntu_account_status "
                       f"FROM users WHERE user_id={int(user_id)};")
    if not rows:
        return None
    uid, gid, account = rows[0]
    return {"uid": int(uid) if uid else None, "gid": int(gid) if gid else None, "account": account}


def password_reset_status(cluster, user_id):
    """이 사용자의 가장 최근 비밀번호 재설정 신청 상태(PENDING·PROCESSING·APPLIED·DENIED). 없으면 None."""
    rows = cluster.sql(f"SELECT status FROM password_reset_requests WHERE user_id={int(user_id)} "
                       "ORDER BY password_reset_request_id DESC LIMIT 1;")
    return rows[0][0] if rows else None


def recorded_password_hash(cluster, user_id):
    """admin_be가 이 사용자의 SSH 비밀번호로 기록해 둔 해시. 비교에만 쓰고 어디에도 남기지 않는다."""
    rows = cluster.sql(f"SELECT IFNULL(ubuntu_password_hash,'') FROM users WHERE user_id={int(user_id)};")
    return rows[0][0] if rows and rows[0][0] else None


def pod_password_hash(cluster, pod_name, username):
    """컨테이너 안 /etc/shadow에 들어 있는 로그인 비밀번호 해시. 비교에만 쓰고 어디에도 남기지 않는다."""
    out = cluster.sh(f'kubectl -n "$NS" exec {safe(pod_name)} -- getent shadow {safe(username)} | cut -d: -f2')
    return out.strip() or None


# 홈 폴더(NAS) 밖, 컨테이너 자체에 쓰는 표식. 재시작 뒤 컨테이너 변경분이 이어졌는지 본다.
POD_MARKER = "/opt/ailab-e2e-marker"


def pod_write_marker(cluster, pod_name):
    cluster.sh(f'kubectl -n "$NS" exec {safe(pod_name)} -- touch {POD_MARKER}')


def pod_has_marker(cluster, pod_name):
    out = cluster.sh(f'kubectl -n "$NS" exec {safe(pod_name)} -- sh -c "test -e {POD_MARKER} && echo yes || echo no"')
    return out.strip() == "yes"


def oplog_codes(cluster, request_id):
    """이 신청 번호로 남은 작업 기록의 오류 코드 집합."""
    rows = cluster.sql(f"SELECT DISTINCT error_code FROM operation_log WHERE request_id='{int(request_id)}' "
                       f"AND error_code IS NOT NULL;", database="operation_state_db")
    return {r[0] for r in rows}


def pod_exists(cluster, pod_name):
    if not pod_name:
        return False
    out = cluster.sh(f'kubectl -n "$NS" get pod {safe(pod_name)} --ignore-not-found -o name')
    return bool(out.strip())


def job_ended_but_stuck(cluster, request_id, row):
    """작업은 끝났는데(DEGRADED 등) 신청이 PROCESSING에 남아 더 기다려도 바뀌지 않는 상태인가.
    admin_be 폴러가 결과를 반영할 틈(1분)은 준다."""
    if not row or row["status"] != "PROCESSING":
        return False
    # 가장 최근 작업 행만 본다 — 재승인 직후에는 앞선 작업의 FAIL 행이 남아 있다.
    rows = cluster.sql(
        f"SELECT phase, created_at < NOW(3) - INTERVAL 60 SECOND FROM operation_log WHERE request_id='{int(request_id)}' "
        "AND action='PROVISION' AND resource_type IS NULL ORDER BY id DESC LIMIT 1;",
        database="operation_state_db")
    return bool(rows) and rows[0][0] in ("FAIL", "UNKNOWN") and rows[0][1] == "1"


def job_outcome(cluster, request_id):
    """이 신청의 가장 최근 생성 작업 결과. 아직 도는 중이면 RUNNING, 없으면 None.
    재시도를 다 써 관리자에게 넘긴 작업은 끝 행의 오류 코드가 DEGRADED다."""
    rows = cluster.sql(
        f"SELECT phase, IFNULL(error_code,'') FROM operation_log WHERE request_id='{int(request_id)}' "
        "AND action='PROVISION' AND resource_type IS NULL ORDER BY id DESC LIMIT 1;",
        database="operation_state_db")
    if not rows:
        return None
    phase, code = rows[0]
    if phase == "START":
        return "RUNNING"
    return "DEGRADED" if code == "DEGRADED" else phase


def wait_until(check, timeout, interval):
    """check()가 참이 될 때까지 기다린다. 마지막 결과를 돌려준다."""
    deadline = time.monotonic() + timeout
    result = check()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = check()
    return result


def wait_status(cluster, request_id, wanted, timeout=900, interval=10):
    """상태가 wanted 중 하나가 될 때까지 기다린다. 돌려주는 값은 마지막으로 본 행이다.
    작업이 끝났는데 신청이 멈춰 있으면 제한 시간까지 기다리지 않고 바로 돌려준다."""
    deadline = time.monotonic() + timeout
    row = request_row(cluster, request_id)
    while not (row and row["status"] in wanted) and time.monotonic() < deadline:
        if "PROCESSING" not in wanted and job_ended_but_stuck(cluster, request_id, row):
            break
        time.sleep(interval)
        row = request_row(cluster, request_id)
    return row


def group_operation(cluster, operation_id):
    """그룹 작업의 상태(PROCESSING·APPLIED·FAILED)와 오류 코드."""
    rows = cluster.sql(f"SELECT status, IFNULL(error_code,'') FROM group_operations "
                       f"WHERE group_operation_id={int(operation_id)};")
    return {"status": rows[0][0], "error": rows[0][1] or None} if rows else None


def group_row(cluster, group_name):
    rows = cluster.sql(f"SELECT group_id, ubuntu_gid FROM `groups` WHERE group_name='{safe(group_name)}';")
    return {"id": int(rows[0][0]), "gid": int(rows[0][1])} if rows else None


def change_request_status(cluster, change_request_id):
    rows = cluster.sql(f"SELECT status FROM change_request WHERE change_request_id={int(change_request_id)};")
    return rows[0][0] if rows else None


def change_request_codes(cluster, change_request_id):
    """이 변경 요청을 반영하려던 그룹 작업들이 남긴 오류 코드 집합."""
    ops = cluster.sql(f"SELECT group_operation_id FROM group_operations "
                      f"WHERE change_request_id={int(change_request_id)};")
    keys = ", ".join(f"'group-op-{int(op[0])}'" for op in ops)
    if not keys:
        return set()
    rows = cluster.sql(f"SELECT DISTINCT error_code FROM operation_log WHERE request_id IN ({keys}) "
                       "AND error_code IS NOT NULL;", database="operation_state_db")
    return {r[0] for r in rows}


def group_membership(cluster, user_id, username, group_name, pods):
    """한 사용자가 그룹에 들어 있는지를 세 곳에서 본다: admin_be 기록, 계정 원장, 떠 있는 컨테이너."""
    recorded = bool(cluster.sql(
        "SELECT 1 FROM user_groups ug JOIN `groups` g ON g.group_id = ug.group_id "
        f"WHERE ug.user_id={int(user_id)} AND g.group_name='{safe(group_name)}';"))
    ledger = cluster.config_server_python(
        f"name, user = {group_name!r}, {username!r}\n"
        "for line in open('/kube_share/group'):\n"
        "    parts = line.rstrip('\\n').split(':')\n"
        "    if parts[0] == name:\n"
        "        print('MEMBER' if user in parts[3].split(',') else 'ABSENT')\n")
    in_pods = []
    for pod in pods:
        out = cluster.sh(f'kubectl -n "$NS" exec {safe(pod)} -- getent group {safe(group_name)} || true')
        in_pods.append(username in out.strip().split(":")[-1].split(","))
    return {"recorded": recorded, "ledger": "MEMBER" in ledger, "pods": in_pods}
