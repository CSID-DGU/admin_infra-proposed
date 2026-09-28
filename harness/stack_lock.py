"""실험 스택 하나를 측정 하네스와 E2E 도구가 동시에 쓰지 못하게 하는 잠금.

잠금은 그 namespace 의 experiment-lock ConfigMap 이다. kubectl create 가 이미 있는 이름이면
실패한다는 성질로 먼저 잡은 쪽만 성공한다. 시간이 지나면 풀리는 규칙은 두지 않는다. 10분 넘게
걸리는 trial 의 잠금을 잘못 풀 수 있어서다. 남은 잠금은 사람이 force_release 로 푼다.

E2E 도구에 잠금이 들어가기 전까지는 이쪽만 막을 수 있으므로, 잡기 전에 E2E 의 흔적(관리자 행과
e2e-run 표지의 NetworkPolicy)을 본다. 모든 kubectl 과 SQL 은 system.py 를 거친다.
"""
import json
from contextlib import contextmanager

import system

LOCK_NAME = "experiment-lock"
E2E_ADMIN_SQL = "SELECT email FROM users WHERE email LIKE 'e2e%-admin@example.com'"


class LockError(Exception):
    pass


class LockHeld(LockError):
    """다른 실행이 잠금을 잡고 있다."""

    def __init__(self, owner, run, started_at):
        super().__init__(f"{LOCK_NAME} 를 이미 잡고 있다: owner={owner} run={run} started_at={started_at}")
        self.owner, self.run, self.started_at = owner, run, started_at


class LockUnknown(LockError):
    """잠금이나 E2E 흔적을 확인하지 못했다. 잠겨 있다는 뜻이 아니다."""


class E2ETraceFound(LockError):
    """E2E 도구가 이 스택을 쓰고 있다는 흔적이 있다."""

    def __init__(self, evidence):
        super().__init__(f"E2E 흔적이 있어 시작하지 않는다: {evidence}")
        self.evidence = evidence


def _kube(host, namespace, args):
    try:
        return system.stack_kube(host, namespace, args)
    except system.SystemCallFailed as e:
        raise LockUnknown(f"kubectl {' '.join(args)} 를 부르지 못했다: {e}") from e


def _already_exists(row):
    return "AlreadyExists" in row.get("stderr", "") or "already exists" in row.get("stderr", "")


def _not_found(row):
    return "NotFound" in row.get("stderr", "") or "not found" in row.get("stderr", "")


def e2e_traces(host, namespace):
    """E2E 흔적 목록. 비어 있으면 흔적이 없다. 확인하지 못하면 LockUnknown."""
    try:
        sql = system.stack_sql(host, namespace, "web_admin", E2E_ADMIN_SQL)
    except system.SystemCallFailed as e:
        raise LockUnknown(f"web_admin.users 를 조회하지 못했다: {e}") from e
    if sql.get("rc") != 0:
        raise LockUnknown(f"web_admin.users 조회 실패: {sql.get('stderr', '')}")
    evidence = [f"users.email={r[0]}" for r in sql.get("rows") or []]
    np = _kube(host, namespace, ["get", "networkpolicy", "-l", "e2e-run", "-o", "name"])
    if np.get("rc") != 0:
        raise LockUnknown(f"e2e-run NetworkPolicy 조회 실패: {np.get('stderr', '')}")
    evidence += [line for line in np.get("stdout", "").splitlines() if line.strip()]
    return evidence


def read(host, namespace):
    """현재 잠금의 data(owner·run·started_at). 없으면 None."""
    row = _kube(host, namespace, ["get", "configmap", LOCK_NAME, "-o", "json"])
    if row.get("rc") != 0:
        if _not_found(row):
            return None
        raise LockUnknown(f"{LOCK_NAME} 조회 실패: {row.get('stderr', '')}")
    try:
        return json.loads(row["stdout"]).get("data") or {}
    except (KeyError, ValueError) as e:
        raise LockUnknown(f"{LOCK_NAME} 출력을 읽지 못했다: {row.get('stdout', '')[:200]!r}") from e


def _held(data):
    return LockHeld(data.get("owner"), data.get("run"), data.get("started_at"))


def acquire(host, namespace, *, owner, run_id, now):
    evidence = e2e_traces(host, namespace)
    if evidence:
        raise E2ETraceFound(evidence)
    row = _kube(host, namespace, [
        "create", "configmap", LOCK_NAME,
        f"--from-literal=owner={owner}", f"--from-literal=run={run_id}",
        f"--from-literal=started_at={now}"])
    if row.get("rc") == 0:
        return
    if not _already_exists(row):
        raise LockUnknown(f"{LOCK_NAME} 생성 실패: {row.get('stderr', '')}")
    data = read(host, namespace)
    if data is None:
        # 있다고 해서 읽었더니 그새 풀렸다. 다시 잡으려 하지 않고 모른다고 올린다.
        raise LockUnknown(f"{LOCK_NAME} 가 이미 있다고 했지만 읽을 때는 없었다")
    raise _held(data)


def _delete(host, namespace):
    row = _kube(host, namespace, ["delete", "configmap", LOCK_NAME])
    if row.get("rc") != 0:
        raise LockUnknown(f"{LOCK_NAME} 삭제 실패: {row.get('stderr', '')}")


def release(host, namespace, *, run_id):
    """자기 잠금(run 이 같은 것)만 푼다. 이미 없으면 할 일이 없다."""
    data = read(host, namespace)
    if data is None:
        return
    if data.get("run") != run_id:
        raise _held(data)
    _delete(host, namespace)


def force_release(host, namespace):
    """확인 없이 지운다. 사람이 부르는 명령에서만 쓴다."""
    _delete(host, namespace)


@contextmanager
def hold(host, namespace, *, owner, run_id, now):
    acquire(host, namespace, owner=owner, run_id=run_id, now=now)
    try:
        yield
    finally:
        release(host, namespace, run_id=run_id)
