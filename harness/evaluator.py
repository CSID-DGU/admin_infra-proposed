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

# trial 기록의 evaluator_version 칸에 적힌다. 판정 규칙이나 결과 구조를 바꾸면 올린다.
VERSION = 2

# 생성 판정의 도메인 검사 일곱 종. 각 검사의 evidence 에는 관계 판정에 쓸 식별자를 담는다.
# login 과 compute_* 는 pod_uid, endpoint 는 backend_pod_uid, compute_uid 는 runtime_uid,
# compute_nfs 는 mount_source 다.
CREATION_CHECKS = ("login", "compute_uid", "storage_rw", "credential",
                   "compute_gpu", "compute_nfs", "endpoint")

# 회수 판정의 도메인 검사 네 종. 각각 "막혔음" 을 확인했을 때 PASS 다. 관계 판정은 없다.
RECLAMATION_CHECKS = ("login_blocked", "credential_blocked",
                      "compute_blocked", "endpoint_blocked")

# access_verdict 에 접는 관계. runtime_uid_vs_expected 는 기록만 하고 접지 않는다. 기대 uid 를
# 대상 시스템 기록이 아니라 AD 에서 독립적으로 읽는 경로(vasc-16)가 아직 없어서, 지금 접으면
# 모든 trial 이 UNKNOWN 이 된다. 그 경로가 생기면 여기에 더한다.
REQUIRED_RELATIONS = ("endpoint_to_pod", "mount_target")


def _fold(results):
    values = set(results)
    if FAIL in values:
        return FAIL  # 위반을 이미 확인했으므로 UNKNOWN 이 섞여도 FAIL 이다
    if values == {PASS}:
        return PASS
    return UNKNOWN


def _collect_all(collect, names, target):
    domains = {}
    for name in names:
        try:
            result, detail = collect(name, target)
        except Exception as e:  # 평가자 쪽 고장. 차단으로 읽으면 없는 위반을 만든다
            domains[name] = {"result": UNKNOWN,
                             "evidence": {"collector_error": type(e).__name__, "message": str(e)}}
            continue
        if result not in (PASS, FAIL, UNKNOWN):
            domains[name] = {"result": UNKNOWN,
                             "evidence": {"unexpected_result": repr(result), "detail": detail}}
            continue
        domains[name] = {"result": result, "evidence": detail}
    return domains


def _id(domains, name, key):
    evidence = domains[name]["evidence"]
    return evidence.get(key) if isinstance(evidence, dict) else None


def _relation(ok, evidence):
    """ok 가 None 이면 비교할 식별자를 얻지 못한 것이므로 UNKNOWN 이다."""
    result = UNKNOWN if ok is None else (PASS if ok else FAIL)
    return {"result": result, "evidence": evidence}


def _relations(domains, expected):
    """관계 판정. 수집기를 다시 부르지 않고 도메인 evidence 끼리만 비교한다."""
    backend = _id(domains, "endpoint", "backend_pod_uid")
    login_pod = _id(domains, "login", "pod_uid")
    source = _id(domains, "compute_nfs", "mount_source")
    suffix = expected.get("home_suffix")
    runtime_uid = _id(domains, "compute_uid", "runtime_uid")
    expected_uid = expected.get("uid")
    return {
        "endpoint_to_pod": _relation(
            None if backend is None or login_pod is None else backend == login_pod,
            {"backend_pod_uid": backend, "login_pod_uid": login_pod}),
        "mount_target": _relation(
            None if source is None or suffix is None else str(source).endswith(suffix),
            {"mount_source": source, "home_suffix": suffix}),
        "runtime_uid_vs_expected": _relation(
            None if runtime_uid is None or expected_uid is None
            else str(runtime_uid) == str(expected_uid),
            {"runtime_uid": runtime_uid, "expected_uid": expected_uid}),
    }


def evaluate_creation(collect, *, target):
    """생성 판정. 일곱 도메인과 필수 관계가 전부 PASS 일 때만 PASS 다.

    target 은 {"username", "expected"} 이다. expected 에는 하네스가 제출한 신청과 그 사용자에게서
    나온 값만 담고, 대상 시스템이 기록한 값을 담지 않는다 (ADR-004). 도메인이 전부 PASS 여도
    endpoint 가 다른 Pod 에 닿으면 사용자는 그 신청을 쓸 수 없으므로 FAIL 이다.
    """
    domains = _collect_all(collect, CREATION_CHECKS, target)
    relations = _relations(domains, target.get("expected") or {})
    verdict = _fold([d["result"] for d in domains.values()]
                    + [relations[name]["result"] for name in REQUIRED_RELATIONS])
    # verdict 는 v1 호환 키다. trial_runner 와 분석 경로가 이 키를 읽는다.
    return {"access_verdict": verdict, "domains": domains, "relations": relations,
            "verdict": verdict}


def evaluate_reclamation(collect, *, target):
    """회수 판정. 네 경로가 전부 막혔음을 확인했을 때만 PASS 다.

    보존 정책으로 홈 데이터가 남는 것은 회수 위반이 아니므로 데이터 존재 여부를 검사
    목록에 넣지 않는다. 보존 때문에 접근 경로가 살아 있다면 그것은 접근 검사가 잡는다.
    """
    domains = _collect_all(collect, RECLAMATION_CHECKS, target)
    verdict = _fold([d["result"] for d in domains.values()])
    return {"access_verdict": verdict, "domains": domains, "relations": {},
            "verdict": verdict}


def evaluate_protection(collect, *, bystanders):
    """보호 판정. 방관자 대상마다 생성 판정을 돌려 전부 PASS 일 때만 PASS 다.

    접근 판정과 다른 칸에 둔다. 방관자가 없으면 판정할 것이 없으므로 None 이다.
    """
    results = {b["username"]: evaluate_creation(collect, target=b) for b in bystanders}
    verdict = _fold([r["access_verdict"] for r in results.values()]) if results else None
    return {"protection_verdict": verdict, "bystanders": results}
