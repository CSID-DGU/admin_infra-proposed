"""시나리오 명세(harness/scenarios/<scenario_id>.yaml)를 읽고 검사하는 유일한 자리.

근거는 docs/domains/scenario-alignment.md 의 "시나리오 명세" 다. 명세는 실행에 필요한 부분과
Metrics Analyzer 만 읽는 기대값(analysis)으로 나뉜다. 측정자가 답을 알고 재면 판정이 기대에
끌려가므로, 실행 경로는 run_view() 만 쓰고 analysis_view() 는 분석기만 부른다. 이 규칙은
test_scenario_spec.py 가 ast 로 지킨다.

빠진 필드와 모르는 필드는 예외다. 기본값으로 채우지 않는다. 채우면 저작자가 적지 않은 기대가
명세에 적힌 것처럼 보인다.
"""
import copy
import hashlib
from pathlib import Path

import yaml

GROUPS = frozenset("NPABCDEFGH")
OPERATION_TYPES = frozenset({"provisioning", "reclamation"})
PAIR_ROLES = frozenset({"create", "revoke"})
METHODS = frozenset({"baseline", "noprobe", "full"})
KINDS = frozenset({"none", "code_hook", "external_mutation"})
ACTIONS = frozenset({"none", "response_loss", "sigkill_before_journal", "fail_persistent",
                     "endpoint_block", "ad_block"})
BOUNDARIES = frozenset(f"X{i}" for i in range(12))
# 주입 성립 확인 방법. 뜻은 fault_injector 에 있다.
VERIFIES = frozenset({"none", "fired_after_success", "fired_before_success", "policy_present"})

_TOP = {"scenario_id", "aliases", "group", "operation", "applies_to", "injection", "analysis"}
_OPERATION = {"type", "pair_role"}
_INJECTION = {"kind", "boundary", "step", "action", "occurrence", "verify"}
_ANALYSIS = {"recoverable", "expected_ground_truth", "forbidden_behavior", "metrics"}


class SpecError(ValueError):
    pass


def _keys(where, obj, expected):
    if not isinstance(obj, dict):
        raise SpecError(f"{where} 는 매핑이어야 한다: {obj!r}")
    missing, unknown = expected - obj.keys(), obj.keys() - expected
    if missing or unknown:
        raise SpecError(f"{where}: 빠진 필드 {sorted(missing)}, 모르는 필드 {sorted(unknown)}")


def _one_of(where, value, allowed):
    if value not in allowed:
        raise SpecError(f"{where} 는 {sorted(allowed)} 중 하나여야 한다: {value!r}")


def _str_list(where, value):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SpecError(f"{where} 는 문자열 목록이어야 한다: {value!r}")


def _validate(raw):
    _keys("명세", raw, _TOP)
    sid = raw["scenario_id"]
    if not isinstance(sid, str) or not sid:
        raise SpecError(f"scenario_id 는 빈 문자열이 아니어야 한다: {sid!r}")
    _one_of("group", raw["group"], GROUPS)
    if sid[0] != raw["group"]:
        raise SpecError(f"{sid}: group {raw['group']!r} 가 ID 의 첫 글자와 다르다")
    _str_list("aliases", raw["aliases"])

    _keys("operation", raw["operation"], _OPERATION)
    _one_of("operation.type", raw["operation"]["type"], OPERATION_TYPES)
    _one_of("operation.pair_role", raw["operation"]["pair_role"], PAIR_ROLES)

    _str_list("applies_to", raw["applies_to"])
    if not raw["applies_to"] or not set(raw["applies_to"]) <= METHODS:
        raise SpecError(f"applies_to 는 {sorted(METHODS)} 의 비지 않은 부분집합이어야 한다")

    inj = raw["injection"]
    _keys("injection", inj, _INJECTION)
    _one_of("injection.kind", inj["kind"], KINDS)
    _one_of("injection.action", inj["action"], ACTIONS)
    if not isinstance(inj["occurrence"], int) or isinstance(inj["occurrence"], bool):
        raise SpecError(f"injection.occurrence 는 정수여야 한다: {inj['occurrence']!r}")
    _one_of("injection.verify", inj["verify"], VERIFIES)
    if inj["step"] is not None and not isinstance(inj["step"], str):
        raise SpecError(f"injection.step 은 문자열이나 null 이어야 한다: {inj['step']!r}")
    if inj["kind"] == "none":
        if (inj["boundary"], inj["step"], inj["action"]) != (None, None, "none"):
            raise SpecError(f"{sid}: kind none 이면 boundary·step 은 null, action 은 none 이어야 한다")
    else:
        _one_of("injection.boundary", inj["boundary"], BOUNDARIES)
        if inj["action"] == "none" or inj["occurrence"] < 1:
            raise SpecError(f"{sid}: 주입이 있으면 action 이 none 이 아니고 occurrence 가 1 이상이어야 한다")

    ana = raw["analysis"]
    _keys("analysis", ana, _ANALYSIS)
    if not isinstance(ana["recoverable"], bool):
        raise SpecError(f"analysis.recoverable 은 참거짓이어야 한다: {ana['recoverable']!r}")
    if not isinstance(ana["expected_ground_truth"], dict) or not ana["expected_ground_truth"]:
        raise SpecError("analysis.expected_ground_truth 는 비지 않은 매핑이어야 한다")
    _str_list("analysis.forbidden_behavior", ana["forbidden_behavior"])
    _str_list("analysis.metrics", ana["metrics"])


class Spec:
    def __init__(self, raw, spec_hash):
        self._raw = raw
        self.scenario_id = raw["scenario_id"]
        self.spec_hash = spec_hash

    def run_view(self):
        """실행 경로(하네스, 평가자, 주입기)가 쓰는 사본. analysis 가 없다."""
        view = copy.deepcopy(self._raw)
        del view["analysis"]
        return view

    def analysis_view(self):
        """Metrics Analyzer 만 부른다."""
        return {"scenario_id": self.scenario_id, "aliases": list(self._raw["aliases"]),
                "spec_hash": self.spec_hash, "analysis": copy.deepcopy(self._raw["analysis"])}


def _read(path):
    data = path.read_bytes()
    raw = yaml.safe_load(data)
    try:
        _validate(raw)
    except SpecError as e:
        raise SpecError(f"{path}: {e}") from None
    return Spec(raw, hashlib.sha256(data).hexdigest()[:16])


def _check_name(spec, path):
    if spec.scenario_id != path.stem:
        raise SpecError(f"{path}: scenario_id {spec.scenario_id!r} 가 파일 이름과 다르다")


def load(path):
    path = Path(path)
    spec = _read(path)
    _check_name(spec, path)
    return spec


def load_all(directory):
    """디렉터리의 명세를 {scenario_id: Spec} 으로 읽는다. 같은 ID 가 두 파일에 있으면 예외다."""
    specs = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        spec = _read(path)
        if spec.scenario_id in specs:
            raise SpecError(f"같은 scenario_id {spec.scenario_id!r} 가 두 파일에 있다: {path}")
        _check_name(spec, path)
        specs[spec.scenario_id] = spec
    return specs
