import pytest

import main
import utils


def test_kubernetes_client_mode_is_required_and_kubeconfig_needs_an_explicit_path():
    with pytest.raises(SystemExit, match="KUBERNETES_CLIENT_MODE"):
        main._resolve_kubernetes_client({})
    with pytest.raises(SystemExit, match="KUBECONFIG_PATH"):
        main._resolve_kubernetes_client({"KUBERNETES_CLIENT_MODE": "kubeconfig"})


def test_in_cluster_failure_never_falls_back_to_kubeconfig(monkeypatch):
    calls = []

    def fail_in_cluster():
        calls.append("in-cluster")
        raise RuntimeError("service account is unavailable")

    monkeypatch.setenv("KUBERNETES_CLIENT_MODE", "in-cluster")
    monkeypatch.delenv("KUBECONFIG_PATH", raising=False)
    monkeypatch.setattr(utils.k8s_config, "load_incluster_config", fail_in_cluster)
    monkeypatch.setattr(utils.k8s_config, "load_kube_config", lambda **_: calls.append("kubeconfig"))

    with pytest.raises(RuntimeError, match="service account"):
        main.load_k8s()
    assert calls == ["in-cluster"]


def test_kubeconfig_mode_uses_only_the_explicit_file(monkeypatch):
    calls = []
    monkeypatch.setenv("KUBERNETES_CLIENT_MODE", "kubeconfig")
    monkeypatch.setenv("KUBECONFIG_PATH", "/work/config")
    monkeypatch.setattr(utils.k8s_config, "load_incluster_config", lambda: calls.append("in-cluster"))
    monkeypatch.setattr(
        utils.k8s_config,
        "load_kube_config",
        lambda **kwargs: calls.append(("kubeconfig", kwargs)),
    )

    main.load_k8s()

    assert calls == [("kubeconfig", {"config_file": "/work/config"})]
