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


def test_every_slack_webhook_key_in_prod_config_is_sunk_for_experiment_stacks():
    """막을 채널을 손으로 적은 목록이 아니라 운영 설정의 키에서 뽑는지, 주소가 아니라 이름만 뽑는지 확인한다."""
    import subprocess

    text = STACK_UP.read_text()
    function = re.search(r"^slack_webhook_keys\(\) \{.*?^\}$", text, re.S | re.M).group(0)
    config = (
        "app:\n  servers:\n    FARM:\n      request-channel: farm-request\n"
        "slack-webhook-url:\n  # 주석\n  error-log: https://hooks.example/a\n  farm-admin: https://hooks.example/b\n"
        "  farm-request: https://hooks.example/c\n\nslack:\n  bot-token: x\n")
    out = subprocess.run(["bash", "-c", function + "\nslack_webhook_keys"], input=config, capture_output=True,
                         text=True, check=True).stdout
    assert out.split() == ["error-log", "farm-admin", "farm-request"]
    assert "hooks.example" not in out and "bot-token" not in out
    # 뽑은 키는 전부 닿지 않는 주소로 덮이고, operation만 덮어쓰기를 끈다
    assert 'for key in $WEBHOOK_KEYS; do SINKS=' in text
    assert re.search(r'\[ "\$STACK" = "operation" \] && SLACK_OVERRIDE=""', text)


def test_only_operation_stack_writes_issuance_sheet():
    # 문서 ID·키는 운영 설정에서 모든 스택이 물려받는다. 켜는 값은 operation에만 넣고, 나머지는 꺼 둔다.
    text = STACK_UP.read_text()
    assert """ISSUANCE_SHEET_OVERRIDE=',"issuance-sheet":{"enabled":false}'""" in text
    assert re.search(
        r'''\[ "\$STACK" = "operation" \] && ISSUANCE_SHEET_OVERRIDE=',"issuance-sheet":\{"enabled":true\}'$''',
        text, re.M)
    assert '$SLACK_OVERRIDE$ISSUANCE_SHEET_OVERRIDE$SLACK_MEMBERSHIP_OVERRIDE,"prometheus"' in text


def test_only_operation_stack_checks_slack_membership():
    # admin_be 기본값이 꺼짐이라 켜는 쪽만 둔다. "slack" 묶음(SLACK_OVERRIDE)과 같은 키를 두 번 쓰지 않는다.
    text = STACK_UP.read_text()
    assert re.search(r"^SLACK_MEMBERSHIP_OVERRIDE=''$", text, re.M)
    assert re.search(
        r'''^\[ "\$STACK" = "operation" \] && SLACK_MEMBERSHIP_OVERRIDE=',"slack\.membership-check\.enabled":true'$''',
        text, re.M)
    assert len(re.findall(r"SLACK_MEMBERSHIP_OVERRIDE=", text)) == 2
