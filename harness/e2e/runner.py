"""catalog.yaml의 사례를 실제 스택에서 한 단계씩 실행하고 판정한다.

사례마다 사용자를 새로 만든다(`<스택 접두어>e2e<runid><사례번호><별칭>`). 스택에 남아 있는 옛 계정은
과거 시험의 NAS 홈·uid 잔재를 물고 있을 수 있어 결과를 믿을 수 없기 때문이다.
"""
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import time

from . import observe
from .catalog import is_fault_case
from .ports import safe
from .resetter import admin_email, e2e_group_name, run_prefix

# admin_be는 사용 목적을 50자 이상 받는다(be#633).
PURPOSE_FILLER = "자동 점검용 시험 신청입니다. 컨테이너 생성과 회수 흐름이 정상인지 확인합니다."

_CRYPT_HASH = re.compile(r"^\$6\$[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}$")


def random_ssh_password_hash() -> str:
    """사용자마다 버리는 무작위 비밀번호의 SHA-512 crypt 해시. 시험은 비밀번호로 접속하지 않지만 실제 스택에
    SSH로 닿는 컨테이너가 생기므로, 알려진 값을 쓰면 시험하는 동안 누구나 들어올 수 있다. 평문은 표준입력으로만
    넘기고 어디에도 남기지 않는다."""
    proc = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=secrets.token_urlsafe(24),
                          capture_output=True, text=True, timeout=10, check=True)
    digest = proc.stdout.strip()
    if not _CRYPT_HASH.match(digest):
        raise RuntimeError("openssl이 SHA-512 crypt 해시를 만들지 못함")
    return digest


class StepFailed(AssertionError):
    pass


class Run:
    def __init__(self, cluster, api, faults, *, stack_prefix, run_id, wait_timeout=900, interval=10,
                 progress=lambda line: None):
        self.cluster, self.api, self.faults = cluster, api, faults
        # 한 사례가 수 분씩 걸려 끝날 때만 알리면 멈춘 것과 구분이 안 된다. 단계마다 한 줄씩 알린다.
        self.progress = progress
        self.prefix = run_prefix(stack_prefix, run_id)
        # 공용 그룹은 AD 에서 지울 수단이 없다. 실행마다 새로 만들면 AD 에 계속 쌓이므로 스택마다 하나를 두고
        # 다시 쓴다 — 정리 때 admin_be 기록만 지우면, 다음 실행의 생성 작업이 남은 그룹을 그대로 이어받는다.
        self.group_name = e2e_group_name(stack_prefix)
        self.run_id = run_id
        self.wait_timeout, self.interval = wait_timeout, interval
        self.admin_id = None
        self.resource_group = None
        self.image = None

    # ---- 준비 ----
    def prepare(self):
        self.admin_id = self._insert_user(email=admin_email(self.run_id), username=None, role="ADMIN")
        rows = self.cluster.sql("SELECT rsgroup_id FROM resource_groups WHERE server_name='FARM' ORDER BY rsgroup_id LIMIT 1;")
        self.resource_group = int(rows[0][0])
        rows = self.cluster.sql("SELECT image_id FROM container_image ORDER BY image_id LIMIT 1;")
        self.image = int(rows[0][0])

    def farm_nodes(self):
        out = self.cluster.config_server_python(
            "import json, os\n"
            "print(json.dumps([n['name'] for n in json.loads(os.environ.get('FARM_NODES_JSON', '[]'))]))")
        return json.loads(out.strip().splitlines()[-1])

    def group_nodes(self):
        """신청에 쓰는 리소스 그룹에 속한 farm 노드. 다른 그룹 노드에는 신청한 GPU가 없어 옮길 수 없다."""
        rows = self.cluster.sql(f"SELECT node_id FROM nodes WHERE rsgroup_id={int(self.resource_group)};")
        in_group = {r[0].lower() for r in rows}
        return [n for n in self.farm_nodes() if n.lower() in in_group]

    def run_case(self, case, *, allow_faults):
        if is_fault_case(case) and not allow_faults:
            return {"id": case["id"], "result": "SKIP", "reason": "장애 사례 (--allow-faults 없음)"}
        if self.cluster.stack in case.get("not_on", []):
            return {"id": case["id"], "result": "SKIP", "reason": f"{self.cluster.stack} 스택에서는 돌리지 않는 사례"}
        ctx = Context(self, case["id"])
        started = time.monotonic()
        try:
            total = len(case["steps"])
            for i, step in enumerate(case["steps"], 1):
                verb, arg = next(iter(step.items()))
                ctx.step_no = i
                self.progress(f"    {case['id']} [{i}/{total}] {round(time.monotonic() - started)}s {verb} {arg}")
                getattr(ctx, "do_" + verb)(arg)
            result = {"result": "PASS"}
        except Exception as e:  # 한 사례의 실패가 다른 사례 실행을 막지 않는다.
            result = {"result": "FAIL", "step": ctx.step_no, "error": f"{type(e).__name__}: {e}"}
        finally:
            try:
                self.faults.heal_all()
            except Exception as e:
                result = {"result": "FAIL", "step": "heal", "error": f"장애 복구 실패: {e}"}
        return {"id": case["id"], "title": case.get("title", ""), "seconds": round(time.monotonic() - started),
                "observed": ctx.observed, **result}

    # ---- 사용자 만들기 ----
    def _insert_user(self, *, email, username, role="USER"):
        name = f"'{safe(username)}'" if username else "NULL"
        # SSH 비밀번호는 웹 계정 비밀번호 하나이고, admin_be는 가입·로그인 때 그 해시를 만든다. 여기서는 가입을
        # 건너뛰고 행을 직접 넣으므로 해시도 직접 넣는다(형식은 random_ssh_password_hash가 확인한다).
        self.cluster.sql(
            "INSERT INTO users (created_at, updated_at, department, email, is_active, name, password, phone, role, "
            "student_id, ubuntu_username, ubuntu_account_status, ubuntu_password_hash) VALUES (NOW(6), NOW(6), 'e2e', "
            f"'{safe(email)}', b'1', 'e2e', 'x', '010-0000-0000', '{safe(role)}', '0000000000', {name}, 'NONE', "
            f"'{random_ssh_password_hash()}');")
        return int(self.cluster.sql(f"SELECT user_id FROM users WHERE email='{safe(email)}';")[0][0])


class Context:
    """사례 하나의 실행 상태: 별칭 → 사용자/신청 번호, 기억해 둔 값, 관측 기록."""

    def __init__(self, run, case_id):
        self.run = run
        self.case = case_id.lower()
        self.users, self.requests, self.memo = {}, {}, {}
        self.observed = []
        self.step_no = 0

    # ---- 도움 ----
    def _call(self, method, path, *, as_user, body=None, expect="ok"):
        status, payload = self.run.api.call(method, path, as_user=as_user, body=body)
        ok = 200 <= status < 300
        self.observed.append({"step": self.step_no, "call": f"{method} {path}", "status": status})
        if expect == "ok" and not ok:
            raise StepFailed(f"{method} {path} → {status} {payload.get('message', '')}")
        if expect == "error" and ok:
            raise StepFailed(f"{method} {path}가 실패해야 하는데 {status}")
        return payload

    @staticmethod
    def _target(arg, key):
        """단계 값은 대상 하나('r1') 또는 {key: 대상, expect: ok|error} 형태다."""
        if isinstance(arg, dict):
            return arg[key], arg.get("expect", "ok")
        return arg, "ok"

    def _req(self, alias):
        return self.requests[alias]

    def _owner(self, alias):
        return self.memo[f"{alias}.owner"]

    # ---- 단계 ----
    def do_user(self, alias):
        name = f"{self.run.prefix}{self.case}{safe(alias)}".lower()
        uid = self.run._insert_user(email=f"{name}@example.com", username=name)
        self.users[alias] = {"id": uid, "name": name}

    def do_apply(self, arg):
        user = self.users[arg["user"]]
        expires = (dt.datetime.now() + dt.timedelta(days=arg.get("days", 3))).strftime("%Y-%m-%dT%H:%M:%S")
        body = {"resourceGroupId": self.run.resource_group, "imageId": self.run.image,
                "usagePurpose": f"e2e {self.case} {PURPOSE_FILLER}", "formAnswers": {}, "expiresAt": expires}
        payload = self._call("POST", "/api/requests", as_user=user["id"], body=body)
        self.requests[arg["as"]] = int(payload["data"]["requestId"])
        self.memo[f"{arg['as']}.owner"] = user["id"]

    def do_approve(self, arg):
        alias, expect = self._target(arg, "req")
        self._call("POST", f"/api/admin/requests/{self._req(alias)}/approval", as_user=self.run.admin_id,
                   body={"imageId": self.run.image, "resourceGroupId": self.run.resource_group, "adminComment": "e2e"},
                   expect=expect)

    def do_reject(self, arg):
        alias, expect = self._target(arg, "req")
        self._call("POST", f"/api/admin/requests/{self._req(alias)}/rejection", as_user=self.run.admin_id,
                   body={"adminComment": "e2e"}, expect=expect)

    def do_cancel(self, arg):
        alias, expect = self._target(arg, "req")
        self._call("DELETE", f"/api/requests/{self._req(alias)}", as_user=self._owner(alias), expect=expect)

    def do_reclaim_container(self, arg):
        alias, expect = self._target(arg, "req")
        self._call("DELETE", f"/api/admin/requests/{self._req(alias)}/container", as_user=self.run.admin_id,
                   expect=expect)

    def do_reclaim_account(self, arg):
        alias, expect = self._target(arg, "user")
        self._call("DELETE", f"/api/admin/users/{self.users[alias]['id']}/ubuntu-account",
                   as_user=self.run.admin_id, expect=expect)

    def do_deactivate(self, arg):
        alias, expect = self._target(arg, "user")
        self._call("PATCH", f"/api/admin/users/{self.users[alias]['id']}", as_user=self.run.admin_id,
                   body={"active": False}, expect=expect)

    def do_reactivate(self, arg):
        alias, expect = self._target(arg, "user")
        self._call("PATCH", f"/api/admin/users/{self.users[alias]['id']}", as_user=self.run.admin_id,
                   body={"active": True}, expect=expect)

    def do_migrate(self, arg):
        alias, expect = self._target(arg, "req")
        row = observe.request_row(self.run.cluster, self._req(alias))
        self.memo[f"{alias}.node_before"] = row["node"]
        others = [n for n in self.run.group_nodes() if n != row["node"]]
        if not others:
            raise StepFailed("같은 리소스 그룹에 옮겨 갈 다른 farm 노드가 없음")
        self._call("POST", f"/api/admin/requests/{self._req(alias)}/migrations", as_user=self.run.admin_id,
                   body={"nodes": [row["node"], others[0]], "force": True}, expect=expect)

    def do_restart(self, arg):
        """현재 노드에서 컨테이너를 다시 만든다. by는 admin(기본)·owner·사용자 별칭, keep은 변경분 유지(기본 true)."""
        alias, expect = self._target(arg, "req")
        options = arg if isinstance(arg, dict) else {}
        row = observe.request_row(self.run.cluster, self._req(alias))
        self.memo[f"{alias}.node_before"], self.memo[f"{alias}.pod_before"] = row["node"], row["pod"]
        by = options.get("by", "admin")
        if by == "admin":
            path, caller = f"/api/admin/requests/{self._req(alias)}/restarts", self.run.admin_id
        else:
            path = f"/api/requests/{self._req(alias)}/restarts"
            caller = self._owner(alias) if by == "owner" else self.users[by]["id"]
        self._call("POST", path, as_user=caller, body={"keepChanges": options.get("keep", True)}, expect=expect)

    def do_expect_restarted(self, alias):
        row = observe.request_row(self.run.cluster, self._req(alias))
        if row["node"] != self.memo[f"{alias}.node_before"]:
            raise StepFailed(f"{alias}: 재시작인데 노드가 {self.memo[f'{alias}.node_before']}에서 {row['node']}로 바뀜")
        if row["pod"] == self.memo[f"{alias}.pod_before"]:
            raise StepFailed(f"{alias}: Pod가 {row['pod']} 그대로 — 다시 만들어지지 않음")

    def do_mark_pod(self, alias):
        row = observe.request_row(self.run.cluster, self._req(alias))
        observe.pod_write_marker(self.run.cluster, row["pod"])

    def do_expect_marker(self, arg):
        for alias, want in arg.items():
            row = observe.request_row(self.run.cluster, self._req(alias))
            present = observe.pod_has_marker(self.run.cluster, row["pod"])
            if present != (want == "present"):
                raise StepFailed(f"{alias}: 컨테이너 표식이 {want}이어야 하는데 {'있음' if present else '없음'}")

    def do_wait(self, arg):
        timeout = arg.get("timeout", self.run.wait_timeout) if isinstance(arg, dict) else self.run.wait_timeout
        for alias, wanted in arg.items():
            if alias == "timeout":
                continue
            wanted = set(wanted if isinstance(wanted, list) else [wanted])
            row = observe.wait_status(self.run.cluster, self._req(alias), wanted, timeout, self.run.interval)
            self.observed.append({"step": self.step_no, "request": alias, "status": row and row["status"]})
            if not row or row["status"] not in wanted:
                codes = sorted(observe.oplog_codes(self.run.cluster, self._req(alias)))
                raise StepFailed(f"{alias}: {sorted(wanted)} 중 하나를 기다렸지만 {row and row['status']} (코드 {codes})")

    def do_wait_job(self, arg):
        """생성 작업이 원하는 결과(SUCCESS·FAIL·DEGRADED)로 끝날 때까지 기다린다."""
        timeout = arg.get("timeout", self.run.wait_timeout)
        for alias, wanted in arg.items():
            if alias == "timeout":
                continue
            outcome = None

            def done():
                nonlocal outcome
                outcome = observe.job_outcome(self.run.cluster, self._req(alias))
                return outcome not in (None, "RUNNING")
            observe.wait_until(done, timeout, self.run.interval)
            self.observed.append({"step": self.step_no, "request": alias, "job": outcome})
            if outcome != wanted:
                codes = sorted(observe.oplog_codes(self.run.cluster, self._req(alias)))
                raise StepFailed(f"{alias}: 작업이 {wanted}로 끝나야 하는데 {outcome} (코드 {codes})")

    def do_wait_codes(self, arg):
        """오류 코드가 작업 기록에 남을 때까지 기다린다 — 장애가 실제로 부딪힌 뒤에 복구하려고 쓴다."""
        timeout = arg.get("timeout", self.run.wait_timeout)
        for alias, codes in arg.items():
            if alias == "timeout":
                continue
            seen = observe.wait_until(
                lambda: set(codes) <= observe.oplog_codes(self.run.cluster, self._req(alias)) and True,
                timeout, self.run.interval)
            if not seen:
                got = sorted(observe.oplog_codes(self.run.cluster, self._req(alias)))
                raise StepFailed(f"{alias}: 오류 코드 {sorted(codes)}를 기다렸지만 {got}")

    def do_expect_status(self, arg):
        for alias, wanted in arg.items():
            row = observe.request_row(self.run.cluster, self._req(alias))
            if not row or row["status"] != wanted:
                raise StepFailed(f"{alias}: {wanted}이어야 하는데 {row and row['status']}")

    def do_expect_user(self, arg):
        for alias, want in arg.items():
            row = observe.user_row(self.run.cluster, self.users[alias]["id"])
            self.observed.append({"step": self.step_no, "user": alias, **row})
            if "account" in want and row["account"] != want["account"]:
                raise StepFailed(f"{alias}: 계정 상태가 {want['account']}이어야 하는데 {row['account']}")
            uid_rule = want.get("uid")
            if uid_rule == "issued" and row["uid"] is None:
                raise StepFailed(f"{alias}: UID가 배정돼야 하는데 없음")
            if uid_rule == "none" and row["uid"] is not None:
                raise StepFailed(f"{alias}: UID가 없어야 하는데 {row['uid']}")
            if isinstance(uid_rule, dict) and row["uid"] != self.memo[uid_rule["same_as"]]:
                raise StepFailed(f"{alias}: UID가 {self.memo[uid_rule['same_as']]}로 유지돼야 하는데 {row['uid']}")

    def do_wait_account(self, arg):
        """우분투 계정이 원하는 상태(NONE·ACTIVE·RELEASING)가 될 때까지 기다린다. 계정 회수는 컨테이너 회수가
        끝난 뒤 노드마다 따로 돌아서, 신청이 DELETED가 된 뒤에도 잠시 RELEASING에 머문다."""
        timeout = arg.get("timeout", self.run.wait_timeout)
        for alias, wanted in arg.items():
            if alias == "timeout":
                continue
            user_id = self.users[alias]["id"]
            row = observe.wait_until(
                lambda: (r := observe.user_row(self.run.cluster, user_id)) and r["account"] == wanted and r,
                timeout, self.run.interval) or observe.user_row(self.run.cluster, user_id)
            self.observed.append({"step": self.step_no, "user": alias, **row})
            if row["account"] != wanted:
                raise StepFailed(f"{alias}: 계정 상태 {wanted}를 기다렸지만 {row['account']}")

    def do_reset_password(self, arg):
        """관리자가 사용자의 비밀번호를 새로 정한다. 버리는 무작위 값이라 어디에도 남기지 않는다.
        바뀌었는지 나중에 견줄 수 있게, 초기화 전에 기록돼 있던 해시를 기억해 둔다."""
        alias, expect = self._target(arg, "user")
        user = self.users[alias]
        self.memo[f"{alias}.password_before"] = observe.recorded_password_hash(self.run.cluster, user["id"])
        self._call("PUT", f"/api/admin/users/{user['id']}/password", as_user=self.run.admin_id,
                   body={"newPassword": secrets.token_urlsafe(24)}, expect=expect)

    def do_wait_password(self, arg):
        """가장 최근 비밀번호 재설정 신청이 원하는 상태(PENDING·PROCESSING·APPLIED·DENIED)가 될 때까지 기다린다.
        초기화는 컨테이너에 반영하는 작업만 등록하고 돌아오므로, 적용은 그 작업이 끝난 뒤다."""
        timeout = arg.get("timeout", self.run.wait_timeout)
        for alias, wanted in arg.items():
            if alias == "timeout":
                continue
            user_id = self.users[alias]["id"]
            observe.wait_until(lambda: observe.password_reset_status(self.run.cluster, user_id) == wanted,
                               timeout, self.run.interval)
            status = observe.password_reset_status(self.run.cluster, user_id)
            self.observed.append({"step": self.step_no, "user": alias, "password_reset": status})
            if status != wanted:
                raise StepFailed(f"{alias}: 비밀번호 재설정 상태 {wanted}를 기다렸지만 {status}")

    def do_expect_password(self, arg):
        """{신청: changed|unchanged}. 그 신청의 컨테이너에 들어 있는 로그인 비밀번호가 admin_be 기록과 같아야 한다
        — 어긋나면 웹과 SSH 비밀번호가 달라진 것이다. 마지막 초기화 전과 견주어 바뀌었는지도 함께 본다."""
        for alias, want in arg.items():
            owner_alias, owner = next((a, u) for a, u in self.users.items() if u["id"] == self._owner(alias))
            row = observe.request_row(self.run.cluster, self._req(alias))
            recorded = observe.recorded_password_hash(self.run.cluster, owner["id"])
            in_pod = observe.pod_password_hash(self.run.cluster, row["pod"], owner["name"])
            changed = recorded != self.memo[f"{owner_alias}.password_before"]
            self.observed.append({"step": self.step_no, "request": alias, "password_in_sync": in_pod == recorded,
                                  "password_changed": changed})
            if in_pod != recorded:
                raise StepFailed(f"{alias}: 컨테이너의 로그인 비밀번호가 admin_be 기록과 다름")
            if changed != (want == "changed"):
                raise StepFailed(f"{alias}: 비밀번호가 {want}이어야 하는데 {'바뀜' if changed else '그대로'}")

    # ---- 공용 그룹 ----
    def do_create_group(self, arg):
        """사용자가 E2E 공용 그룹을 만든다. 만들기는 작업으로 등록만 되므로 끝나기는 wait_group 으로 기다린다."""
        if observe.group_row(self.run.cluster, self.run.group_name):
            # 같은 실행의 앞선 사례가 이미 만들었다. 그 그룹을 그대로 쓴다.
            self.observed.append({"step": self.step_no, "group": "already exists"})
            self.memo[f"{arg['as']}.operation"] = None
            return
        payload = self._call("POST", "/api/groups", as_user=self.users[arg["user"]]["id"],
                             body={"groupName": self.run.group_name})
        self.memo[f"{arg['as']}.operation"] = int(payload["data"]["operationId"])

    def do_wait_group(self, arg):
        """그룹 작업(만들기·빼기)이 원하는 상태(APPLIED·FAILED)가 될 때까지 기다린다."""
        timeout = arg.get("timeout", self.run.wait_timeout)
        for alias, wanted in arg.items():
            if alias == "timeout":
                continue
            operation_id = self.memo[f"{alias}.operation"]
            if operation_id is None:
                continue  # 이미 있던 그룹이라 작업이 없다
            observe.wait_until(
                lambda: (o := observe.group_operation(self.run.cluster, operation_id)) and o["status"] != "PROCESSING",
                timeout, self.run.interval)
            row = observe.group_operation(self.run.cluster, operation_id)
            self.observed.append({"step": self.step_no, "group_operation": alias, **(row or {})})
            if not row or row["status"] != wanted:
                raise StepFailed(f"{alias}: 그룹 작업이 {wanted}로 끝나야 하는데 {row}")

    def do_request_group(self, arg):
        """신청의 주인이 E2E 공용 그룹 추가를 변경 요청으로 낸다."""
        alias = arg["req"]
        group = observe.group_row(self.run.cluster, self.run.group_name)
        if not group:
            raise StepFailed(f"그룹 {self.run.group_name}이 admin_be에 없음")
        self._call("POST", f"/api/requests/{self._req(alias)}/change", as_user=self._owner(alias),
                   body={"changeType": "GROUP", "newValue": json.dumps([group["gid"]]),
                         "reason": f"e2e {self.case} 공용 그룹 추가"})
        rows = self.run.cluster.sql("SELECT MAX(change_request_id) FROM change_request "
                                    f"WHERE request_id={int(self._req(alias))} AND change_type='GROUP';")
        self.memo[f"{arg['as']}.change"] = int(rows[0][0])

    def do_approve_change(self, arg):
        alias, expect = self._target(arg, "change")
        self._call("POST", f"/api/admin/change-requests/{self.memo[f'{alias}.change']}/approval",
                   as_user=self.run.admin_id, body={"adminComment": "e2e"}, expect=expect)

    def do_wait_change(self, arg):
        """변경 요청이 원하는 상태가 될 때까지 기다린다. 그룹 추가는 승인하면 PROCESSING 이 되고, 반영 작업이
        성공하면 FULFILLED, 실패하면 PENDING 으로 돌아온다."""
        timeout = arg.get("timeout", self.run.wait_timeout)
        for alias, wanted in arg.items():
            if alias == "timeout":
                continue
            change_id = self.memo[f"{alias}.change"]
            observe.wait_until(lambda: observe.change_request_status(self.run.cluster, change_id) == wanted,
                               timeout, self.run.interval)
            status = observe.change_request_status(self.run.cluster, change_id)
            self.observed.append({"step": self.step_no, "change": alias, "status": status})
            if status != wanted:
                codes = sorted(observe.change_request_codes(self.run.cluster, change_id))
                raise StepFailed(f"{alias}: 변경 요청 상태 {wanted}를 기다렸지만 {status} (코드 {codes})")

    def do_remove_group(self, arg):
        """관리자가 사용자를 E2E 공용 그룹에서 뺀다. 빼기도 작업으로 등록만 된다."""
        group = observe.group_row(self.run.cluster, self.run.group_name)
        if not group:
            raise StepFailed(f"그룹 {self.run.group_name}이 admin_be에 없음")
        payload = self._call("DELETE", f"/api/admin/users/{self.users[arg['user']]['id']}/groups/{group['id']}",
                             as_user=self.run.admin_id)
        self.memo[f"{arg['as']}.operation"] = int(payload["data"]["operationId"])

    def do_expect_member(self, arg):
        """{사용자: present|absent}. admin_be 기록·계정 원장·떠 있는 컨테이너가 모두 같은 답이어야 한다."""
        for alias, want in arg.items():
            user = self.users[alias]
            pods = [r[0] for r in self.run.cluster.sql(
                f"SELECT pod_name FROM requests WHERE user_id={int(user['id'])} AND status='FULFILLED' "
                "AND pod_name IS NOT NULL;")]
            seen = observe.group_membership(self.run.cluster, user["id"], user["name"], self.run.group_name, pods)
            self.observed.append({"step": self.step_no, "user": alias, "membership": seen})
            answers = [seen["recorded"], seen["ledger"], *seen["pods"]]
            if any(answer != (want == "present") for answer in answers):
                raise StepFailed(f"{alias}: 그룹 소속이 {want}이어야 하는데 {seen}")

    def do_expect_change_codes(self, arg):
        for alias, codes in arg.items():
            seen = observe.change_request_codes(self.run.cluster, self.memo[f"{alias}.change"])
            self.observed.append({"step": self.step_no, "change": alias, "codes": sorted(seen)})
            missing = set(codes) - seen
            if missing:
                raise StepFailed(f"{alias}: 오류 코드 {sorted(missing)}가 기록되지 않음 (기록된 코드 {sorted(seen)})")

    def do_remember_uid(self, arg):
        row = observe.user_row(self.run.cluster, self.users[arg["user"]]["id"])
        self.memo[arg["as"]] = row["uid"]

    def do_expect_codes(self, arg):
        for alias, codes in arg.items():
            seen = observe.oplog_codes(self.run.cluster, self._req(alias))
            self.observed.append({"step": self.step_no, "request": alias, "codes": sorted(seen)})
            missing = set(codes) - seen
            if missing:
                raise StepFailed(f"{alias}: 오류 코드 {sorted(missing)}가 기록되지 않음 (기록된 코드 {sorted(seen)})")

    def do_expect_pod(self, arg):
        for alias, want in arg.items():
            row = observe.request_row(self.run.cluster, self._req(alias))
            present = observe.pod_exists(self.run.cluster, row and row["pod"])
            if present != (want == "present"):
                raise StepFailed(f"{alias}: Pod가 {want}이어야 하는데 {'있음' if present else '없음'}")

    def do_expect_node_changed(self, alias):
        row = observe.request_row(self.run.cluster, self._req(alias))
        before = self.memo[f"{alias}.node_before"]
        if row["node"] == before:
            raise StepFailed(f"{alias}: 노드가 {before}에서 바뀌지 않음")

    def do_fault(self, arg):
        (kind, target), = arg.items()
        getattr(self.run.faults, kind)(target)

    def do_heal(self, _):
        self.run.faults.heal_all()

    def do_sleep(self, seconds):
        time.sleep(float(seconds))
