"""평가자가 대상 시스템과 경로를 공유하지 않는다는 사실을 계속 확인하는 회귀 시험.

한 번 확인하고 끝내지 않는 이유는 평가자가 앞으로도 자란다는 데 있다. 실스택 수집기와
자격증명 취급이 붙는 동안 "대상 시스템의 access_state 를 참고하면 판정이 정확해진다" 는
판단이 반드시 한 번은 나오고, 그 변경은 지금 막아 두지 않으면 조용히 들어온다. 들어오면
평가자가 대상 시스템과 같은 정보원을 쓰게 되어 오류율이 구조적으로 0으로 나오고, 실험이
재려던 것을 재지 못한다.

문자열 검색이 아니라 ast 로 판정한다. 주석과 문서 문자열은 금지 대상이 아니라 설명이므로,
본문을 훑는 방식으로는 진짜 위반과 설명을 구분하지 못한다.
"""
import ast
from pathlib import Path

ADR = ("근거는 docs/adr/ADR-004-evaluator-path-independence.md 에 있다. "
       "규칙을 우회하기 전에 그 문서를 먼저 읽어야 한다.")

SOURCE = Path(__file__).resolve().parent / "evaluator.py"

# 대상 시스템의 모듈. 하나라도 import 하면 평가자와 피평가자가 같은 코드를 쓰게 된다.
FORBIDDEN_SYSTEM_MODULES = frozenset(
    {"main", "adapters", "application", "lifecycle_steps", "entrypoints"})

# 자원에 직접 닿는 모듈. 평가자는 수집기를 통해서만 바깥과 닿는다.
FORBIDDEN_RESOURCE_MODULES = frozenset({"subprocess", "requests", "kubernetes"})

# 대상 시스템의 자기 진술을 받아들이는 인자 이름. 자리를 만들면 언젠가 채워진다.
FORBIDDEN_ARG_WORDS = ("declaration", "access_state", "probe", "verification")


def _tree():
    return ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))


def _imported_roots(tree):
    """import 한 모듈 이름의 최상위 조각을 모은다."""
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _verdict_functions(tree):
    """평가자 모듈의 판정 함수들. 모듈 최상위 함수 전부가 여기에 해당한다."""
    return [n for n in tree.body if isinstance(n, ast.FunctionDef)]


def test_does_not_import_system_under_test():
    shared = _imported_roots(_tree()) & FORBIDDEN_SYSTEM_MODULES
    assert not shared, (
        f"평가자가 대상 시스템 모듈을 import 했다: {sorted(shared)}. "
        f"같은 코드를 쓰면 같은 오류에 함께 빠진다. {ADR}")


def test_does_not_reach_resources_directly():
    shared = _imported_roots(_tree()) & FORBIDDEN_RESOURCE_MODULES
    assert not shared, (
        f"평가자가 자원에 직접 닿는 모듈을 import 했다: {sorted(shared)}. "
        f"바깥과 닿는 일은 부르는 쪽이 넘긴 수집기가 맡는다. {ADR}")


def test_verdict_functions_take_no_system_declaration():
    tree = _tree()
    offenders = []
    for func in _verdict_functions(tree):
        args = func.args
        names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
        names += [a.arg for a in (args.vararg, args.kwarg) if a is not None]
        for name in names:
            if any(word in name for word in FORBIDDEN_ARG_WORDS):
                offenders.append(f"{func.name}({name})")
    assert not offenders, (
        f"판정 함수가 대상 시스템의 자기 진술을 받는 인자를 두었다: {offenders}. "
        f"정답은 독립 경로로 확인한 접근 가능성뿐이다. {ADR}")


def test_verdict_functions_have_no_default_collector():
    tree = _tree()
    offenders = []
    for func in _verdict_functions(tree):
        args = func.args
        positional = args.posonlyargs + args.args
        if positional and args.defaults and len(args.defaults) >= len(positional):
            offenders.append(f"{func.name}({positional[0].arg})")
    assert not offenders, (
        f"판정 함수의 수집기 인자에 기본값이 있다: {offenders}. "
        f"기본값이 있으면 부르는 쪽이 경로를 고르지 않아도 판정이 나오고, "
        f"어느 경로로 잰 값인지 나중에 알 수 없다. {ADR}")
