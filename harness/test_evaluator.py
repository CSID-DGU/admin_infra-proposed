"""evaluator.py 계약 검증. 수집기를 가짜로 세워서 판정 규칙만 시험한다."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluator  # noqa: E402


def _collector(results, seen=None):
    """검사 이름별 결과를 돌려주는 가짜 수집기. 값이 예외면 그것을 던진다."""
    def collect(check, username):
        if seen is not None:
            seen.append(check)
        value = results[check]
        if isinstance(value, Exception):
            raise value
        return value, {"scope": check, "user": username}
    return collect


def test_creation_all_pass_is_pass():
    collect = _collector({name: evaluator.PASS for name in evaluator.CREATION_CHECKS})
    assert evaluator.evaluate_creation(collect, username="u1")["verdict"] == evaluator.PASS


def test_creation_one_fail_outweighs_the_rest():
    results = {name: evaluator.PASS for name in evaluator.CREATION_CHECKS}
    results["endpoint"] = evaluator.FAIL
    out = evaluator.evaluate_creation(_collector(results), username="u1")
    assert out["verdict"] == evaluator.FAIL
    assert out["checks"]["endpoint"] == evaluator.FAIL


def test_creation_one_unknown_makes_the_whole_unknown():
    results = {name: evaluator.PASS for name in evaluator.CREATION_CHECKS}
    results["storage_access"] = evaluator.UNKNOWN
    out = evaluator.evaluate_creation(_collector(results), username="u1")
    assert out["verdict"] == evaluator.UNKNOWN


def test_creation_fail_beats_unknown():
    """위반을 이미 확인했으므로 UNKNOWN 이 섞여도 FAIL 이다."""
    results = {name: evaluator.PASS for name in evaluator.CREATION_CHECKS}
    results["login"] = evaluator.FAIL
    results["endpoint"] = evaluator.UNKNOWN
    assert evaluator.evaluate_creation(_collector(results), username="u1")["verdict"] == evaluator.FAIL


def test_collector_failure_is_unknown_not_fail():
    """평가자가 못 물어본 것은 위반이 아니다."""
    results = {name: evaluator.PASS for name in evaluator.CREATION_CHECKS}
    results["container_identity"] = TimeoutError("test client unreachable")
    out = evaluator.evaluate_creation(_collector(results), username="u1")
    assert out["verdict"] == evaluator.UNKNOWN
    assert out["checks"]["container_identity"] == evaluator.UNKNOWN
    assert "test client unreachable" in out["evidence"]["container_identity"]["message"]


def test_unexpected_result_is_unknown_and_kept_as_evidence():
    """알 수 없는 값을 성공으로 접으면 안 된다."""
    results = {name: evaluator.PASS for name in evaluator.CREATION_CHECKS}
    results["login"] = True
    out = evaluator.evaluate_creation(_collector(results), username="u1")
    assert out["checks"]["login"] == evaluator.UNKNOWN
    assert "True" in out["evidence"]["login"]["unexpected_result"]


def test_every_check_is_collected_even_after_a_fail():
    """앞선 검사가 FAIL 이어도 증거를 전부 모은다. 빠지면 원인을 가릴 수 없다."""
    seen = []
    results = {name: evaluator.FAIL for name in evaluator.CREATION_CHECKS}
    evaluator.evaluate_creation(_collector(results, seen), username="u1")
    assert tuple(seen) == evaluator.CREATION_CHECKS


def test_result_is_json_serializable():
    collect = _collector({name: evaluator.PASS for name in evaluator.CREATION_CHECKS})
    out = evaluator.evaluate_creation(collect, username="u1")
    assert json.loads(json.dumps(out))["verdict"] == evaluator.PASS


def test_reclamation_passes_only_when_all_four_paths_are_blocked():
    collect = _collector({name: evaluator.PASS for name in evaluator.RECLAMATION_CHECKS})
    out = evaluator.evaluate_reclamation(collect, username="u1")
    assert out["verdict"] == evaluator.PASS
    assert set(out["checks"]) == set(evaluator.RECLAMATION_CHECKS)


def test_reclamation_one_surviving_path_is_fail():
    """하나라도 막히지 않았으면 회수가 끝나지 않은 것이다."""
    results = {name: evaluator.PASS for name in evaluator.RECLAMATION_CHECKS}
    results["credential_blocked"] = evaluator.FAIL
    assert evaluator.evaluate_reclamation(_collector(results), username="u1")["verdict"] == evaluator.FAIL


def test_reclamation_collector_failure_is_unknown():
    results = {name: evaluator.PASS for name in evaluator.RECLAMATION_CHECKS}
    results["endpoint_blocked"] = ConnectionError("node unreachable")
    out = evaluator.evaluate_reclamation(_collector(results), username="u1")
    assert out["verdict"] == evaluator.UNKNOWN
    assert out["evidence"]["endpoint_blocked"]["collector_error"] == "ConnectionError"


def test_reclamation_collects_every_check():
    seen = []
    results = {name: evaluator.PASS for name in evaluator.RECLAMATION_CHECKS}
    evaluator.evaluate_reclamation(_collector(results, seen), username="u1")
    assert tuple(seen) == evaluator.RECLAMATION_CHECKS
