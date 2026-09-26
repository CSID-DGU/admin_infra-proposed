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
    rows = cluster.sql(f"SELECT IFNULL(ubuntu_uid,''), IFNULL(ubuntu_gid,''), ubuntu_account_active+0 "
                       f"FROM users WHERE user_id={int(user_id)};")
    if not rows:
        return None
    uid, gid, active = rows[0]
    return {"uid": int(uid) if uid else None, "gid": int(gid) if gid else None, "active": active == "1"}


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
