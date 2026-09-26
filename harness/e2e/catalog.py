"""catalog.yaml 읽기와 형식 검사. 시험 사례의 유일한 원본이다."""
import pathlib
import re

import yaml

CATALOG = pathlib.Path(__file__).with_name("catalog.yaml")

# 단계 이름 → 값이 가리키는 대상의 종류. runner.py의 구현과 1:1이다(test_e2e_catalog가 확인).
VERBS = {
    "user", "apply", "approve", "reject", "cancel", "reclaim_container", "reclaim_account",
    "deactivate", "reactivate", "migrate", "wait", "expect_status", "expect_user", "remember_uid",
    "expect_codes", "expect_pod", "expect_node_changed", "fault", "heal", "sleep", "wait_job", "wait_codes",
}
FAULT_VERBS = {"fault"}
_TRANSITION = re.compile(r"^[A-Z]+>[A-Z]+$")


class CatalogError(ValueError):
    pass


def load(path=CATALOG):
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    validate(data)
    return data


def validate(data):
    ids = set()
    for case in data.get("cases", []):
        cid = case.get("id")
        if not cid or cid in ids:
            raise CatalogError(f"사례 id가 없거나 겹침: {cid}")
        ids.add(cid)
        for t in case.get("covers", []):
            if not _TRANSITION.match(t):
                raise CatalogError(f"{cid}: 전이 표기는 'FROM>TO' 형식이어야 함: {t}")
        kinds = [k for k in ("steps", "junit", "pytest") if k in case]
        if len(kinds) != 1:
            raise CatalogError(f"{cid}: steps·junit·pytest 중 정확히 하나만 있어야 함 (현재 {kinds})")
        for step in case.get("steps", []):
            if not isinstance(step, dict) or len(step) != 1:
                raise CatalogError(f"{cid}: 단계는 키 하나짜리 사전이어야 함: {step}")
            verb = next(iter(step))
            if verb not in VERBS:
                raise CatalogError(f"{cid}: 모르는 단계 {verb}")
    for code, reason in (data.get("waived_codes") or {}).items():
        if not reason or not str(reason).strip():
            raise CatalogError(f"면제 코드 {code}에 이유가 없음")


def is_fault_case(case):
    return any(next(iter(s)) in FAULT_VERBS for s in case.get("steps", []))


def real_cases(data):
    return [c for c in data["cases"] if "steps" in c]
