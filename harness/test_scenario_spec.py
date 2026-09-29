"""시나리오 명세 로더와, 실행 경로가 기대값(analysis)을 읽지 못한다는 규칙의 회귀 시험.

실행 경로 검사는 문자열 검색이 아니라 ast 로 한다. 주석과 문서 문자열에 analysis 라는 낱말이
나오는 것은 설명이지 위반이 아니다.
"""
import ast
import shutil
from pathlib import Path

import pytest
import yaml

import scenario_spec

HERE = Path(__file__).resolve().parent
SCENARIOS = HERE / "scenarios"
WAVE1 = {"N1", "N2", "A3-KRB5", "B4-KRB5", "C3-KRB5", "F1-ADBLOCK", "D6-ENDPOINT", "A1-POD"}
RUN_MODULES = ("trial_runner", "evaluator", "measure", "measure_ports", "fault_injector",
               "resetter", "inventory")


def _write(tmp_path, name, raw):
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def _a3():
    return yaml.safe_load((SCENARIOS / "A3-KRB5.yaml").read_text(encoding="utf-8"))


def test_wave1_specs_load_and_ids_match_file_names():
    specs = scenario_spec.load_all(SCENARIOS)
    assert set(specs) == WAVE1
    for sid, spec in specs.items():
        assert (SCENARIOS / f"{sid}.yaml").is_file()
        assert len(spec.spec_hash) == 16


def test_f1_is_not_recoverable():
    spec = scenario_spec.load(SCENARIOS / "F1-ADBLOCK.yaml")
    assert spec.analysis_view()["analysis"]["recoverable"] is False
    assert "premature_revoked" in spec.analysis_view()["analysis"]["forbidden_behavior"]


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("group"),
    lambda r: r["injection"].pop("verify"),
    lambda r: r["analysis"].pop("recoverable"),
    lambda r: r.update(extra=1),
    lambda r: r["injection"].update(extra=1),
    lambda r: r["analysis"].update(extra=1),
    lambda r: r["injection"].update(action="explode"),
    lambda r: r["injection"].update(boundary="X12"),
    lambda r: r.update(applies_to=["manual"]),
    lambda r: r["operation"].update(pair_role="migrate"),
], ids=["no-group", "no-verify", "no-recoverable", "unknown-top", "unknown-injection",
        "unknown-analysis", "unknown-action", "bad-boundary", "bad-method", "bad-role"])
def test_invalid_spec_raises(tmp_path, mutate):
    raw = _a3()
    mutate(raw)
    with pytest.raises(scenario_spec.SpecError):
        scenario_spec.load(_write(tmp_path, "A3-KRB5", raw))


def test_id_must_match_file_name(tmp_path):
    with pytest.raises(scenario_spec.SpecError):
        scenario_spec.load(_write(tmp_path, "A3-OTHER", _a3()))


def test_run_view_has_no_analysis():
    for spec in scenario_spec.load_all(SCENARIOS).values():
        view = spec.run_view()
        assert "analysis" not in view
        assert view["injection"] == spec.run_view()["injection"]
    # run_view 는 사본이다. 고쳐도 명세가 바뀌지 않는다.
    spec = scenario_spec.load(SCENARIOS / "A3-KRB5.yaml")
    spec.run_view()["injection"]["action"] = "none"
    assert spec.run_view()["injection"]["action"] == "response_loss"


def _analysis_reads(path):
    """analysis_view 를 가리키는 속성 접근과 "analysis" 상수 첨자를 찾는다."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "analysis_view":
            hits.append(f"{path.name}:{node.lineno} .analysis_view")
        elif (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
              and node.slice.value == "analysis"):
            hits.append(f"{path.name}:{node.lineno} [\"analysis\"]")
    return hits


def test_run_modules_do_not_read_analysis():
    hits = [h for m in RUN_MODULES for h in _analysis_reads(HERE / f"{m}.py")]
    assert not hits, (
        f"실행 경로가 명세의 기대값을 읽었다: {hits}. 측정자가 답을 알고 재면 판정이 기대에 "
        "끌려간다. 근거는 docs/domains/scenario-alignment.md 의 계획 원칙 2다.")


def test_analysis_reads_detector_catches_both_forms(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text('x = spec["analysis"]\ny = s.analysis_view()\n', encoding="utf-8")
    assert len(_analysis_reads(probe)) == 2


def test_duplicate_id_in_two_files_raises(tmp_path):
    shutil.copy(SCENARIOS / "A3-KRB5.yaml", tmp_path / "A3-KRB5.yaml")
    shutil.copy(SCENARIOS / "A3-KRB5.yaml", tmp_path / "A3-KRB6.yaml")
    with pytest.raises(scenario_spec.SpecError, match="두 파일"):
        scenario_spec.load_all(tmp_path)
