"""E2E 사례 목록이 빠짐없는지 클러스터 없이 확인한다.

- admin_be 신청 전이 표의 모든 전이가 어느 사례의 covers에 있다.
- config-server의 오류 코드가 사례·시험 코드·면제 목록 중 하나에 있다(면제는 이유 필수, 낡은 면제 금지).
- junit으로 가리킨 admin_be 시험이 실제로 있다.

admin_be 저장소는 ADMIN_BE_DIR(기본: 이 저장소 옆의 admin_be)에서 찾는다. CI는 E2E_REQUIRE_ADMIN_BE=1로
없으면 실패시키고, 로컬에서 없으면 그 검사만 건너뛴다.
"""
import inspect
import os
import pathlib
import re

import pytest
import yaml

from e2e import catalog
from e2e.runner import Context

ROOT = pathlib.Path(__file__).resolve().parent.parent
ADMIN_BE = pathlib.Path(os.environ.get("ADMIN_BE_DIR", ROOT.parent / "admin_be"))

# 오류 코드로 보는 문자열: 대문자_밑줄 상수 중 실패를 뜻하는 꼬리를 가진 것.
_CODE = re.compile(r'"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)"')
_FAIL_TAIL = re.compile(r"(_FAILED|_MISSING|_MISMATCH|_CONFLICT|_TIMEOUT|_NOT_FOUND|_UNKNOWN|_EXHAUSTED|_EXISTS"
                        r"|_INVALID|_INVALID_RESPONSE|_DENIED|_REFUSED|_UNREADABLE|_LOST|_BUSY|_LOCKED|_ERROR"
                        r"|_REJECTED|_UNAVAILABLE|_EXPIRED)$|^DEGRADED$")


def _admin_be_or_skip():
    if ADMIN_BE.is_dir():
        return ADMIN_BE
    if os.environ.get("E2E_REQUIRE_ADMIN_BE") == "1":
        pytest.fail(f"admin_be 저장소가 없음: {ADMIN_BE}")
    pytest.skip("admin_be 저장소가 옆에 없어 건너뜀")


def config_server_codes():
    codes = set()
    for path in (ROOT / "config-server").rglob("*.py"):
        if "tests" in path.parts:
            continue
        codes |= {c for c in _CODE.findall(path.read_text(encoding="utf-8")) if _FAIL_TAIL.search(c)}
    return codes


def codes_in_tests(codes):
    text = "\n".join(p.read_text(encoding="utf-8") for p in
                     [*(ROOT / "config-server" / "tests").glob("*.py"), *(ROOT / "harness").glob("test_*.py")]
                     if p.name != pathlib.Path(__file__).name)
    return {c for c in codes if re.search(rf"\b{c}\b", text)}


@pytest.fixture(scope="module")
def data():
    return catalog.load()


def test_catalog_is_valid(data):
    assert data["cases"]


def test_every_step_verb_has_an_implementation():
    implemented = {name[3:] for name, _ in inspect.getmembers(Context, inspect.isfunction) if name.startswith("do_")}
    assert implemented == catalog.VERBS


def test_every_lifecycle_transition_is_covered(data):
    table = yaml.safe_load((_admin_be_or_skip() / "src/main/resources/lifecycle-transitions.yaml").read_text())
    transitions = {f"{src}>{dst}" for src, targets in table.items() for dst in targets}
    covered = {t for case in data["cases"] for t in case.get("covers", [])}
    assert transitions - covered == set(), "사례가 없는 전이"
    assert covered - transitions == set(), "전이 표에 없는 전이를 covers에 적음"


def test_junit_references_exist(data):
    root = _admin_be_or_skip() / "src/test/java"
    for case in data["cases"]:
        if "junit" not in case:
            continue
        cls, method = case["junit"].split("#")
        files = list(root.rglob(f"{cls}.java"))
        assert files, f"{case['id']}: {cls}.java 없음"
        assert re.search(rf"\bvoid {method}\(", files[0].read_text(encoding="utf-8")), f"{case['id']}: {method} 없음"


def test_every_error_code_is_covered_tested_or_waived(data):
    codes = config_server_codes()
    case_codes = {c for case in data["cases"] for c in case.get("codes", [])}
    waived = set(data.get("waived_codes") or {})
    tested = codes_in_tests(codes)

    assert case_codes - codes == set(), "config-server에 없는 코드를 사례에 적음(오타?)"
    assert waived - codes == set(), "config-server에 없는 코드를 면제함"
    assert waived & (tested | case_codes) == set(), "시험이 생긴 코드는 면제 목록에서 뺀다"
    assert codes - case_codes - tested - waived == set(), "시험도 사례도 면제도 없는 오류 코드"


def test_fault_cases_are_detected(data):
    assert {c["id"] for c in data["cases"] if catalog.is_fault_case(c)} >= {"F01", "F02", "F03", "F04", "F05"}
