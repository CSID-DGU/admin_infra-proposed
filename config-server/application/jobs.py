"""작업 실행 엔진 — 등록된 작업을 단계 재시도 정책과 lease 아래에서 끝까지 실행한다.

main.py에서 얇게 이동한 코드다. main의 단계 함수·저장소 래퍼는 테스트가 main.* 를
대역으로 바꾸는 계약을 지키기 위해 `_main.<이름>`으로 호출 시점에 늦게 바인딩한다.
"""
import json
import os
import time

from kubernetes import client

from adapters import job_control
from adapters.job_control import LeaseLost
from adapters.operation_log import Action, Phase, current_job_id, current_attempt
from lifecycle_steps import verify

class _MainProxy:
    """main 속성의 늦은 바인딩. jobs를 먼저 import해도 순환이 성립하지 않도록
    main 로드를 첫 속성 접근 시점까지 미룬다. 테스트가 main.*를 대역으로 바꾸는
    계약(단계 목록·정책·저장소 래퍼)도 이 지연 참조로 유지된다."""

    def __getattr__(self, name):
        import main
        return getattr(main, name)


_main = _MainProxy()


STEP_MAX_ATTEMPTS = int(os.getenv("STEP_MAX_ATTEMPTS", "3"))

RETRY_DELAY_SEC = float(os.getenv("STEP_RETRY_DELAY_SEC", "2"))

def _observe_account_created(ctx):
    """계정 단계의 효과 확인. 이미 만들어졌으면 후속 단계가 쓸 uid/gid를 함께 복원한다."""
    for line in _main.read_passwd_lines():
        entry = _main.parse_passwd_line(line)
        if entry and entry["name"] == ctx["username"]:
            ctx.setdefault("uid", int(entry["uid"]))
            ctx.setdefault("gid", int(entry["gid"]))
            return True
    return False

def _observe_krb5_principal(ctx):
    """principal 단계의 마지막 효과인 keytab Secret 존재 여부로 판정한다."""
    _main.load_k8s()
    try:
        client.CoreV1Api().read_namespaced_secret(
            name=f"krb5-keytab-{ctx['username']}", namespace=_main.app.config["NAMESPACE"])
        return True
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return False
        raise

def _observe_pod_created(ctx):
    _main.load_k8s()
    try:
        client.CoreV1Api().read_namespaced_pod(ctx["pod_name"], _main.app.config["NAMESPACE"])
        return True
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return False
        raise

STEP_OBSERVERS = {
    "step_create_account": _observe_account_created,
    "step_create_krb5_principal": _observe_krb5_principal,
    "step_create_pod_k8s": _observe_pod_created,
}

RERUN_SAFE = {
    "step_fetch_user_config", "step_prepare_pod", "step_select_node", "step_build_pod_spec",
    "step_create_home", "step_wait_ready", "step_create_services",
    "step_delete_services", "step_release_nodeports", "step_delete_pod_k8s",
    "step_cleanup_pod_node_krb5", "step_check_account_revocable",
    "step_delete_account", "step_remove_krb5",
    "step_migrate_select_target", "step_migrate_inherit_password", "step_migrate_cleanup_old",
    "step_add_user_groups", "step_sync_ad_groups",
}

PRE_STEP = {
    "step_build_pod_spec": lambda ctx: ctx.get("pod_name") and _main.release_nodeports(ctx["pod_name"]),
    "step_create_services": lambda ctx: _main.delete_nodeport_services(ctx["pod_name"], _main.app.config["NAMESPACE"]),
}

ALWAYS_RERUN = {"step_fetch_user_config", "step_migrate_inherit_password"}

DEFER_DONE = {"step_build_pod_spec": "step_create_pod_k8s"}

SAVED_CTX_KEYS = ("uid", "gid", "pod_name", "node", "allocated_ports", "pod_node_name",
                  "verify_ports", "verify_node",
                  "old_pod_name", "from_node", "skipped", "skip_reason", "old_pod_cleanup")

def _saved_ctx(ctx):
    return {k: ctx[k] for k in SAVED_CTX_KEYS if k in ctx}

class _StepDegraded(Exception):
    """재시도로 해소되지 않거나 실제 상태를 판정할 수 없다. 자동 완료 대신 관리자에게 넘긴다."""

    def __init__(self, step_name, reason, cause, unknowable):
        super().__init__(reason)
        self.step_name, self.reason, self.cause = step_name, reason, cause
        self.unknowable = unknowable  # True면 실행 여부 자체를 모름 → 작업 끝 행을 UNKNOWN으로

def _is_baseline():
    return _main.VERIFY_MODE == "baseline"

def _end_phase(unknown):
    """baseline은 운영 admin_be처럼 결과 불명(타임아웃 등)도 실패로 판정하고 뒷정리한다.
    결과 불명이었다는 사실은 error_detail에 남긴다."""
    return Phase.UNKNOWN if unknown and not _is_baseline() else Phase.FAIL

def _execute_step(step, ctx, kind, request_id, username):
    """단계 하나를 재시도 정책으로 실행한다. UNKNOWN이면 재실행 전에 실제 상태를 먼저 본다.
    baseline은 한 번만 실행하고, 실패하면 확인·재시도 없이 그 오류를 그대로 넘긴다(운영 동기 경로와 같음)."""
    name = step.__name__
    baseline = _is_baseline()
    # 접근 검증은 생성 직후 전파 지연(kinit 타이머·NFS)이 있어 기본보다 여유 있게 재시도한다.
    # (verify 속성은 호출 시점에만 읽는다 — import 순서와 무관하게 동작)
    if baseline:
        max_attempts = 1
    elif name in verify.RERUN_SAFE_STEPS:
        max_attempts = verify.VERIFY_MAX_ATTEMPTS
    else:
        max_attempts = _main.STEP_MAX_ATTEMPTS
    for attempt in range(1, max_attempts + 1):
        hook = _main.PRE_STEP.get(name)
        if hook is not None:
            try:
                hook(ctx)
            except Exception:
                _main.app.logger.warning(f"[JOB] {name} 사전 정리 실패 — 단계는 계속", exc_info=True)
        token = current_attempt.set(attempt)
        try:
            step(ctx)
            return
        except Exception as e:
            err = e
        finally:
            current_attempt.reset(token)

        if baseline:
            raise err
        if not getattr(err, "retry", True):
            _main.app.logger.info(f"[JOB] {name} 다시 해도 같은 결과 — 재시도하지 않음")
            raise err
        unknown = err.unknown if isinstance(err, _main.StepFailed) else _main._is_unknown_result(err)
        if unknown:
            observer = _main.STEP_OBSERVERS.get(name)
            if observer is not None:
                try:
                    if observer(ctx):
                        _main.app.logger.info(f"[JOB] {name} 결과 불명이었지만 효과 확인됨 — 성공으로 진행")
                        return
                except Exception as oe:
                    raise _StepDegraded(name, "OBSERVE_FAILED", oe, unknowable=True) from err
            elif name not in _main.RERUN_SAFE and name not in verify.RERUN_SAFE_STEPS:
                raise _StepDegraded(name, "UNRESUMABLE_UNKNOWN", err, unknowable=True) from err
        # 4xx는 다시 돌려도 결과가 같다(중복·검증류). UNKNOWN은 제외 — 위에서 이미 걸렀다.
        if isinstance(err, _main.StepFailed) and not unknown and 400 <= err.status < 500:
            raise err
        if attempt == max_attempts:
            raise _StepDegraded(name, "RETRIES_EXHAUSTED", err, unknowable=unknown) from err
        code = err.body.get("error") if isinstance(err, _main.StepFailed) and isinstance(err.body, dict) \
            else type(err).__name__
        _main.log_operation(request_id=request_id, username=username, action=JOB_ACTIONS[kind],
                      phase=Phase.RETRY, attempt=attempt + 1, resource_type=name[-32:],
                      error_code=str(code)[:64], error_detail=str(err)[:1000])
        if _main.RETRY_DELAY_SEC:
            time.sleep(_main.RETRY_DELAY_SEC)

JOB_ACTIONS = {"provision": Action.PROVISION, "revoke": Action.REVOKE, "migrate": Action.MIGRATE}

_JOB_KIND = {action.value: kind for kind, action in JOB_ACTIONS.items()}

def _job_steps(kind, job):
    if kind == "migrate":
        steps = list(_main.MIGRATE_STEPS)
        if _main.VERIFY_MODE == "full":
            # 새 Pod가 실제로 쓸 수 있는지 시험한 뒤에 기존 Pod를 정리한다(마지막 단계가 기존 Pod 정리).
            # 시험이 끝내 통과하지 못하면 두 Pod를 그대로 둔 채 관리자에게 넘긴다(DEGRADED).
            steps = steps[:-1] + verify.VERIFY_ACCESS_STEPS + steps[-1:]
        return steps
    if kind == "provision":
        if job.get("account"):
            steps = _main.ACCOUNT_CREATE_STEPS + _main.POD_CREATE_STEPS
        elif job.get("supp_groups_only"):
            # 기존 계정 재사용 시 그룹만 추가
            steps = _main.SUPP_GROUPS_ONLY_STEPS + _main.POD_CREATE_STEPS
        else:
            steps = _main.POD_CREATE_STEPS
        if _main.VERIFY_MODE == "full":
            # 다섯 시험을 모두 통과해야 작업 SUCCESS 행이 남는다 — 통과 전엔 완료로 기록되지 않는다.
            steps = steps + verify.VERIFY_ACCESS_STEPS
        return steps
    steps = list(_main.POD_DELETE_STEPS) if job.get("pod_name") else []
    if job.get("delete_account"):
        # 계정을 회수해도 홈은 남긴다 — 지우는 단계를 넣지 않는다.
        steps += [_main.step_check_account_revocable, _main.step_delete_account, _main.step_remove_krb5]
    if _main.VERIFY_MODE == "full" and steps:
        # 검사 대상(노드·포트)은 삭제 전에 잡아 두고, 차단 확인은 삭제가 다 끝난 뒤 한다.
        steps = ([verify.step_capture_access_targets] if job.get("pod_name") else []) + steps \
            + ([verify.step_verify_revoked] if job.get("pod_name") else []) \
            + ([verify.step_verify_account_revoked] if job.get("delete_account") else [])
    return steps

def _job_ctx(kind, request_id, job):
    ctx = {"request_id": request_id, "username": job["username"]}
    if kind == "provision":
        ctx["config_by_request"] = True
        if job.get("account"):
            ctx.update(name=job["username"], **job["account"])
        elif job.get("supp_groups_only"):
            # 기존 계정 재사용 시 그룹만 추가하는 경우
            ctx["supp_groups"] = job["supp_groups_only"]
    if kind == "revoke":
        ctx.update(pod_name=job.get("pod_name"), node_name=job.get("node_name"),
                   rollback=_main._new_delete_rollback())
    if kind == "migrate":
        # 새 Pod 이름은 Pod 준비 단계가 정한다. 기존 Pod는 old_pod_name으로 따로 둔다.
        ctx.update(config_by_request=True, old_pod_name=job.get("pod_name"), nodes=job["nodes"],
                   min_ratio=job.get("min_improvement_ratio", 0.2), force=bool(job.get("force")))
    return ctx

def find_unfinished_jobs(limit=100):
    """operation_log에서 작업 START만 있고 끝이 없는 작업을 오래된 순으로. (kind, request_id, username, job_id)"""
    conn = _main.get_log_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.action, s.request_id, s.username, s.id FROM operation_log s "
                "WHERE s.action IN (%s, %s, %s) AND s.phase = %s AND NOT EXISTS ("
                " SELECT 1 FROM operation_log e WHERE e.request_id = s.request_id"
                " AND e.action = s.action AND e.id > s.id AND e.phase IN (%s, %s, %s)) "
                "ORDER BY s.id LIMIT %s",
                (Action.PROVISION.value, Action.REVOKE.value, Action.MIGRATE.value, Phase.START.value,
                 Phase.SUCCESS.value, Phase.FAIL.value, Phase.UNKNOWN.value, limit),
            )
            return [(_JOB_KIND[a], r, u, j) for a, r, u, j in cur.fetchall()]
    finally:
        conn.close()

def job_end_exists(action, request_id, job_id):
    """그 작업(START 행 id = job_id)의 끝 행이 이미 있는지. 제어기가 미완료 목록을 읽은 뒤 목록을 훑는
    사이에 그 작업이 끝나면 같은 작업을 한 번 더 집을 수 있다. 끝난 작업은 lease 행도 지워져 선점이
    다시 성공하므로, 입력이 없을 때 이 조회로 "이미 끝난 작업"과 "입력이 유실된 작업"을 가른다.
    조회가 실패하면 판단을 미루지 않고 없는 것으로 본다(기존처럼 실패로 남겨 사람이 보게 한다)."""
    try:
        conn = _main.get_log_db_connection()
    except Exception:
        _main.app.logger.warning("[JOB] job end lookup failed", exc_info=True)
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM operation_log WHERE request_id = %s AND action = %s AND id > %s"
                " AND phase IN (%s, %s, %s) LIMIT 1",
                (str(request_id), action, job_id,
                 Phase.SUCCESS.value, Phase.FAIL.value, Phase.UNKNOWN.value),
            )
            return cur.fetchone() is not None
    except Exception:
        _main.app.logger.warning("[JOB] job end lookup failed", exc_info=True)
        return False
    finally:
        conn.close()

def _finish_job(kind, request_id, username, phase, error_code=None, error_detail=None, ctx=None):
    ctx = ctx or {}
    if kind in ("provision", "migrate") and phase != Phase.SUCCESS:
        # Pod 단계까지 가지 못한 실패(계정 단계 등)는 진행 상황이 "started"에 멈춰 있으므로 닫아 준다.
        try:
            if (_main.get_pod_creation_status(request_id) or {}).get("stage") != "failed":
                _main.set_pod_creation_status(request_id, "failed", error_code or "작업 실패")
        except Exception:
            _main.app.logger.warning("[JOB] pod status update failed", exc_info=True)
    if phase == Phase.SUCCESS:
        # 작업이 만든 자원을 결과 조회에 실어 준다. 동기 경로는 같은 값을 응답 본문으로 돌려주므로,
        # 비동기 경로로 승인하는 쪽(admin_be)도 이 값으로 신청 기록을 채운다.
        result = {
            "uid": ctx.get("uid"), "gid": ctx.get("gid"),
            "pod_name": ctx.get("pod_name"), "node": ctx.get("node"),
            "ports": ctx.get("allocated_ports") or [],
        }
        if kind == "migrate":
            skipped = bool(ctx.get("skipped"))
            result.update(status="skipped" if skipped else "migrated", reason=ctx.get("skip_reason"),
                          from_node=ctx.get("from_node"), to_node=None if skipped else ctx.get("node"),
                          old_pod_name=ctx.get("old_pod_name"), old_pod_cleanup=ctx.get("old_pod_cleanup"))
            if skipped:
                result.update(pod_name=None, node=None, ports=[])
        _main.save_job_result(JOB_ACTIONS[kind].value, request_id, result)
        if kind == "provision" and _main.VERIFY_MODE == "full":
            # 진행 상황은 컨테이너 생성 직후 "컨테이너 생성 완료"로 한 번 기록되고 접근 시험이 그 뒤에 돈다.
            # 시험까지 끝났다는 것을 화면에 남긴다. 표시용이라 실패해도 결과 기록은 계속한다.
            try:
                _main.set_pod_creation_status(request_id, "ready", "컨테이너 생성·접근 확인 완료")
            except Exception:
                _main.app.logger.warning("[JOB] pod status update failed", exc_info=True)
    _record_job_result(kind, request_id, username, {
        "phase": phase.value, "error_code": error_code, "error_detail": error_detail,
        "pod_name": ctx.get("pod_name"), "node_name": ctx.get("node") or ctx.get("pod_node_name"),
    })

def _record_job_result(kind, request_id, username, result):
    """작업 결과 행을 남긴 뒤에만 입력을 지운다. 결과 행 기록이 실패하면 입력을 결과와 함께 "done"으로
    남겨 다음 바퀴에 결과 행만 다시 기록한다. 입력부터 지우면 제어기가 그 작업을 입력 없는 작업으로 보고
    실패로 기록해, 실제로 성공한 작업이 실패로 남는다."""
    action = JOB_ACTIONS[kind]
    try:
        _main.log_operation(request_id=request_id, username=username, action=action,
                      pod_name=result.get("pod_name"), node_name=result.get("node_name"),
                      phase=Phase(result["phase"]), error_code=result.get("error_code"),
                      error_detail=result.get("error_detail"), raise_errors=True)
    except Exception:
        _main.app.logger.exception(f"[JOB] result row write failed, will retry: {kind} request_id={request_id}")
        try:
            _main.mark_job_done(action.value, request_id, result)
        except Exception:
            _main.app.logger.exception("[JOB] job result save failed")
        return
    try:
        _main.delete_job_input(action.value, request_id)
    except Exception:
        _main.app.logger.warning("[JOB] job input delete failed", exc_info=True)

def _compensate_provision(kind, job, ctx, done):
    """생성 작업이 계정을 새로 만든 뒤 그다음 단계에서 실패하면 그 계정을 되돌린다. baseline에서는 admin_be가
    같은 보상을 하며, 같은 조건(노드를 모르거나 같은 사용자의 컨테이너가 남아 있으면 보류)을 따른다.
    계정 단계 안에서 실패한 경우는 그 단계가 이미 되돌렸다. 제안 시스템은 회수 때 홈을 보존하므로 여기서도
    홈은 지우지 않는다(이전 회수에서 보존된 같은 이름의 홈일 수 있다). 결과는 작업 결과 행에 함께 남긴다."""
    if kind != "provision" or "step_create_krb5_principal" not in done:
        return None
    comp = {"request_id": ctx["request_id"], "username": job["username"],
            "node_name": ctx.get("node"), "pod_name": ctx.get("pod_name")}
    try:
        for step in (_main.step_check_account_revocable, _main.step_delete_account, _main.step_remove_krb5):
            step(comp)
    except _main.StepFailed as e:
        code = e.body.get("error") if isinstance(e.body, dict) else "STEP_FAILED"
        _main.app.logger.warning(f"[JOB] 계정 되돌리기 {code}: request_id={ctx['request_id']}")
        return f"held:{code}" if code in ("ACCOUNT_NODE_UNKNOWN", "ACCOUNT_IN_USE") else f"failed:{code}"
    except Exception as e:
        _main.app.logger.exception(f"[JOB] 계정 되돌리기 실패: request_id={ctx['request_id']}")
        return f"failed:{type(e).__name__}"
    return "account_removed"

def _interrupt_baseline_job(kind, request_id, username, job, ctx, done):
    """baseline에서 제어기가 작업 도중 죽었다가 인수한 작업. 운영에서 config-server가 요청 처리 중 죽으면
    admin_be는 연결 오류만 받고 어느 노드에 배포하던 중이었는지 모른 채 뒷정리한다(노드를 모르면 계정 삭제
    보류). 이어서 실행하지 않고 그 결과를 그대로 재현한다 — 노드·Pod 정보를 넘기지 않는다."""
    _main.app.logger.warning(f"[JOB] baseline: {kind} request_id={request_id} 중단된 작업 — 이어하지 않고 종료")
    blind = {k: v for k, v in ctx.items() if k not in ("node", "pod_name", "pod_node_name")}
    detail = {"interrupted_after": done[-1], "compensation": _compensate_provision(kind, job, blind, done)}
    # 노드·Pod를 뺀 blind를 넘긴다. 원본을 넘기면 운영에서 재현하려던 "어디에 배포하던 중이었는지
    # 모르는 상태"가 깨져, 결과 행에 노드·Pod가 남는다.
    _finish_job(kind, request_id, username, Phase.FAIL, "INTERRUPTED",
                json.dumps(detail, ensure_ascii=False, default=str), blind)

def run_job(kind, request_id, username, job_id=None):
    """등록된 작업 하나를 단계 함수로 끝까지 실행하고 작업 단위 끝 행을 남긴다. 실행하는 동안의 모든
    기록에는 작업 번호(job_id)가 붙는다."""
    token = current_job_id.set(job_id)
    try:
        _run_job(kind, request_id, username, job_id)
    finally:
        current_job_id.reset(token)

def _release_lease(job_id):
    if job_id is None:
        return
    try:
        job_control.release(job_id)
    except Exception:
        _main.app.logger.warning("[JOB] lease release failed", exc_info=True)

def _run_job(kind, request_id, username, job_id=None):
    action = JOB_ACTIONS[kind]

    # 소유권 선점. 다른 살아있는 제어기가 잡고 있으면 이번 바퀴는 물러난다. 제어기가 죽었던
    # 작업은 lease가 만료된 뒤 여기서 인수되어, 끝난 단계를 건너뛰고 이어서 실행된다.
    done, saved = [], {}
    if job_id is not None:
        try:
            lease = job_control.claim(job_id, request_id, action.value)
        except Exception:
            _main.app.logger.warning(f"[JOB] lease claim failed {kind} request_id={request_id} — 다음 바퀴에 재시도",
                               exc_info=True)
            return
        if lease is None:
            return
        done, saved = lease

    stored = _main.load_job_input(action.value, request_id)
    if stored is None:
        # 끝 행이 이미 있으면 방금 끝난 작업을 한 번 더 집은 것이다. 실패로 적으면 admin_be가 마지막 행만
        # 보고 성공한 작업을 실패로 되돌린다(신청은 옛 Pod 이름을 가리킨 채 남고 새 Pod는 고아가 된다).
        if job_id is not None and _main.job_end_exists(action.value, request_id, job_id):
            _main.app.logger.info(f"[JOB] 이미 끝난 작업 재선택 — 건너뜀: {kind} request_id={request_id} job_id={job_id}")
            _release_lease(job_id)
            return
        _finish_job(kind, request_id, username, Phase.FAIL, "JOB_INPUT_MISSING",
                    "job input not found in Redis")
        _release_lease(job_id)
        return
    if stored.get("state") == "done":
        # 지난번에 끝났지만 결과 행 기록이 실패한 작업 — 단계는 다시 돌리지 않고 결과 행만 기록한다.
        _record_job_result(kind, request_id, username, stored["result"])
        _release_lease(job_id)
        return
    _main.mark_job_running(action.value, request_id)

    job = stored["job"]
    ctx = _job_ctx(kind, request_id, job)
    ctx.update(saved)
    done = list(done)
    if done and _is_baseline():
        _interrupt_baseline_job(kind, request_id, username, job, ctx, done)
        _release_lease(job_id)
        return
    if done:
        _main.app.logger.info(f"[JOB] resume {kind} request_id={request_id} after {done[-1]}")
    else:
        _main.app.logger.info(f"[JOB] start {kind} request_id={request_id}")
    try:
        for step in _main._job_steps(kind, job):
            if ctx.get("skipped"):
                break  # 마이그레이션에서 옮길 이유가 없다고 판정되면 남은 단계를 돌리지 않는다
            name = step.__name__
            if name in _main.ALWAYS_RERUN:
                # 이어받기에서도 매번 다시 실행한다 — done에 넣지 않는다.
                # 실행 자체는 공통 실행기를 거쳐야 재시도·사전 정리·결과 불명 처리가 똑같이 걸린다.
                _execute_step(step, ctx, kind, request_id, username)
                continue
            partner = _main.DEFER_DONE.get(name)
            if (partner in done) if partner else (name in done):
                continue
            _execute_step(step, ctx, kind, request_id, username)
            if name not in _main.DEFER_DONE:
                done.append(name)
                if job_id is not None:
                    job_control.record_step(job_id, done, _saved_ctx(ctx))
    except LeaseLost:
        _main.app.logger.warning(f"[JOB] lease lost {kind} request_id={request_id} — 새 소유자가 이어간다")
        return
    except _StepDegraded as e:
        cause = e.cause
        body = cause.body if isinstance(cause, _main.StepFailed) else {"error": str(cause)}
        # 판정 불능·재시도 소진 — 자원을 임의로 되돌리지 않고 근거를 남겨 관리자에게 넘긴다.
        detail = {"degraded": True, "step": e.step_name, "reason": e.reason, "error": body,
                  "inspect": f"{e.step_name} 대상 자원의 실제 상태를 확인한 뒤 재등록 또는 수동 정리"}
        _finish_job(kind, request_id, username, Phase.UNKNOWN if e.unknowable else Phase.FAIL,
                    "DEGRADED", json.dumps(detail, ensure_ascii=False, default=str), ctx)
        _release_lease(job_id)
        return
    except _main.StepFailed as e:
        code = e.body.get("error") if isinstance(e.body, dict) else None
        detail = {"error": e.body, "compensation": _compensate_provision(kind, job, ctx, done)}
        if e.unknown:
            detail["unknown"] = True
        _finish_job(kind, request_id, username, _end_phase(e.unknown),
                    str(code)[:64] if code else "STEP_FAILED",
                    json.dumps(detail, ensure_ascii=False, default=str), ctx)
        _release_lease(job_id)
        return
    except Exception as e:
        _main.app.logger.exception(f"[JOB] {kind} request_id={request_id} unexpected error")
        detail = {"error": str(e), "compensation": _compensate_provision(kind, job, ctx, done)}
        _finish_job(kind, request_id, username, _end_phase(_main._fail_phase(e) == Phase.UNKNOWN), "UNEXPECTED_ERROR",
                    json.dumps(detail, ensure_ascii=False, default=str), ctx)
        _release_lease(job_id)
        return
    _main.app.logger.info(f"[JOB] done {kind} request_id={request_id}")
    _finish_job(kind, request_id, username, Phase.SUCCESS, ctx=ctx)
    _release_lease(job_id)
