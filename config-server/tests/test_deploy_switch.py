"""장애 주입 스위치(FAULT_INJECTION)가 실험 스택 셋에서만 켜지고 운영에서는 꺼져 있는지 텍스트로 확인한다.

게이트 환경에는 helm 이 없다고 보고, 차트와 설치 스크립트를 문자열로 읽는다.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "config-server" / "Chart"
STACK_UP = ROOT / "ops" / "proposed-stack" / "stack-up.sh"


def test_helpers_sets_fault_injection_only_under_the_switch():
    tpl = (CHART / "templates" / "_helpers.tpl").read_text()
    blocks = re.findall(r"\{\{- if \.Values\.faultInjection \}\}(.*?)\{\{- end \}\}", tpl, re.S)
    assert len(blocks) == 1 and "FAULT_INJECTION" in blocks[0]
    assert tpl.count("FAULT_INJECTION") == 1


def test_chart_default_is_off():
    assert re.search(r"^faultInjection: false$", (CHART / "values.yaml").read_text(), re.M)


def test_stack_up_turns_it_on_only_for_experiment_stacks():
    lines = STACK_UP.read_text().splitlines()
    on = [l for l in lines if re.search(r"FAULT_INJECTION=true|faultInjection=true", l)]
    assert on, "stack-up.sh 에서 실험 스택용 켜기 줄을 찾지 못함"
    for l in on:
        arm = re.match(r'\s*case "\$STACK" in ([\w|]+)\)', l)
        assert arm, f"켜기 줄이 STACK 조건 밖에 있음: {l}"
        assert set(arm.group(1).split("|")) <= {"noprobe", "full", "baseline"}


def test_no_other_values_file_turns_it_on():
    for p in ROOT.rglob("*.y*ml"):
        if ".git" in p.parts:
            continue
        assert "faultInjection: true" not in p.read_text(errors="ignore"), p
