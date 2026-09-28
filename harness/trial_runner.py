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

Fault Injector 는 아직 없다. 장애 등록이 붙을 자리는 submit 을 부르기 앞이다.

환경 판정은 포트 environment 로 받아서 기록의 environment 칸에 남긴다. open_trial 앞에서 한
번 부르므로 환경 확인이 스택에 남기는 흔적이 trial 의 시간창에 들어가지 않는다. 판정이 DIRTY
나 UNKNOWN 이어도 trial 은 그대로 진행한다. 그런 trial 은 버리지 않고 표시해서 따로 세며(R4),
세는 일은 Metrics Analyzer 가 한다. environment 에도 기본값을 두지 않는다. 기본값이 있으면
환경을 확인하지 않은 trial 이 CLEAN 처럼 보여서 R4 의 구분이 조용히 사라진다. 복원과 잔재
검사를 하는 Environment Resetter 본체는 아직 없고, 붙을 자리는 close_trial 을 부른 뒤다.
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


def run_trial(conn, *, trial_id, method, server_group, operation, horizon_sec,
              repetition, revisions, username, scenario_id=None,
              submit, advance, declaration, collect, clock, save, environment):
    """trial 하나를 끝까지 진행하고 관측 기록을 돌려준다.

    open_trial 은 trial 하나에 한 번만 부른다. 대상 시스템이 몇 번 재시도하든 그것은 같은
    작업의 시도이고, 재전송을 별도 trial 로 세면 모든 비율의 분모가 부풀어 오른다.
    """
    try:
        evaluate = _EVALUATORS[operation]
    except KeyError:
        raise ValueError(
            f"operation 은 {sorted(_EVALUATORS)} 중 하나여야 한다: {operation!r}") from None

    environment_record = _judge_environment(environment)

    trial.open_trial(conn, trial_id=trial_id, method=method, server_group=server_group,
                     operation=operation, horizon_sec=horizon_sec, repetition=repetition,
                     revisions=revisions, scenario_id=scenario_id)

    # 장애 등록 자리. Fault Injector 가 생기면 여기서 시나리오를 켠다.
    t_submitted = clock()
    request_id = submit()
    trial.bind_request(conn, trial_id, request_id)

    declared_value = None
    t_declared = None
    at_declaration = None
    while clock() - t_submitted < horizon_sec:
        advance()
        if declared_value is None:
            value = declaration()
            if value is not None:
                declared_value = value
                t_declared = clock()
                at_declaration = evaluate(collect, username=username)

    t_horizon_end = clock()
    at_horizon = evaluate(collect, username=username)

    # 선언이 성립한 상태에서 판정이 PASS 인 첫 확인 지점이다. 두 시각의 최대값이 아니다.
    # 나중의 복구가 앞서 있었던 잘못된 선언을 지우지 않으므로 at_declaration 은 그대로 둔다.
    t_verified = None
    if at_declaration is not None and at_declaration["verdict"] == evaluator.PASS:
        t_verified = t_declared
    elif declared_value is not None and at_horizon["verdict"] == evaluator.PASS:
        t_verified = t_horizon_end

    trial.close_trial(conn, trial_id)
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
        "independent_verdict": {"at_declaration": at_declaration, "at_horizon": at_horizon},
        "environment": environment_record,
    }
    # 저장이 실패하면 삼키지 않고 올린다. 저장하지 못한 trial 을 성공처럼 끝내면 결측이 보이지 않는다.
    save(record)
    return record
