"""가상 계층 위에 고정 fixture 세 가지를 세우고 평가자가 옳게 판정하는지 본다.

수집기와 달리 이 파일은 대상 시스템의 선언을 읽어도 된다. 불일치 상황에서 "시스템은 성공이라고
적어 두었는데 평가자는 FAIL 을 냈다" 를 보이려면 두 값을 같은 자리에서 비교해야 하기 때문이다.
금지는 harness/virtual_probe.py 에만 걸린다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluator  # noqa: E402
from virtual_probe import virtual_collector  # noqa: E402

e2e = sys.modules["cs_e2e"]

USER = "exp-np-p"


class _Unreachable:
    """평가 경로가 끊긴 상태를 흉내 낸다. 자원을 지우지 않고 물어볼 길만 막는다."""

    def __getattr__(self, name):
        raise TimeoutError("virtual k8s client unreachable")


def _provision(e, rid):
    r = e.api.post("/operations/provision", json={"request_id": rid, "username": USER,
                                                  "account": {"passwd_base64": e2e.PW}})
    assert r.status_code == 202, r.get_json()
    e2e.tick(e)
    assert e2e.result(e, "provision", rid)["phase"] == "SUCCESS", e2e.rows(e, rid)
    return next(iter(e.v1.pods))


def _revoke(e, rid, pod_name):
    r = e.api.post("/operations/revoke", json={"request_id": rid, "username": USER,
                                               "pod_name": pod_name, "delete_account": True})
    assert r.status_code == 202, r.get_json()
    e2e.tick(e)
    assert e2e.result(e, "revoke", rid)["phase"] == "SUCCESS", e2e.rows(e, rid)


def test_normal_creation_is_pass(env):
    """정상 fixture: 생성을 끝낸 자원을 읽으면 다섯 검사가 전부 PASS 다."""
    e = env
    _provision(e, "901")
    out = evaluator.evaluate_creation(virtual_collector(e), username=USER)
    assert out["verdict"] == evaluator.PASS, out
    assert set(out["checks"].values()) == {evaluator.PASS}


def test_revoked_reclamation_is_pass(env):
    """차단 fixture: 계정까지 거둔 뒤에는 네 검사가 전부 막혔음으로 확인된다."""
    e = env
    pod_name = _provision(e, "902")
    _revoke(e, "902", pod_name)
    out = evaluator.evaluate_reclamation(virtual_collector(e), username=USER)
    assert out["verdict"] == evaluator.PASS, out
    assert set(out["checks"].values()) == {evaluator.PASS}


def test_mismatch_between_declaration_and_access_is_fail(env):
    """불일치 fixture: 대상 시스템 몰래 Pod 를 없애면 선언은 성공인데 판정은 FAIL 이다."""
    e = env
    pod_name = _provision(e, "904")
    del e.v1.pods[pod_name]

    out = evaluator.evaluate_creation(virtual_collector(e), username=USER)
    assert out["verdict"] == evaluator.FAIL, out
    assert out["checks"]["container_identity"] == evaluator.FAIL
    assert out["checks"]["storage_access"] == evaluator.FAIL
    # 같은 시점에 대상 시스템은 여전히 성공을 선언하고 있다. 두 값이 어긋난 것이 이 실험의 대상이다.
    assert e2e.result(e, "provision", "904")["phase"] == "SUCCESS"


def test_collector_failure_is_unknown_not_fail(env):
    """평가자 고장: 자원이 정상인데도 못 물어봤으면 없는 위반을 만들지 않고 UNKNOWN 이다."""
    e = env
    _provision(e, "905")
    assert evaluator.evaluate_creation(virtual_collector(e), username=USER)["verdict"] == evaluator.PASS

    e.v1 = _Unreachable()
    out = evaluator.evaluate_creation(virtual_collector(e), username=USER)
    assert out["verdict"] == evaluator.UNKNOWN, out
    assert out["checks"]["container_identity"] == evaluator.UNKNOWN
    assert out["evidence"]["container_identity"]["collector_error"] == "TimeoutError"
    assert evaluator.FAIL not in out["checks"].values()
