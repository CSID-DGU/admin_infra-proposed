"""admin_infra_server 의 server-state CLI 를 부르는 유일한 자리.

인프라를 테스트하고 접근하는 일은 admin_infra_server 의 스크립트로 한다고 팀이 합의했다.
하네스 어디에서도 SSH 나 kubectl 을 직접 부르지 않고 전부 이 파일을 거친다 — 합의를
문서가 아니라 코드로 강제하기 위해서다. 새 경로가 필요하면 여기에 함수를 더한다.

독립 평가자가 이 경로를 쓰는 것은 우연이 아니라 요구사항이다. 대상 시스템은 kubectl exec
로 자기 자신을 확인하고 이쪽은 Ansible SSH 로 확인하므로, 두 판단이 같은 고장에 함께
빠지지 않는다 (논문 5.1절의 독립 평가 요구).

환경변수 ADMIN_INFRA_SERVER 에 admin_infra_server 체크아웃 경로를 준다.
"""
import json
import os
import subprocess
from pathlib import Path

ENV_VAR = "ADMIN_INFRA_SERVER"
DEFAULT_TIMEOUT_SEC = 600


class SystemCallFailed(Exception):
    """server-state 를 부르지 못했다. 감사 결과가 실패인 것과는 다르다."""


def cli_path():
    root = os.environ.get(ENV_VAR)
    if not root:
        raise SystemCallFailed(
            f"{ENV_VAR} 가 설정되지 않았다. admin_infra_server 체크아웃 경로를 지정해야 한다.")
    path = Path(root) / "server-state" / "bin" / "server-state"
    if not path.is_file():
        raise SystemCallFailed(f"server-state 를 찾지 못했다: {path}")
    return path


def run(subcommand, *args, show_command=False, timeout=DEFAULT_TIMEOUT_SEC):
    """server-state 를 JSON 출력으로 부르고 (행 목록, 종료코드) 를 돌려준다.

    종료코드가 0이 아니어도 예외를 던지지 않는다. 감사 실패는 판정에 쓰는 자료이지
    호출 실패가 아니기 때문이다. 무엇을 실패로 볼지는 부르는 쪽이 정한다.
    CLI 자체를 부르지 못한 경우에만 SystemCallFailed 가 난다.
    """
    cmd = [str(cli_path()), "--format", "json", subcommand, *args]
    if show_command:
        cmd.append("--show-command")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SystemCallFailed(f"server-state {subcommand} 실행 실패: {e}") from e
    try:
        rows = json.loads(p.stdout or "[]")
    except json.JSONDecodeError as e:
        raise SystemCallFailed(
            f"server-state {subcommand} 출력이 JSON 이 아니다: {p.stdout[:400]!r} / {p.stderr[:200]!r}") from e
    return rows, p.returncode


def audit(hosts, component, show_command=False):
    """읽기 전용 감사. 자원 상태를 바꾸지 않는다."""
    return run("audit", "--hosts", hosts, "--component", component, show_command=show_command)


def list_hosts(hosts="all"):
    """대상 호스트와 주소와 외부 health 포트. 실험 환경 기록(논문 Table 3)에 쓴다."""
    return run("list-hosts", "--hosts", hosts)


def describe(component=None):
    """정책에 선언된 컴포넌트와 안전 등급. 실행한 정책 버전을 기록할 때 쓴다."""
    args = ("--component", component) if component else ()
    return run("describe", *args)


# apply(수렴)는 아직 열지 않는다. 자원을 바꾸는 경로라 안전 승인 인자를 함께 설계해야 하고,
# 지금 하네스에서 그것을 부르는 곳이 없다. Environment Resetter 를 만들 때 여기에 더한다.
