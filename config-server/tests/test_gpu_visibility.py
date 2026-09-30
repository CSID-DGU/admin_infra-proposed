import pytest

import main
import utils


@pytest.mark.parametrize("db_name, driver_name", [
    ("RTX 3090", "NVIDIA GeForce RTX 3090"),
    ("RTX A5000", "NVIDIA RTX A5000"),
    ("RTX 6000 Ada", "NVIDIA RTX 6000 Ada Generation"),
    ("H200 NVL", "NVIDIA H200 NVL"),
    ("RTX 2080 Ti", "NVIDIA GeForce RTX 2080 Ti"),
])
def test_db_and_driver_names_of_same_gpu_match(db_name, driver_name):
    assert utils.normalize_gpu_model(db_name) == utils.normalize_gpu_model(driver_name)


@pytest.mark.parametrize("a, b", [
    ("RTX 3090", "NVIDIA GeForce RTX 3090 Ti"),
    ("RTX A6000", "NVIDIA RTX 6000 Ada Generation"),
    ("RTX A5000", "NVIDIA RTX A6000"),
])
def test_different_gpus_do_not_match(a, b):
    assert utils.normalize_gpu_model(a) != utils.normalize_gpu_model(b)


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


def _series(gpu, uuid, model):
    return {"metric": {"Hostname": "farm10", "gpu": str(gpu), "UUID": uuid, "modelName": model}, "value": [0, "0"]}


def test_list_node_gpus_orders_by_index_and_dedupes(monkeypatch):
    seen = {}

    def fake_get(url, params, timeout):
        seen["query"] = params["query"]
        return _Resp({"data": {"result": [
            _series(1, "GPU-b", "NVIDIA GeForce RTX 3090"),
            _series(0, "GPU-a", "NVIDIA RTX A5000"),
            _series(0, "GPU-a", "NVIDIA RTX A5000"),
        ]}})
    monkeypatch.setattr("requests.get", fake_get)

    assert utils.list_node_gpus("farm10", "http://prom", 3) == [
        ("GPU-a", "NVIDIA RTX A5000"), ("GPU-b", "NVIDIA GeForce RTX 3090")]
    assert 'Hostname="farm10"' in seen["query"]


def test_list_node_gpus_raises_when_prometheus_fails(monkeypatch):
    monkeypatch.setattr("requests.get", lambda url, params, timeout: _Resp({}, status=503))
    with pytest.raises(RuntimeError):
        utils.list_node_gpus("farm10", "http://prom", 3)


def test_mixed_node_exposes_every_gpu_of_requested_model_only(monkeypatch):
    from lifecycle_steps.provision import _resolve_visible_gpus
    monkeypatch.setattr(main, "list_node_gpus", lambda node, url, t: [
        ("GPU-0", "NVIDIA RTX A5000"), ("GPU-1", "NVIDIA GeForce RTX 3090"),
        ("GPU-2", "NVIDIA RTX A5000"), ("GPU-3", "NVIDIA GeForce RTX 3090")])
    with main.app.app_context():
        assert _resolve_visible_gpus("farm10", 2, ["RTX A5000"]) == "GPU-0,GPU-2"
        assert _resolve_visible_gpus("farm10", 2, ["RTX 3090"]) == "GPU-1,GPU-3"
