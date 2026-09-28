"""resetter.environment_for 판정 규칙. test_inventory 의 가짜 스택을 그대로 쓴다."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resetter  # noqa: E402
import system  # noqa: E402
from test_inventory import E2E, HOST, ME, NS, PREFIX, FakeStack, _obj  # noqa: E402

OTHER_M = "exp-fu-m0001"


@pytest.fixture
def stack(monkeypatch):
    s = FakeStack()
    # 기본은 이 trial 의 사용자 자원만 있는 스택이다.
    for kind in s.objects:
        s.objects[kind] = [o for o in s.objects[kind] if ME in o["metadata"]["name"]]
    for table in s.tables:
        s.tables[table] = [r for r in s.tables[table] if r[0] == ME]
    monkeypatch.setattr(system, "stack_kube", s.kube)
    monkeypatch.setattr(system, "stack_sql", s.sql)
    return s


def _judge():
    return resetter.environment_for(HOST, NS, PREFIX, username=ME)()


def test_own_resources_are_not_residue(stack):
    verdict, ev = _judge()
    assert verdict == resetter.UNKNOWN
    assert ev["residue"] == [] and ev["errors"] == {} and ev["unchecked"]


def test_other_measure_user_pod_is_dirty(stack):
    stack.objects["pods"].append(_obj(f"ailab-{OTHER_M}-33333333", app="ailab-guest", username=OTHER_M))
    verdict, ev = _judge()
    assert verdict == resetter.DIRTY
    assert ev["residue"] == [{"user": OTHER_M, "kind": "pod", "name": f"ailab-{OTHER_M}-33333333"}]


def test_failed_lookup_is_unknown(stack):
    stack.kube_fail.add("services")
    verdict, ev = _judge()
    assert verdict == resetter.UNKNOWN and "service" in ev["errors"]


def test_e2e_user_is_dirty(stack):
    stack.objects["pods"].append(_obj(f"ailab-{E2E}-22222222", app="ailab-guest", username=E2E))
    verdict, ev = _judge()
    assert verdict == resetter.DIRTY and ev["e2e_users"] == 1


def test_never_clean_while_unchecked(stack):
    assert resetter.UNCHECKED
    # 스택 단위로 깨끗한 두 경우(자원 없음, 자기 자원만) 모두 CLEAN 이 아니다.
    assert _judge()[0] != resetter.CLEAN
    for kind in stack.objects:
        stack.objects[kind] = []
    for table in stack.tables:
        stack.tables[table] = []
    assert _judge()[0] != resetter.CLEAN
