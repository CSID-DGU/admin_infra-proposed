"""evaluator.py 계약 검증. 수집기를 가짜로 세워서 판정 규칙만 시험한다."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluator  # noqa: E402

P, F, U = evaluator.PASS, evaluator.FAIL, evaluator.UNKNOWN

TARGET = {"username": "u1", "expected": {"home_suffix": "/u1", "uid": 50000}}

# 관계가 전부 PASS 로 나오는 evidence. 시험마다 필요한 칸만 덮어쓴다.
GOOD_EVIDENCE = {
    "login": {"pod_uid": "pod-a"},
    "compute_uid": {"pod_uid": "pod-a", "runtime_uid": 50000},
    "compute_nfs": {"pod_uid": "pod-a", "mount_source": "nas:/share/user/u1"},
    "endpoint": {"backend_pod_uid": "pod-a"},
}


def _collector(results, evidence=None, seen=None):
    """검사 이름별 결과를 돌려주는 가짜 수집기. 값이 예외면 그것을 던진다."""
    evidence = {**GOOD_EVIDENCE, **(evidence or {})}

    def collect(check, target):
        if seen is not None:
            seen.append((check, target))
        value = results[check]
        if isinstance(value, Exception):
            raise value
        return value, {"scope": check, **evidence.get(check, {})}
    return collect


def _all(names, value=P):
    return {name: value for name in names}


def _create(results=None, evidence=None, target=TARGET):
    results = {**_all(evaluator.CREATION_CHECKS), **(results or {})}
    return evaluator.evaluate_creation(_collector(results, evidence), target=target)


def test_creation_all_pass_is_pass():
    out = _create()
    assert out["access_verdict"] == P
    assert out["verdict"] == P  # v1 호환 키
    assert {r["result"] for r in out["domains"].values()} == {P}


def test_creation_one_fail_outweighs_the_rest():
    out = _create({"endpoint": F})
    assert out["access_verdict"] == F
    assert out["domains"]["endpoint"]["result"] == F


def test_creation_one_unknown_makes_the_whole_unknown():
    assert _create({"storage_rw": U})["access_verdict"] == U


def test_creation_fail_beats_unknown():
    """위반을 이미 확인했으므로 UNKNOWN 이 섞여도 FAIL 이다."""
    assert _create({"login": F, "endpoint": U})["access_verdict"] == F


def test_collector_failure_is_unknown_not_fail():
    """평가자가 못 물어본 것은 위반이 아니다."""
    out = _create({"compute_uid": TimeoutError("test client unreachable")})
    assert out["access_verdict"] == U
    assert out["domains"]["compute_uid"]["result"] == U
    assert "test client unreachable" in out["domains"]["compute_uid"]["evidence"]["message"]


def test_unexpected_result_is_unknown_and_kept_as_evidence():
    """알 수 없는 값을 성공으로 접으면 안 된다."""
    out = _create({"login": True})
    assert out["domains"]["login"]["result"] == U
    assert "True" in out["domains"]["login"]["evidence"]["unexpected_result"]


def test_every_check_is_collected_even_after_a_fail_and_gets_the_target():
    """앞선 검사가 FAIL 이어도 증거를 전부 모은다. 수집기는 사용자 이름이 아니라 대상을 받는다."""
    seen = []
    evaluator.evaluate_creation(_collector(_all(evaluator.CREATION_CHECKS, F), seen=seen), target=TARGET)
    assert tuple(c for c, _ in seen) == evaluator.CREATION_CHECKS
    assert all(t is TARGET for _, t in seen)


def test_result_is_json_serializable():
    assert json.loads(json.dumps(_create()))["access_verdict"] == P


# 관계 판정

def test_endpoint_to_pod_mismatch_fails_even_when_every_domain_passes():
    """논문 E1: endpoint 는 Pod B 에 닿고 나머지 증거는 Pod A 에서 나왔으면 사용자는 쓸 수 없다."""
    out = _create(evidence={"endpoint": {"backend_pod_uid": "pod-b"}})
    assert {r["result"] for r in out["domains"].values()} == {P}
    assert out["relations"]["endpoint_to_pod"]["result"] == F
    assert out["access_verdict"] == F


def test_endpoint_to_pod_without_an_identifier_is_unknown():
    out = _create(evidence={"login": {}})
    assert out["relations"]["endpoint_to_pod"]["result"] == U
    assert out["access_verdict"] == U


def test_endpoint_to_pod_survives_a_collector_error():
    """수집기가 죽어 evidence 가 식별자 없는 오류 기록이면 관계는 UNKNOWN 이다."""
    out = _create({"endpoint": ConnectionError("x")})
    assert out["relations"]["endpoint_to_pod"]["result"] == U


def test_mount_target_pass_fail_unknown():
    assert _create()["relations"]["mount_target"]["result"] == P
    wrong = _create(evidence={"compute_nfs": {"mount_source": "nas:/share/user/u10"}})
    assert wrong["relations"]["mount_target"]["result"] == F
    assert wrong["access_verdict"] == F
    missing = _create(evidence={"compute_nfs": {"mount_source": None}})
    assert missing["relations"]["mount_target"]["result"] == U
    assert missing["access_verdict"] == U


def test_runtime_uid_vs_expected_pass_fail_unknown():
    assert _create()["relations"]["runtime_uid_vs_expected"]["result"] == P
    no_expected = _create(target={"username": "u1", "expected": {"home_suffix": "/u1", "uid": None}})
    assert no_expected["relations"]["runtime_uid_vs_expected"]["result"] == U
    assert no_expected["access_verdict"] == P


def test_runtime_uid_mismatch_does_not_fold_into_access_verdict():
    """기대 uid 를 AD 에서 독립적으로 읽는 경로가 없으므로 기록만 하고 접지 않는다."""
    out = _create(evidence={"compute_uid": {"pod_uid": "pod-a", "runtime_uid": 50001}})
    assert out["relations"]["runtime_uid_vs_expected"]["result"] == F
    assert out["access_verdict"] == P


# 회수 판정

def _revoke(results=None):
    results = {**_all(evaluator.RECLAMATION_CHECKS), **(results or {})}
    return evaluator.evaluate_reclamation(_collector(results), target=TARGET)


def test_reclamation_passes_only_when_all_four_paths_are_blocked():
    out = _revoke()
    assert out["access_verdict"] == P and out["verdict"] == P
    assert set(out["domains"]) == set(evaluator.RECLAMATION_CHECKS)
    assert out["relations"] == {}


def test_reclamation_one_surviving_path_is_fail():
    """하나라도 막히지 않았으면 회수가 끝나지 않은 것이다."""
    assert _revoke({"credential_blocked": F})["access_verdict"] == F


def test_reclamation_collector_failure_is_unknown():
    out = _revoke({"endpoint_blocked": ConnectionError("node unreachable")})
    assert out["access_verdict"] == U
    assert out["domains"]["endpoint_blocked"]["evidence"]["collector_error"] == "ConnectionError"


def test_reclamation_collects_every_check():
    seen = []
    evaluator.evaluate_reclamation(_collector(_all(evaluator.RECLAMATION_CHECKS), seen=seen), target=TARGET)
    assert tuple(c for c, _ in seen) == evaluator.RECLAMATION_CHECKS


# 보호 판정

def _bystander(name):
    return {"username": name, "expected": {"home_suffix": "/u1", "uid": None}}


def test_protection_passes_when_every_bystander_keeps_access():
    collect = _collector(_all(evaluator.CREATION_CHECKS))
    out = evaluator.evaluate_protection(collect, bystanders=[_bystander("b1"), _bystander("b2")])
    assert out["protection_verdict"] == P
    assert set(out["bystanders"]) == {"b1", "b2"}


def test_protection_fails_when_one_bystander_lost_access():
    def collect(check, target):
        if target["username"] == "b2" and check == "storage_rw":
            return F, {}
        return _collector(_all(evaluator.CREATION_CHECKS))(check, target)
    out = evaluator.evaluate_protection(collect, bystanders=[_bystander("b1"), _bystander("b2")])
    assert out["protection_verdict"] == F
    assert out["bystanders"]["b1"]["access_verdict"] == P
    assert out["bystanders"]["b2"]["access_verdict"] == F


def test_protection_without_bystanders_is_none():
    out = evaluator.evaluate_protection(_collector({}), bystanders=[])
    assert out == {"protection_verdict": None, "bystanders": {}}
