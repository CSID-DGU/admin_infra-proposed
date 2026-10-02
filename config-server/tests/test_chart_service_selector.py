"""Service가 API Pod만 고르는지 텍스트로 확인한다(게이트 환경에는 helm이 없다).

크론잡 Pod는 API Pod와 app 라벨이 같다. Service가 app만 보면 크론잡이 도는 동안 요청 일부가 포트를 열지 않은
크론잡 Pod로 가서 Connection refused가 난다(2026-10-02 noprobe에서 재시작 등록이 502로 실패).
"""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "config-server" / "Chart" / "templates"
ONLY_API = "component: api"


def _text(name):
    return (TEMPLATES / name).read_text()


def test_service_selects_a_label_only_the_api_pod_has():
    selector = re.search(r"selector:\n((?:    .+\n)+)", _text("service.yaml")).group(1)
    assert ONLY_API in selector
    assert ONLY_API in _text("deployment.yaml")


def test_cronjob_and_controller_pods_do_not_carry_that_label():
    for name in ("cronjob-gpu-check.yaml", "cronjob-krb5-reconcile.yaml", "controller.yaml"):
        assert ONLY_API not in _text(name), name
