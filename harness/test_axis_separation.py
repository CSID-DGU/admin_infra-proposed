"""네 개의 결과 축이 Trial Runner 안에서 섞이지 않는다는 사실을 계속 확인하는 회귀 시험.

네 축은 명령 결과(command_outcome)와 검증 결과(verification_result)와 시스템 선언
(system_declaration)과 독립 판정(independent_verdict)이다. 이 네 값을 각각 다른 칸에 적고
한 칸을 다른 칸에서 유도하지 않는 것이 이 실험의 생명선이다. 근거는 ADR-000 의 1항과
ADR-004 에 있다.

Trial Runner 는 네 축이 한자리에 모이는 첫 지점이다. 여기서 축을 한 번 섞으면 그 뒤의
Metrics Analyzer 가 아무리 정확해도 되돌릴 수 없다. 사람이 코드를 읽어서 확인하는 방식으로는
이 규칙이 몇 달을 버티지 못하므로, 시험으로 바꿔서 회귀 그물에 건다.

문자열 검색이 아니라 ast 로 판정한다. 주석과 문서 문자열은 금지 대상이 아니라 설명이므로,
본문을 훑는 방식으로는 진짜 위반과 설명을 구분하지 못한다.
"""
import ast
from pathlib import Path

ADR = ("근거는 docs/adr/ADR-000-philosophy.md 와 "
       "docs/adr/ADR-004-evaluator-path-independence.md 에 있다. "
       "규칙을 우회하기 전에 그 문서를 먼저 읽어야 한다.")

SOURCE = Path(__file__).resolve().parent / "trial_runner.py"

# 대상 시스템이 operation_log 에 남기는 두 축. 하네스는 harness/trial.py 의 events_of 로
# 회수하기만 하므로, 반환 기록에 이 이름의 칸을 두지 않는다.
SYSTEM_OWNED_AXES = frozenset({"command_outcome", "verification_result"})

# 평가 함수에 실리면 안 되는 이름 조각. 평가자가 시스템의 자기 진술을 보고 판정하게 된다.
DECLARATION_WORDS = ("declaration", "declared", "access_state", "probe", "verification")

# 파생 지표를 가리키는 이름 조각. 밑줄로 끊은 낱말 단위로 견주므로 separate 같은 낱말에는
# 걸리지 않는다.
DERIVED_METRIC_WORDS = frozenset({"wrong", "rate", "ratio", "pct", "percent"})


def _tree():
    return ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))


def _dict_keys(tree):
    """소스에 나오는 모든 dict 리터럴의 문자열 키를 모은다."""
    keys = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys += [k.value for k in node.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    return keys


def _bound_names(tree):
    """대입과 인자로 이름이 붙는 자리를 모두 모은다."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.append(node.id)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
    return names


def _callee_name(node):
    """Call 노드에서 부르는 쪽의 이름을 뽑는다. 못 뽑으면 빈 문자열이다."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _evaluate_calls(tree):
    """평가 함수를 부르는 자리. 이름에 evaluate 가 들어간 호출 전부를 본다.

    이 모듈은 evaluate_creation 과 evaluate_reclamation 을 표로 묶어서 고른 뒤 지역 이름으로
    부르므로, 호출 대상을 이름 하나로 못박으면 실제 호출 자리를 놓친다.
    """
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and "evaluate" in _callee_name(n)]


def _tokens(name):
    return set(name.lower().strip("_").split("_"))


def test_return_record_has_no_system_owned_axes():
    found = SYSTEM_OWNED_AXES & set(_dict_keys(_tree()))
    assert not found, (
        f"하네스가 직접 기록하는 자리에 대상 시스템 소유의 축을 두었다: {sorted(found)}. "
        f"명령 결과와 검증 결과는 대상 시스템이 operation_log 에 남기고 하네스는 "
        f"harness/trial.py 의 events_of 로 회수한다. 빈 칸을 만들어 두면 언젠가 다른 값에서 "
        f"유도해 채우게 되고, 그 순간 네 축이 섞인다. {ADR}")


def test_evaluate_calls_take_no_system_declaration():
    offenders = []
    for call in _evaluate_calls(_tree()):
        passed = []
        for arg in call.args:
            if isinstance(arg, ast.Name):
                passed.append(arg.id)
            elif isinstance(arg, ast.Attribute):
                passed.append(arg.attr)
        for kw in call.keywords:
            if kw.arg is not None:
                passed.append(kw.arg)
            if isinstance(kw.value, ast.Name):
                passed.append(kw.value.id)
            elif isinstance(kw.value, ast.Attribute):
                passed.append(kw.value.attr)
        for name in passed:
            if any(word in name.lower() for word in DECLARATION_WORDS):
                offenders.append(f"{_callee_name(call)}(... {name} ...) 줄 {call.lineno}")
    assert not offenders, (
        f"평가 호출에 대상 시스템의 자기 진술을 실었다: {offenders}. "
        f"평가자가 선언을 보고 판정하면 시스템 선언 축과 독립 판정 축이 같은 정보원을 쓰게 "
        f"되어, 잘못된 완료 선언이 구조적으로 잡히지 않는다. {ADR}")


def test_does_not_compute_derived_metrics():
    tree = _tree()
    offenders = sorted({name for name in _dict_keys(tree) + _bound_names(tree)
                        if _tokens(name) & DERIVED_METRIC_WORDS})
    assert not offenders, (
        f"Trial Runner 가 파생 지표를 계산하거나 담으려 했다: {offenders}. "
        f"비율은 분모를 어떻게 잡느냐에 따라 달라지므로 Metrics Analyzer 한곳에서만 정한다. "
        f"관측과 집계를 한자리에서 하면 분모를 바꾼 흔적이 남지 않는다. {ADR}")


def test_does_not_fold_timestamps_with_max():
    tree = _tree()
    offenders = [f"줄 {n.lineno}" for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and _callee_name(n) == "max"]
    assert not offenders, (
        f"시각 계산에 max 를 썼다: {offenders}. t_verified 는 서로 어긋날 수 있는 두 "
        f"타임스탬프의 최대값이 아니라, 선언이 성립한 상태에서 독립 판정이 PASS 인 첫 확인 "
        f"지점이다. 최대값으로 합치면 나중의 복구가 앞서 있었던 잘못된 선언을 지운다. {ADR}")
