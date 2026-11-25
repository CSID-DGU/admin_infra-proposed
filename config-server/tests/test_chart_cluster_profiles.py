import pathlib
import re
import shutil
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHART = ROOT / "Chart"


def _render(release, values, *extra):
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm is not installed")
    version = subprocess.run([helm, "version", "--short"], capture_output=True, text=True)
    if version.returncode:
        pytest.skip(f"helm cannot run in this environment: {version.stderr.strip()}")
    rendered = subprocess.run(
        [helm, "template", release, str(CHART), "-f", str(CHART / values), *extra],
        capture_output=True, text=True, timeout=30,
    )
    assert rendered.returncode == 0, rendered.stderr
    return rendered.stdout


def test_chart_source_has_no_implicit_management_desktop_or_farm_env_output():
    values = (CHART / "values.yaml").read_text()
    helpers = (CHART / "templates" / "_helpers.tpl").read_text()
    assert "csid-dgu-desktop" not in values
    assert "nodeSelector: {}" in values
    assert "cluster.id를 지정해야" in helpers
    assert "- name: INFRA_NODES_JSON" in helpers
    assert "- name: FARM_NODES_JSON" not in helpers


@pytest.mark.parametrize(
    ("release", "values", "cluster", "role"),
    [
        ("farm-config", "values-farm.example.yaml", "farm", "farm-management"),
        ("lab-config", "values-lab.example.yaml", "lab", "lab-management"),
    ],
)
def test_farm_and_lab_profiles_render_as_independent_releases(release, values, cluster, role):
    rendered = _render(release, values)
    assert f'name: {release}\n' in rendered
    assert f'app.kubernetes.io/instance: "{release}"' in rendered
    assert f'provi.dgu.ac.kr/cluster-id: "{cluster}"' in rendered
    assert f'value: "{cluster}"' in rendered
    assert 'name: KUBERNETES_CLIENT_MODE\n              value: "in-cluster"' in rendered
    assert 'name: KUBERNETES_CLIENT_VALIDATE_AT_STARTUP\n              value: "true"' in rendered
    assert f'provi.dgu.ac.kr/role: {role}' in rendered
    assert "app: containerssh-config-server" in rendered
    if cluster == "farm":
        # 기존 FARM 기본 동작은 두 주기 작업을 유지한다.
        assert f"name: {release}-gpu-check" in rendered
        assert f"name: {release}-krb5-reconcile" in rendered
        assert rendered.count(f'provi.dgu.ac.kr/role: {role}') >= 3
    else:
        # 신규 LAB은 API health/read-only 검증 전까지 controller와 주기 작업을 만들지 않는다.
        assert f"name: {release}-gpu-check" not in rendered
        assert f"name: {release}-krb5-reconcile" not in rendered
        assert f"name: {release}-controller" not in rendered
    for suffix in ("-db", "-log-db", "-api-token", "-infra-ssh", "-infra-ad-ssh", "-krb5"):
        assert f'{release}{suffix}' in rendered
    assert "FARM_NODES_JSON" not in rendered


@pytest.mark.parametrize(
    ("value", "resource_name", "other_resource_name"),
    [
        (
            "cronjobs.gpuCheck.enabled=true",
            "lab-config-gpu-check",
            "lab-config-krb5-reconcile",
        ),
        (
            "cronjobs.krb5Reconcile.enabled=true",
            "lab-config-krb5-reconcile",
            "lab-config-gpu-check",
        ),
    ],
)
def test_lab_cronjobs_can_be_enabled_independently_after_initial_validation(
        value, resource_name, other_resource_name):
    rendered = _render("lab-config", "values-lab.example.yaml", "--set", value)
    assert f"name: {resource_name}" in rendered
    assert f"name: {other_resource_name}" not in rendered


def test_legacy_values_render_with_the_exact_immutable_selectors_and_compatible_paths():
    rendered = _render(
        "containerssh-config-server",
        "fixtures/values-legacy-upgrade.yaml",
        "--set", "cluster.id=farm",
        "--set", "image.tag=test",
    )

    api_selector = re.search(
        r"kind: Deployment\nmetadata:\n  name: containerssh-config-server\n.*?selector:\n    matchLabels:\n(.*?)  template:",
        rendered,
        re.DOTALL,
    )
    controller_selector = re.search(
        r"kind: Deployment\nmetadata:\n  name: containerssh-config-server-controller\n.*?selector:\n    matchLabels:\n(.*?)  template:",
        rendered,
        re.DOTALL,
    )
    assert api_selector and api_selector.group(1).strip() == "app: containerssh-config-server"
    assert controller_selector and controller_selector.group(1).strip() == "app: containerssh-config-controller"
    assert "name: containerssh-config-service" in rendered
    assert "serviceAccountName: config-server" in rendered
    assert 'value: "/etc/farm-ssh/id_ed25519"' in rendered
    assert 'value: "/etc/farm-ad-ssh/id_ed25519"' in rendered
    assert "mountPath: /etc/infra-ssh" in rendered
    assert "mountPath: /etc/farm-ssh" in rendered
    assert "mountPath: /etc/infra-ad-ssh" in rendered
    assert "mountPath: /etc/farm-ad-ssh" in rendered
    assert "default_realm = LEGACY.EXAMPLE" in rendered
    assert "kdc = 192.0.2.31" in rendered
    assert "admin_server = 192.0.2.31" in rendered
    assert ".ailab.dgu = LEGACY.EXAMPLE" in rendered
    for secret in ("config-server-db-secret", "log-mysql-secret"):
        assert f'name: "{secret}"' in rendered
    for secret in ("nas-ssh-key", "farm-ssh-key", "farm-ad-ssh-key"):
        assert f'secretName: "{secret}"' in rendered


def test_chart_rejects_missing_cluster_id():
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm is not installed")
    version = subprocess.run([helm, "version", "--short"], capture_output=True, text=True)
    if version.returncode:
        pytest.skip(f"helm cannot run in this environment: {version.stderr.strip()}")
    rendered = subprocess.run(
        [helm, "template", "missing-cluster", str(CHART), "--set", "image.tag=test"],
        capture_output=True, text=True, timeout=30,
    )
    assert rendered.returncode != 0
    assert "cluster.id" in rendered.stderr


def test_chart_rejects_unknown_cluster_id():
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm is not installed")
    version = subprocess.run([helm, "version", "--short"], capture_output=True, text=True)
    if version.returncode:
        pytest.skip(f"helm cannot run in this environment: {version.stderr.strip()}")
    rendered = subprocess.run(
        [helm, "template", "unknown-cluster", str(CHART),
         "--set", "cluster.id=staging", "--set", "image.tag=test"],
        capture_output=True, text=True, timeout=30,
    )
    assert rendered.returncode != 0
    assert "farm" in rendered.stderr and "lab" in rendered.stderr


def test_chart_rejects_lab_with_farm_legacy_selector():
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm is not installed")
    version = subprocess.run([helm, "version", "--short"], capture_output=True, text=True)
    if version.returncode:
        pytest.skip(f"helm cannot run in this environment: {version.stderr.strip()}")
    rendered = subprocess.run(
        [helm, "template", "lab-config", str(CHART),
         "--set", "cluster.id=lab",
         "--set", "compatibility.deploymentSelectorMode=legacy",
         "--set", "image.tag=test"],
        capture_output=True, text=True, timeout=30,
    )
    assert rendered.returncode != 0
    assert "LAB" in rendered.stderr and "legacy selector" in rendered.stderr


def test_lab_release_does_not_inherit_farm_ssh_values():
    rendered = _render(
        "lab-config",
        "values.yaml",
        "--set", "cluster.id=lab",
        "--set", "compatibility.deploymentSelectorMode=release",
        "--set", "image.tag=test",
        "--set", "farm.ssh.user=farm-secret-user",
        "--set", "farm.ssh.keyPath=/farm-secret/id_ed25519",
        "--set", "farm.adSsh.user=farm-ad-secret-user",
        "--set", "farm.adSsh.keyPath=/farm-ad-secret/id_ed25519",
    )
    for farm_value in (
        "farm-secret-user",
        "/farm-secret/id_ed25519",
        "farm-ad-secret-user",
        "/farm-ad-secret/id_ed25519",
    ):
        assert farm_value not in rendered


def test_lab_release_does_not_accept_legacy_farm_kdc_host():
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm is not installed")
    version = subprocess.run([helm, "version", "--short"], capture_output=True, text=True)
    if version.returncode:
        pytest.skip(f"helm cannot run in this environment: {version.stderr.strip()}")
    rendered = subprocess.run(
        [helm, "template", "lab-config", str(CHART),
         "--set", "cluster.id=lab",
         "--set", "compatibility.deploymentSelectorMode=release",
         "--set", "image.tag=test",
         "--set", "krb5.realm=LAB.EXAMPLE",
         "--set", "krb5.kdcHost=192.0.2.10"],
        capture_output=True, text=True, timeout=30,
    )
    assert rendered.returncode != 0
    assert "krb5.kdcHosts" in rendered.stderr


def test_lab_release_uses_only_its_explicit_kdc_and_domain_settings():
    rendered = _render(
        "lab-config",
        "values.yaml",
        "--set", "cluster.id=lab",
        "--set", "compatibility.deploymentSelectorMode=release",
        "--set", "image.tag=test",
        "--set", "krb5.realm=LAB.EXAMPLE",
        "--set", "krb5.kdcHosts[0]=198.51.100.10",
        "--set", "krb5.kdcHost=192.0.2.10",
    )
    assert "kdc = 198.51.100.10" in rendered
    assert "192.0.2.10" not in rendered
    assert ".ailab.dgu" not in rendered


def test_make_deploy_binds_helm_and_resource_namespace_and_selector_mode():
    makefile = (ROOT / "Makefile").read_text()
    assert "--set config.namespace=$(NAMESPACE)" in makefile
    assert "--set compatibility.deploymentSelectorMode=$(SELECTOR_MODE)" in makefile
    assert "SELECTOR_MODE ?= legacy" in makefile
