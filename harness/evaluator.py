"""접근 가능성 판정을 내리는 유일한 자리.

대상 시스템이 스스로 "완료했다" 고 적은 값을 여기서 읽지 않는다. 이 실험이 재려는 오류가
"시스템이 자기 상태를 잘못 안다" 이므로, 평가자가 그 시스템과 같은 정보원을 쓰면 같은
오류에 함께 빠져서 오류율이 구조적으로 0으로 나온다 (ADR-004). 그래서 함수 시그니처에
system_declaration 도 access_state 도 Probe 결과도 받는 자리를 두지 않는다. 자리를 만들어
두면 언젠가 채워지기 때문이다.

자원에 실제로 접근하는 일은 이 모듈이 하지 않고, 부르는 쪽이 넘긴 collect 가 한다. 판정
규칙과 자원 접근을 갈라 두어야 가상 계층에서는 가짜 자원 상태를, 실스택에서는 별도 경로를
꽂을 수 있다.

평가자 자신의 고장은 FAIL 이 아니라 UNKNOWN 이다. collect 가 예외를 던지거나 알 수 없는
값을 돌려주면 그 검사는 UNKNOWN 이 되고 예외 내용이 증거로 남는다. 못 물어본 것을 위반으로
세면 실제로는 없는 위반을 만들어 내기 때문이다.

실스택 수집기를 붙일 다음 사람이 읽을 자리를 여기에 둔다. 두 가지가 정해져 있다.

첫째, 실스택 수집기는 harness/system.py 의 server-state 경로를 쓴다. 대상 시스템은
kubectl exec 로 자기 자신을 확인하고 이쪽은 Ansible SSH 로 확인하므로, 두 판단이 같은
고장에 함께 빠지지 않는다.

둘째, 평가 전용 테스트 계정의 자격증명을 어떻게 전달할지는 아직 정해지지 않았다. 공개
레포의 실행 로그에 노출되면 안 된다는 조건만 정해져 있다. 가상 계층은 자격증명이 필요
없으므로 지금 당장 막히지는 않지만, 실스택 수집기를 붙이기 전에 사람이 결정해야 한다.

이 규칙들이 지켜지는지는 harness/test_evaluator_independence.py 가 ast 로 계속 확인한다.
"""

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"

# 생성 판정의 필수 검사 다섯 종. 논문이 정한 접근 경로와 1:1 로 대응한다.
CREATION_CHECKS = ("login", "home_read_write", "container_identity",
                   "storage_access", "endpoint")

# 회수 판정의 필수 검사 네 종. 각각 "막혔음" 을 확인했을 때 PASS 다.
RECLAMATION_CHECKS = ("login_blocked", "credential_blocked",
                      "container_blocked", "endpoint_blocked")


def _evaluate(collect, names, username):
    checks, evidence = {}, {}
    for name in names:
        try:
            result, detail = collect(name, username)
        except Exception as e:  # 평가자 쪽 고장. 차단으로 읽으면 없는 위반을 만든다
            checks[name] = UNKNOWN
            evidence[name] = {"collector_error": type(e).__name__, "message": str(e)}
            continue
        if result not in (PASS, FAIL, UNKNOWN):
            checks[name] = UNKNOWN
            evidence[name] = {"unexpected_result": repr(result), "detail": detail}
            continue
        checks[name] = result
        evidence[name] = detail
    values = set(checks.values())
    if FAIL in values:
        verdict = FAIL  # 위반을 이미 확인했으므로 UNKNOWN 이 섞여도 FAIL 이다
    elif values == {PASS}:
        verdict = PASS
    else:
        verdict = UNKNOWN
    return {"verdict": verdict, "checks": checks, "evidence": evidence}


def evaluate_creation(collect, *, username):
    """생성 판정. 다섯 경로가 전부 실제로 열려 있을 때만 PASS 다."""
    return _evaluate(collect, CREATION_CHECKS, username)


def evaluate_reclamation(collect, *, username):
    """회수 판정. 네 경로가 전부 막혔음을 확인했을 때만 PASS 다.

    보존 정책으로 홈 데이터가 남는 것은 회수 위반이 아니므로 데이터 존재 여부를 검사
    목록에 넣지 않는다. 보존 때문에 접근 경로가 살아 있다면 그것은 접근 검사가 잡는다.
    """
    return _evaluate(collect, RECLAMATION_CHECKS, username)
