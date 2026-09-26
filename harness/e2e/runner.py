"""catalog.yaml의 사례를 실제 스택에서 한 단계씩 실행하고 판정한다.

사례마다 사용자를 새로 만든다(`<스택 접두어>e2e<runid><사례번호><별칭>`). 스택에 남아 있는 옛 계정은
과거 시험의 NAS 홈·uid 잔재를 물고 있을 수 있어 결과를 믿을 수 없기 때문이다.
"""
import datetime as dt
import json
import os
import time

from . import observe
from .catalog import is_fault_case
from .ports import safe
from .resetter import admin_email, run_prefix


class StepFailed(AssertionError):
    pass


class Run:
    def __init__(self, cluster, api, faults, *, stack_prefix, run_id, wait_timeout=900, interval=10,
                 progress=lambda line: None):
        self.cluster, self.api, self.faults = cluster, api, faults
        # 한 사례가 수 분씩 걸려 끝날 때만 알리면 멈춘 것과 구분이 안 된다. 단계마다 한 줄씩 알린다.
        self.progress = progress
        self.prefix = run_prefix(stack_prefix, run_id)
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

    def run_case(self, case, *, allow_faults):
        if is_fault_case(case) and not allow_faults:
            return {"id": case["id"], "result": "SKIP", "reason": "장애 사례 (--allow-faults 없음)"}
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
        self.cluster.sql(
            "INSERT INTO users (created_at, updated_at, department, email, is_active, name, password, phone, role, "
            "student_id, ubuntu_username, ubuntu_account_active) VALUES (NOW(6), NOW(6), 'e2e', "
            f"'{safe(email)}', b'1', 'e2e', 'x', '010-0000-0000', '{safe(role)}', '0000000000', {name}, 0);")
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
                "usagePurpose": f"e2e {self.case}", "formAnswers": {}, "expiresAt": expires,
                "ubuntuPassword": "E2e-" + os.urandom(8).hex()}
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
        others = [n for n in self.run.farm_nodes() if n != row["node"]]
        if not others:
            raise StepFailed("옮겨 갈 다른 farm 노드가 없음")
        self._call("POST", f"/api/admin/requests/{self._req(alias)}/migrations", as_user=self.run.admin_id,
                   body={"nodes": [row["node"], others[0]], "force": True}, expect=expect)

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

    def do_expect_status(self, arg):
        for alias, wanted in arg.items():
            row = observe.request_row(self.run.cluster, self._req(alias))
            if not row or row["status"] != wanted:
                raise StepFailed(f"{alias}: {wanted}이어야 하는데 {row and row['status']}")

    def do_expect_user(self, arg):
        for alias, want in arg.items():
            row = observe.user_row(self.run.cluster, self.users[alias]["id"])
            self.observed.append({"step": self.step_no, "user": alias, **row})
            if "active" in want and row["active"] != want["active"]:
                raise StepFailed(f"{alias}: 계정 활성 {want['active']}이어야 하는데 {row['active']}")
            uid_rule = want.get("uid")
            if uid_rule == "issued" and row["uid"] is None:
                raise StepFailed(f"{alias}: UID가 배정돼야 하는데 없음")
            if uid_rule == "none" and row["uid"] is not None:
                raise StepFailed(f"{alias}: UID가 없어야 하는데 {row['uid']}")
            if isinstance(uid_rule, dict) and row["uid"] != self.memo[uid_rule["same_as"]]:
                raise StepFailed(f"{alias}: UID가 {self.memo[uid_rule['same_as']]}로 유지돼야 하는데 {row['uid']}")

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
