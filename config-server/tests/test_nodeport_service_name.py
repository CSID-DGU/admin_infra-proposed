"""포트 용도는 신청자가 자유롭게 적는 글이다. 서비스 이름·라벨에 그대로 넣으면 한글·공백·괄호가 들어간
신청에서 쿠버네티스가 서비스를 거절해 생성 작업이 맨 끝에서 실패한다(2026-10-06 operation 신청 66)."""
import re

import pytest

import utils

DNS_1035 = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
LABEL_VALUE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")


def _body(purpose, username="yongbin53", external_port=30012):
    port = {"internal_port": 5173, "external_port": external_port}
    if purpose is not None:
        port["usage_purpose"] = purpose
    return utils.nodeport_service_body(username, "ns", f"ailab-{username}-abcd1234", port)


@pytest.mark.parametrize("purpose", ["프론트엔드 (React, Vite)", "백엔드 포트", "a" * 200, "UPPER case", "", None, "ssh"])
def test_name_and_labels_are_valid_for_any_purpose(purpose):
    body = _body(purpose)
    assert DNS_1035.match(body.metadata.name) and len(body.metadata.name) <= 63
    assert all(LABEL_VALUE.match(v) for v in body.metadata.labels.values())
    assert body.spec.ports[0].name is None


def test_name_carries_neither_purpose_nor_port():
    body = _body("jupyter", external_port=30012)
    assert re.fullmatch(r"ailab-yongbin53-[0-9a-f]{8}", body.metadata.name)
    assert _body("jupyter").metadata.name != body.metadata.name


def test_longest_username_fits():
    assert len(_body("ssh", username="a" * 32).metadata.name) <= 63


def test_purpose_text_is_kept_as_annotation():
    assert _body("프론트엔드 (React, Vite)").metadata.annotations[utils.PURPOSE_ANNOTATION] == "프론트엔드 (React, Vite)"
    assert _body(None).metadata.annotations[utils.PURPOSE_ANNOTATION] == "custom"


def test_purpose_label_only_when_label_safe():
    # ssh 라벨은 수집 도구가 SSH 서비스를 찾는 데 쓴다(harness/real_collector.py).
    assert _body("ssh").metadata.labels["purpose"] == "ssh"
    assert "purpose" not in _body("프론트엔드 (React, Vite)").metadata.labels


def test_selector_and_ports_unchanged():
    body = _body("ssh")
    assert body.spec.type == "NodePort" and body.spec.selector == {"pod_name": "ailab-yongbin53-abcd1234"}
    port = body.spec.ports[0]
    assert (port.port, port.target_port, port.node_port, port.protocol) == (5173, 5173, 30012, "TCP")
    assert body.metadata.labels["pod_name"] == "ailab-yongbin53-abcd1234"


def test_create_makes_one_service_per_port(monkeypatch):
    created = []

    class Api:
        def create_namespaced_service(self, namespace, body):
            created.append((namespace, body))

    monkeypatch.setattr(utils, "load_k8s", lambda: None)
    monkeypatch.setattr(utils.client, "CoreV1Api", Api)
    import main
    with main.app.app_context():
        utils.create_nodeport_services("eunice", "ns", "ailab-eunice-abcd1234", [
            {"internal_port": 5000, "external_port": 30020, "usage_purpose": "백엔드 포트"},
            {"internal_port": 5173, "external_port": 30021, "usage_purpose": "프론트엔드 포트"},
        ])
    assert [b.spec.ports[0].node_port for _, b in created] == [30020, 30021]
    assert len({b.metadata.name for _, b in created}) == 2
