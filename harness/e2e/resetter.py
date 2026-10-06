"""한 실행이 만든 것을 전부 지우고, 남은 것이 있으면 목록으로 돌려준다.

실행이 만드는 이름은 모두 `<스택 접두어>e2e<runid>`로 시작한다(계정명·웹 계정 이메일 둘 다). 지우는
범위를 이 접두어로만 좁혀서, 같은 스택의 다른 사용자나 다른 실행은 건드리지 않는다.

순서가 중요하다. 컨테이너와 계정은 제품이 하는 방식(관리자 계정 회수 API)으로 먼저 거둔다 — DB 행을
먼저 지우면 제품이 회수할 근거를 잃어 Pod·원장 행이 고아로 남는다. 홈 디렉터리는 제품이 일부러 남기는
것이라(사람을 가리키는 값) 이 시험 계정들에 한해 따로 지운다.
"""
import time

from .ports import safe

OPEN = ("PENDING", "PROCESSING", "FULFILLED", "MIGRATING", "EXPIRING")


def run_prefix(stack_prefix, run_id):
    return f"{stack_prefix}e2e{safe(run_id)}".lower()


def e2e_group_name(stack_prefix):
    """E2E 가 쓰는 공용 그룹 이름. 스택마다 하나이고 실행마다 다시 쓴다(실행 접두어와는 겹치지 않는다)."""
    return f"{stack_prefix}e2e-team"


def admin_email(run_id):
    return f"e2e{safe(run_id)}-admin@e2e.local".lower()


class Resetter:
    def __init__(self, cluster, api, stack_prefix, run_id, *, wait_timeout=900, interval=10, allow_operation=False):
        if cluster.stack == "operation" and not allow_operation:
            # 실사용자 홈과 계정이 있는 스택에서는 명시하지 않는 한 이름 규칙이 맞아도 지우지 않는다.
            raise ValueError("operation 스택에서는 allow_operation 없이 E2E 정리를 실행하지 않는다")
        self.cluster, self.api = cluster, api
        self.prefix = run_prefix(stack_prefix, run_id)
        self.group_name = e2e_group_name(stack_prefix)
        self.admin_email = admin_email(run_id)
        self.wait_timeout, self.interval = wait_timeout, interval

    # ---- 조회 ----
    def users(self):
        rows = self.cluster.sql(
            f"SELECT user_id, IFNULL(ubuntu_username,''), ubuntu_account_status FROM users "
            f"WHERE ubuntu_username LIKE '{self.prefix}%' OR email = '{self.admin_email}';")
        return [{"id": int(r[0]), "name": r[1], "account": r[2]} for r in rows]

    def open_requests(self):
        rows = self.cluster.sql(
            "SELECT r.request_id, r.status, u.user_id FROM requests r JOIN users u ON u.user_id = r.user_id "
            f"WHERE u.ubuntu_username LIKE '{self.prefix}%' AND r.status IN ({_quoted(OPEN)});")
        return [{"id": int(r[0]), "status": r[1], "user": int(r[2])} for r in rows]

    # ---- 정리 ----
    def reset(self, admin_id):
        """정리 후 남은 것의 목록을 돌려준다. 빈 목록이어야 성공이다."""
        # 작업이 아직 도는 신청만 기다린다. 작업이 끝났는데 PROCESSING에 남은 신청(DEGRADED 등)은 제품에
        # 되돌릴 경로가 없으므로(config-server 몫) 회수 작업을 config-server에 직접 등록해 자원을 거둔다.
        self._wait(lambda: all(self._job_finished(r["id"]) for r in self.open_requests()
                               if r["status"] == "PROCESSING"))
        stuck = {r["id"] for r in self.open_requests() if r["status"] == "PROCESSING"}
        for req in self.open_requests():
            if req["id"] in stuck:
                self._revoke_directly(req)
        for req in self.open_requests():
            if req["status"] == "PENDING":
                self.api.call("POST", f"/api/admin/requests/{req['id']}/rejection", as_user=admin_id,
                              body={"adminComment": "e2e cleanup"})
        for user in self.users():
            if user["account"] != "NONE" or any(r["user"] == user["id"] for r in self.open_requests()):
                self.api.call("DELETE", f"/api/admin/users/{user['id']}/ubuntu-account", as_user=admin_id)
        # 회수 API는 작업만 등록하고 돌아온다. 컨테이너(EXPIRING→DELETED)와 계정(RELEASING→NONE)이 끝나기를 기다린다.
        # 직접 회수한 신청은 admin_be에서 PROCESSING으로 남는다(되돌릴 경로가 없다). 행은 아래에서 지운다.
        self._wait(lambda: all(r["id"] in stuck for r in self.open_requests())
                   and all(u["account"] != "RELEASING" for u in self.users()))

        names = [u["name"] for u in self.users() if u["name"].startswith(self.prefix)]
        if names:
            self._delete_homes(names)
        self._delete_rows()
        return self.residue()

    def residue(self):
        left = []
        left += [f"웹 계정 {u['name'] or u['id']}" for u in self.users()]
        left += [f"작업 기록 {n}행" for (n,) in self.cluster.sql(
            f"SELECT COUNT(*) FROM operation_log WHERE username LIKE '{self.prefix}%' HAVING COUNT(*) > 0;",
            database="operation_state_db")]
        left += [f"포트 배정 {n}" for (n,) in self.cluster.sql(
            f"SELECT username FROM nodeport_allocations WHERE username LIKE '{self.prefix}%';",
            database="pod_port_db")]
        left += [f"keytab 정리 대기 {n}" for (n,) in self.cluster.sql(
            f"SELECT username FROM krb5_cleanup_pending WHERE username LIKE '{self.prefix}%';",
            database="pod_port_db")]
        pods = self.cluster.sh('kubectl -n "$NS" get pods --no-headers -o custom-columns=U:.metadata.labels.username')
        left += [f"Pod {n}" for n in sorted(set(pods.split())) if n.startswith(self.prefix)]
        found = self.cluster.config_server_python(
            "import utils\n"
            f"prefix = {self.prefix!r}\n"
            "for path in ('/kube_share/passwd', '/kube_share/group'):\n"
            "    for line in open(path):\n"
            "        if line.startswith(prefix):\n"
            "            print('원장', path.rsplit('/', 1)[1], line.split(':', 1)[0])\n"
            "with utils._nas_ssh_client() as ssh:\n"
            "    root = utils._user_home_path(prefix).rsplit('/', 1)[0]\n"
            "    _, out, _ = ssh.exec_command('ls -1 ' + root)\n"
            "    for name in out.read().decode().split():\n"
            "        if name.startswith(prefix):\n"
            "            print('홈', name)\n")
        left += [line for line in found.splitlines() if line.strip()]
        return left

    # ---- 내부 ----
    def _last_provision(self, request_id):
        rows = self.cluster.sql(
            f"SELECT phase, IFNULL(node_name,''), username FROM operation_log WHERE request_id='{int(request_id)}' "
            "AND action='PROVISION' AND resource_type IS NULL ORDER BY id DESC LIMIT 1;",
            database="operation_state_db")
        return rows[0] if rows else None

    def _job_finished(self, request_id):
        last = self._last_provision(request_id)
        return bool(last) and last[0] in ("SUCCESS", "FAIL", "UNKNOWN")

    def _revoke_directly(self, req):
        last = self._last_provision(req["id"])
        if not last or not last[2].startswith(self.prefix):
            return
        _, node, username = last
        # 계정만 지우라고 하면 컨테이너가 남아 있어 ACCOUNT_IN_USE로 거절된다. 이 사람의 Pod를 함께 넘긴다.
        pods = self.cluster.sh(f'kubectl -n "$NS" get pods -l username={safe(username)} -o jsonpath="{{.items[*].metadata.name}}"').split()
        self.cluster.config_server_python(
            "import json, os, time, requests\n"
            "headers = {'X-Internal-Token': os.environ.get('CONFIG_API_TOKEN', '')}\n"
            f"body = {{'request_id': '{int(req['id'])}', 'username': {username!r}, 'node_name': {node or None!r}, "
            f"'pod_name': {(pods[0] if pods else None)!r}, 'delete_account': True}}\n"
            "requests.post('http://127.0.0.1:8000/operations/revoke', json=body, headers=headers, timeout=30)\n"
            "for _ in range(60):\n"
            f"    if not any(l.startswith({username + ':'!r}) for l in open('/kube_share/passwd')):\n"
            "        break\n"
            "    time.sleep(5)\n", timeout=420)

    def _delete_homes(self, names):
        for name in names:
            if not name.startswith(self.prefix):
                raise ValueError(f"실행 접두어가 아닌 이름: {name}")
        self.cluster.config_server_python(
            "import utils\n"
            f"for name in {sorted(names)!r}:\n"
            "    utils.delete_user_home_directory(name)\n", timeout=600)

    def _delete_rows(self):
        users = f"SELECT user_id FROM users WHERE ubuntu_username LIKE '{self.prefix}%' OR email = '{self.admin_email}'"
        reqs = f"SELECT request_id FROM requests WHERE user_id IN ({users})"
        # 비밀번호 재설정 신청은 users를 가리킨다. 이 표가 생기기 전의 admin_be가 떠 있는 스택에서도 돌 수 있게
        # 표가 있을 때만 지운다.
        has_resets = self.cluster.sql("SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() "
                                      "AND table_name = 'password_reset_requests';")
        resets = (f"DELETE FROM password_reset_requests WHERE user_id IN (SELECT * FROM ({users}) t) "
                  f"OR reviewed_by IN (SELECT * FROM ({users}) t);\n") if has_resets else ""
        # 그룹 작업은 users·change_request·groups 를 가리킨다. E2E 공용 그룹은 admin_be 기록만 지운다 — 인프라의
        # 그룹(AD·원장·팀 디렉터리)은 지울 수단이 없어 남기고, 다음 실행의 생성 작업이 그대로 이어받는다.
        # 다른 실행의 사용자가 아직 속해 있으면 그 실행이 쓰는 중이므로 그룹 행은 두고 간다.
        has_group_ops = self.cluster.sql("SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() "
                                         "AND table_name = 'group_operations';")
        group_ops = (f"DELETE FROM group_operations WHERE user_id IN (SELECT * FROM ({users}) t) "
                     f"OR requested_by IN (SELECT * FROM ({users}) t);\n") if has_group_ops else ""
        e2e_group = f"SELECT group_id FROM `groups` WHERE group_name = '{safe(self.group_name)}'"
        group = (f"DELETE FROM `groups` WHERE group_name = '{safe(self.group_name)}' "
                 f"AND NOT EXISTS (SELECT 1 FROM user_groups WHERE group_id IN (SELECT * FROM ({e2e_group}) t)) "
                 f"AND NOT EXISTS (SELECT 1 FROM request_groups WHERE group_id IN (SELECT * FROM ({e2e_group}) t)) "
                 f"AND NOT EXISTS (SELECT 1 FROM group_operations WHERE group_id IN (SELECT * FROM ({e2e_group}) t));\n"
                 ) if has_group_ops else ""
        self.cluster.sql(
            "START TRANSACTION;\n"
            f"{resets}"
            f"{group_ops}"
            f"DELETE FROM pod_external_ports WHERE request_id IN (SELECT * FROM ({reqs}) t);\n"
            f"DELETE FROM port_requests WHERE request_id IN (SELECT * FROM ({reqs}) t);\n"
            f"DELETE FROM change_request WHERE request_id IN (SELECT * FROM ({reqs}) t);\n"
            f"DELETE FROM request_groups WHERE request_id IN (SELECT * FROM ({reqs}) t);\n"
            f"DELETE FROM requests WHERE user_id IN (SELECT * FROM ({users}) t);\n"
            f"DELETE FROM user_groups WHERE user_id IN (SELECT * FROM ({users}) t);\n"
            f"DELETE FROM users WHERE user_id IN (SELECT * FROM ({users}) t);\n"
            f"{group}"
            "COMMIT;")
        self.cluster.sql(f"DELETE FROM operation_log WHERE username LIKE '{self.prefix}%';",
                         database="operation_state_db")
        self.cluster.sql(f"DELETE FROM krb5_cleanup_pending WHERE username LIKE '{self.prefix}%';",
                         database="pod_port_db")

    def _wait(self, done):
        deadline = time.monotonic() + self.wait_timeout
        while not done() and time.monotonic() < deadline:
            time.sleep(self.interval)


def _quoted(values):
    return ", ".join(f"'{safe(v)}'" for v in values)
