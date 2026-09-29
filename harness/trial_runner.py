"""trial 한 번의 진행을 지휘하고 관측한 사실만 남기는 자리.

VASC 는 네 개의 결과 축을 각각 다른 칸에 적고 한 칸을 다른 칸에서 유도하지 않는다.
명령 결과(command_outcome)와 검증 결과(verification_result)는 대상 시스템이 operation_log
에 남기고, 하네스는 harness/trial.py 의 events_of 로 시간창을 잡아서 회수한다. 이 모듈이
직접 기록하는 축은 나머지 둘, 곧 시스템 선언(system_declaration)과 독립 판정
(independent_verdict)뿐이다. 그래서 반환 기록에 앞의 두 축을 담을 빈 칸을 만들지 않는다.
빈 자리를 만들어 두면 언젠가 다른 값에서 유도해 채우게 되고, 그 순간 측정 장치가 측정하려던
오류를 똑같이 저지른다 (ADR-004).

파생 지표는 계산하지 않는다. 잘못된 완료 선언률이나 검증 완료율 같은 비율은 분모를 어떻게
잡느냐에 따라 달라지므로 Metrics Analyzer 한곳에서만 정한다. 여기서 한 번 더 계산하면 같은
지표가 두 규칙을 갖게 되고, 두 값이 어긋났을 때 어느 쪽이 정본인지 말할 수 없다.

바깥과 닿는 일은 전부 포트로 주입받는다. 이 모듈은 HTTP 도 SQL 도 시계도 직접 부르지
않는다. 가상 계층에는 실제로 흐르는 600초가 없으므로 관측 구간을 가짜 시계로 압축해서
돌려야 하고, 그러려면 시간을 앞으로 보내는 책임이 이 모듈 바깥의 advance 구현에 있어야
한다.

저장도 포트로 받는다. 시스템 선언과 독립 판정은 반환 기록에만 있으므로, 부르는 쪽이 넘긴 save
를 반환 직전에 한 번 부른다. save 에 기본값을 두지 않는다. 기본값이 있으면 저장을 빠뜨린 호출이
조용히 통과하고, 그 trial 의 측정값은 흔적 없이 사라진다.

접근 가능성은 두 번 확인한다. 첫 완료 선언 시점과 관측 구간 H 의 끝이다. 한 번만 재면
"선언 시점에는 됐는데 H 안에 무너진" 경우와 "선언 시점부터 틀린" 경우를 가를 수 없다.

장애는 포트 fault 로 받는다(harness/fault_injector.py). submit 바로 앞에서 장전하고, close_trial
바로 뒤에 발동 여부를 읽어 기록의 fault 칸에 남긴다. fault 칸은 장애를 걸었다는 사실이라 평가에
쓰지 않는다(ADR-004). 도중에 예외가 나도 장전이 스택에 남아 다음 trial 에 걸리지 않게 finally 에서
푼다.

환경 판정은 포트 environment 로 받아서 기록의 environment 칸에 남긴다. open_trial 앞에서 한
번 부르므로 환경 확인이 스택에 남기는 흔적이 trial 의 시간창에 들어가지 않는다. 판정이 DIRTY
나 UNKNOWN 이어도 trial 은 그대로 진행한다. 그런 trial 은 버리지 않고 표시해서 따로 세며(R4),
세는 일은 Metrics Analyzer 가 한다. environment 에도 기본값을 두지 않는다. 기본값이 있으면
환경을 확인하지 않은 trial 이 CLEAN 처럼 보여서 R4 의 구분이 조용히 사라진다. 복원과 잔재
검사를 하는 Environment Resetter 본체는 아직 없고, 붙을 자리는 close_trial 을 부른 뒤다.

기록 스키마 v2 의 보조 칸(scenario, samples, snapshots, protection)은 모두 선택 인자로 받고,
안 넘기면 None 이나 빈 목록이다. snapshot 과 bystanders 쪽 측정이 예외를 올리면 그 칸에
{"error", "message"} 를 적고 trial 은 계속한다. 측정 보조 장치의 고장 때문에 trial 을 잃지 않기
위해서다. 명세의 analysis 는 옮기지 않는다. scenario 칸에는 id, aliases, spec_hash 만 적는다.
"""
import evaluator
import trial

CLEAN, DIRTY = "CLEAN", "DIRTY"

_EVALUATORS = {
    "CREATE": evaluator.evaluate_creation,
    "REVOKE": evaluator.evaluate_reclamation,
}


def _judge_environment(environment):
    """환경 판정을 한 번 받는다. 판정기의 고장은 DIRTY 가 아니라 UNKNOWN 이다.

    확인하지 못한 환경을 DIRTY 로 세면 없는 오염을 만들어 낸다 (evaluator._evaluate 와 같은 규칙).
    """
    try:
        verdict, evidence = environment()
    except Exception as e:
        return {"verdict": evaluator.UNKNOWN,
                "evidence": {"collector_error": type(e).__name__, "message": str(e)}}
    if verdict not in (CLEAN, DIRTY, evaluator.UNKNOWN):
        return {"verdict": evaluator.UNKNOWN,
                "evidence": {"unexpected_result": repr(verdict), "detail": evidence}}
    return {"verdict": verdict, "evidence": evidence}


def _auxiliary(call):
    """보조 측정을 한 번 부른다. 예외는 칸에 적고 올리지 않는다."""
    try:
        return call()
    except Exception as e:
        return {"error": type(e).__name__, "message": str(e)}


def run_trial(conn, *, trial_id, method, server_group, operation, horizon_sec,
              repetition, revisions, username, scenario_id=None,
              submit, advance, declaration, collect, clock, save, environment,
              start_state=None, fault=None, expected=None,
              scenario=None, sample_every=None, snapshot=None, bystanders=None):
    """trial 하나를 끝까지 진행하고 관측 기록을 돌려준다.

    open_trial 은 trial 하나에 한 번만 부른다. 대상 시스템이 몇 번 재시도하든 그것은 같은
    작업의 시도이고, 재전송을 별도 trial 로 세면 모든 비율의 분모가 부풀어 오른다.

    start_state 는 회수 trial 에만 있다. run_pair 가 앞선 생성 trial 의 판정을 넘기고, 짝 없이
    돈 회수 trial 은 None 으로 남아서 그 사실이 기록에 드러난다.

    expected 는 평가자가 관계 판정에 쓰는 기대값이다. 하네스가 제출한 신청과 사용자 이름에서
    나온 값만 담고, 대상 시스템이 기록한 값으로 채우지 않는다 (ADR-004). uid 는 AD 에서 독립적으로
    읽는 경로가 생기기 전까지 None 이다.

    scenario 는 scenario_spec.Spec 이다. sample_every 를 주면 선언 뒤 그 걸음 수마다 한 번 평가해서
    independent_verdict.samples 에 쌓는다. snapshot 은 자원 목록을 돌려주는 포트로 open_trial 앞과
    close_trial 뒤에 한 번씩 부른다. bystanders 는 방관자 target 목록이고, 보호 판정을 submit 앞과
    H 끝에 한 번씩 낸다.
    """
    try:
        evaluate = _EVALUATORS[operation]
    except KeyError:
        raise ValueError(
            f"operation 은 {sorted(_EVALUATORS)} 중 하나여야 한다: {operation!r}") from None
    if operation == "CREATE" and start_state is not None:
        raise ValueError(f"start_state 는 회수 trial 에만 있다. CREATE 에 넘어왔다: {start_state!r}")

    if sample_every is not None and (not isinstance(sample_every, int) or sample_every < 1):
        raise ValueError(f"sample_every 는 1 이상의 정수여야 한다: {sample_every!r}")
    scenario_record = None
    if scenario is not None:
        view = scenario.run_view()
        if scenario_id is not None and scenario_id != view["scenario_id"]:
            raise ValueError(f"scenario_id {scenario_id!r} 가 명세 {view['scenario_id']!r} 와 다르다")
        scenario_record = {"id": view["scenario_id"], "aliases": view["aliases"],
                           "spec_hash": scenario.spec_hash}

    def protection():
        return _auxiliary(lambda: evaluator.evaluate_protection(collect, bystanders=bystanders))

    target = {"username": username,
              "expected": {"home_suffix": f"/{username}", "uid": None, **(expected or {})}}

    environment_record = _judge_environment(environment)
    snapshot_before = _auxiliary(snapshot) if snapshot is not None else None
    protection_before = protection() if bystanders is not None else None

    trial.open_trial(conn, trial_id=trial_id, method=method, server_group=server_group,
                     operation=operation, horizon_sec=horizon_sec, repetition=repetition,
                     revisions=revisions, scenario_id=scenario_id)

    if fault is not None:
        fault.arm()
    try:
        t_submitted = clock()
        request_id = submit()
        trial.bind_request(conn, trial_id, request_id)

        declared_value = None
        t_declared = None
        at_declaration = None
        samples = []
        steps_after = 0
        while clock() - t_submitted < horizon_sec:
            advance()
            if declared_value is None:
                value = declaration()
                if value is not None:
                    declared_value = value
                    t_declared = clock()
                    at_declaration = evaluate(collect, target=target)
            elif sample_every is not None:
                steps_after += 1
                if steps_after % sample_every == 0:
                    samples.append({"at": clock(), "verdict": evaluate(collect, target=target)})

        t_horizon_end = clock()
        at_horizon = evaluate(collect, target=target)
        protection_after = protection() if bystanders is not None else None

        # 선언이 성립한 상태에서 판정이 PASS 인 첫 확인 지점이다. 두 시각의 최대값이 아니다.
        # 나중의 복구가 앞서 있었던 잘못된 선언을 지우지 않으므로 at_declaration 은 그대로 둔다.
        t_verified = None
        if at_declaration is not None and at_declaration["verdict"] == evaluator.PASS:
            t_verified = t_declared
        elif declared_value is not None and at_horizon["verdict"] == evaluator.PASS:
            t_verified = t_horizon_end

        trial.close_trial(conn, trial_id)
        snapshot_after = _auxiliary(snapshot) if snapshot is not None else None
        fault_record = fault.report() if fault is not None else None
    finally:
        if fault is not None:
            fault.disarm()
    # 환경 복원과 잔재 검사 자리. Environment Resetter 가 생기면 여기서 되돌린다.

    record = {
        "trial_id": trial_id,
        "method": method,
        "server_group": server_group,
        "operation": operation,
        "scenario_id": scenario_id,
        "horizon_sec": horizon_sec,
        "repetition": repetition,
        "request_id": request_id,
        "timestamps": {
            "submitted": t_submitted,
            "declared": t_declared,
            "verified": t_verified,
            "horizon_end": t_horizon_end,
        },
        "system_declaration": {"value": declared_value, "at": t_declared},
        "independent_verdict": {"at_declaration": at_declaration, "at_horizon": at_horizon,
                                "samples": samples},
        "environment": environment_record,
        "start_state": start_state,
        "fault": fault_record,
        "scenario": scenario_record,
        "snapshots": (None if snapshot is None
                      else {"before": snapshot_before, "after": snapshot_after}),
        "protection": (None if bystanders is None
                       else {"before": protection_before, "after": protection_after}),
        "evaluator_version": evaluator.VERSION,
    }
    # 저장이 실패하면 삼키지 않고 올린다. 저장하지 못한 trial 을 성공처럼 끝내면 결측이 보이지 않는다.
    save(record)
    return record


def run_pair(conn, *, create_trial_id, revoke_trial_id, method, server_group,
             horizon_sec, repetition, revisions, username,
             create_scenario_id=None, revoke_scenario_id=None,
             create_fault=None, revoke_fault=None,
             create_submit, create_declaration, revoke_submit, revoke_declaration,
             advance, collect, clock, environment, save, snapshot=None, bystanders=None):
    """생성 trial 에 이어서 같은 신청으로 회수 trial 을 돌리고 (생성 기록, 회수 기록) 을 돌려준다.

    근거는 docs/domains/experiment-records.md 의 "회수 trial 은 생성 trial 에 바로 이어 붙입니다 (R2)".
    생성 trial 이 선언 없이 끝났거나 판정이 FAIL 이어도 회수 trial 은 돈다. 회수는 측정이면서
    자원을 거두는 일이고, PASS 가 아닌 시작을 어떻게 셀지는 Metrics Analyzer 가 정한다. 생성
    trial 이 예외로 끝나면 그 예외를 그대로 올리고 회수 trial 은 시작하지 않는다.

    순서는 run_trial 두 번의 호출 순서로만 보장한다. 생성 trial 은 close_trial 과 save 까지 마친
    뒤에 돌아오므로 회수 trial 은 그 뒤에 열린다. 다만 두 시간창이 같은 밀리초에 맞닿는 경우는
    따로 막지 않는다. 회수 trial 앞의 환경 확인이 걸리는 시간으로 벌어진다고 보며, 가상 계층의
    sqlite 는 시각이 1초 단위라서 이 겹침을 시험으로 확인할 수도 없다.
    """
    common = dict(method=method, server_group=server_group, horizon_sec=horizon_sec,
                  repetition=repetition, revisions=revisions, username=username,
                  advance=advance, collect=collect, clock=clock, save=save,
                  environment=environment, snapshot=snapshot, bystanders=bystanders)

    created = run_trial(conn, trial_id=create_trial_id, operation="CREATE",
                        scenario_id=create_scenario_id, fault=create_fault, submit=create_submit,
                        declaration=create_declaration, **common)
    request_id = created["request_id"]

    def submit():
        # 두 trial 이 다른 신청을 가리키면 R2 의 짝이 성립하지 않는다.
        returned = revoke_submit(request_id)
        if returned != request_id:
            raise trial.TrialStateError(
                f"회수 제출이 신청 {returned} 를 돌려줬다. 생성 trial {create_trial_id} 의 신청은 {request_id} 다.")
        return returned

    revoked = run_trial(conn, trial_id=revoke_trial_id, operation="REVOKE",
                        scenario_id=revoke_scenario_id, fault=revoke_fault, submit=submit,
                        declaration=revoke_declaration,
                        start_state={
                            "creation_trial_id": create_trial_id,
                            "creation_verdict_at_horizon":
                                created["independent_verdict"]["at_horizon"]["verdict"],
                        }, **common)
    return created, revoked
