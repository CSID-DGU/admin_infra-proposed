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
        assert 'name: CONFIG_API_READ_ONLY\n              value: "false"' in rendered
        assert 'name: CONFIG_API_MAINTENANCE\n              value: "false"' in rendered
        assert f"name: {release}-gpu-check" in rendered
        assert f"name: {release}-krb5-reconcile" in rendered
        assert rendered.count(f'provi.dgu.ac.kr/role: {role}') >= 3
    else:
        # 신규 LAB은 API health/read-only 검증 전까지 controller와 주기 작업을 만들지 않는다.
        assert 'name: CONFIG_API_READ_ONLY\n              value: "true"' in rendered
        assert 'name: CONFIG_API_MAINTENANCE\n              value: "false"' in rendered
        assert f"name: {release}-gpu-check" not in rendered
        assert f"name: {release}-krb5-reconcile" not in rendered
        assert f"name: {release}-controller" not in rendered
    for suffix in ("-db", "-log-db", "-api-token", "-infra-ssh", "-infra-ad-ssh", "-krb5"):
        assert f'{release}{suffix}' in rendered
    assert "FARM_NODES_JSON" not in rendered


def test_lab_read_only_profile_has_no_kubernetes_mutation_rbac():
    rendered = _render("lab-config", "values-lab.example.yaml")
    role = re.search(
        r"kind: Role\nmetadata:\n  name: lab-config-role\n.*?---",
        rendered,
        re.DOTALL,
    )
    assert role
    body = role.group(0)
    manifest_lines = [line for line in body.splitlines() if not line.lstrip().startswith("#")]
    manifest = "\n".join(manifest_lines)
    assert 'resources: ["pods", "pods/log", "services", "persistentvolumeclaims", "events"]' in body
    assert 'verbs: ["get", "list", "watch"]' in body
    for forbidden in ('pods/exec', 'resources: ["secrets"]', '"create"', '"delete"', '"patch"', '"update"'):
        assert forbidden not in manifest


def test_farm_profile_keeps_existing_mutation_rbac():
    rendered = _render("farm-config", "values-farm.example.yaml")
    role = re.search(
        r"kind: Role\nmetadata:\n  name: farm-config-role\n.*?---",
        rendered,
        re.DOTALL,
    )
    assert role
    body = role.group(0)
    assert 'resources: ["pods", "pods/exec", "pods/log", "services"]' in body
    assert 'resources: ["secrets"]' in body
    assert 'verbs: ["get", "list", "watch", "create", "delete", "patch", "update"]' in body


def test_farm_maintenance_profile_closes_admission_and_background_writers():
    rendered = _render(
        "containerssh-config-server", "fixtures/values-legacy-upgrade.yaml",
        "--set", "cluster.id=farm",
        "--set", "image.tag=test",
        "--set", "api.maintenance=true",
        "--set", "controller.replicas=0",
        "--set", "cronjobs.gpuCheck.suspend=true",
        "--set", "cronjobs.krb5Reconcile.suspend=true",
    )
    assert 'name: CONFIG_API_MAINTENANCE\n              value: "true"' in rendered
    controller = re.search(
        r"kind: Deployment\nmetadata:\n  name: containerssh-config-server-controller\n.*?spec:\n  replicas: (\d+)",
        rendered,
        re.DOTALL,
    )
    assert controller and controller.group(1) == "0"
    assert rendered.count("  suspend: true") == 2
    # Maintenance changes values and replicas only; the legacy public name and
    # immutable selectors remain byte-for-byte compatible.
    assert "name: containerssh-config-service" in rendered
    assert "app: containerssh-config-server\n    component: api" in rendered


@pytest.mark.parametrize(
    "override",
    [
        "controller.replicas=1",
        "cronjobs.gpuCheck.suspend=false",
        "cronjobs.krb5Reconcile.suspend=false",
    ],
)
def test_farm_maintenance_rejects_any_live_background_writer(override):
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm is not installed")
    version = subprocess.run([helm, "version", "--short"], capture_output=True, text=True)
    if version.returncode:
        pytest.skip(f"helm cannot run in this environment: {version.stderr.strip()}")
    rendered = subprocess.run(
        [helm, "template", "containerssh-config-server", str(CHART), "-f",
         str(CHART / "fixtures/values-legacy-upgrade.yaml"),
         "--set", "cluster.id=farm",
         "--set", "image.tag=test",
         "--set", "api.maintenance=true",
         "--set", "controller.replicas=0",
         "--set", "cronjobs.gpuCheck.suspend=true",
         "--set", "cronjobs.krb5Reconcile.suspend=true",
         "--set", override],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rendered.returncode != 0
    assert "api.maintenance=true" in rendered.stderr


@pytest.mark.parametrize(
    "override",
    [
        "controller.enabled=true",
        "cronjobs.gpuCheck.enabled=true",
        "cronjobs.krb5Reconcile.enabled=true",
    ],
)
def test_lab_read_only_profile_rejects_background_writers(override):
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm is not installed")
    version = subprocess.run([helm, "version", "--short"], capture_output=True, text=True)
    if version.returncode:
        pytest.skip(f"helm cannot run in this environment: {version.stderr.strip()}")
    rendered = subprocess.run(
        [helm, "template", "lab-config", str(CHART), "-f", str(CHART / "values-lab.example.yaml"),
         "--set", override],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert rendered.returncode != 0
    assert "api.readOnly=true" in rendered.stderr


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
    rendered = _render(
        "lab-config", "values-lab.example.yaml",
        "--set", "api.readOnly=false",
        "--set", value,
    )
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
    legacy_service = re.search(
        r"kind: Service\nmetadata:\n  name: containerssh-config-service\n.*?selector:\n((?:    [^\n]+\n?)+)",
        rendered,
        re.DOTALL,
    )
    assert legacy_service
    selector_lines = [line.strip() for line in legacy_service.group(1).splitlines() if line.strip()]
    assert selector_lines == ["app: containerssh-config-server", "component: api"]
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


def test_lab_release_ignores_legacy_farm_kdc_host_without_direct_client_config():
    rendered = _render(
        "lab-config",
        "values.yaml",
        "--set", "cluster.id=lab",
        "--set", "compatibility.deploymentSelectorMode=release",
        "--set", "image.tag=test",
        "--set", "krb5.realm=LAB.EXAMPLE",
        "--set", "krb5.kdcHost=192.0.2.10",
    )
    assert "default_realm = LAB.EXAMPLE" in rendered
    assert "192.0.2.10" not in rendered
    assert "[realms]" not in rendered
    assert "\n            kdc =" not in rendered
    assert "\n            admin_server =" not in rendered


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


def test_lab_release_can_use_an_independent_persistent_ledger_without_farm_nfs():
    rendered = _render(
        "lab-config",
        "values-lab.example.yaml",
        "--set", "kubeShare.claimName=lab-config-ledger",
    )
    assert "claimName: lab-config-ledger" in rendered
    assert 'server: "198.51.100.31"' not in rendered
    assert 'path: "/REPLACE_WITH_LAB_KUBE_SHARE"' not in rendered


def test_farm_release_keeps_the_existing_nfs_ledger_by_default():
    rendered = _render("farm-config", "values-farm.example.yaml")
    assert 'server: "192.0.2.31"' in rendered
    assert 'path: "/REPLACE_WITH_FARM_KUBE_SHARE"' in rendered


def test_farm_cronjob_suspension_is_chart_owned_and_can_be_flipped_atomically():
    rendered = _render(
        "farm-config",
        "values-farm.example.yaml",
        "--set", "cronjobs.gpuCheck.suspend=true",
        "--set", "cronjobs.krb5Reconcile.suspend=true",
    )
    documents = [document for document in rendered.split("\n---\n") if "kind: CronJob" in document]
    assert len(documents) == 2
    assert all(re.search(r"(?m)^  suspend: true$", document) for document in documents)

    resumed = _render("farm-config", "values-farm.example.yaml")
    resumed_cronjobs = [document for document in resumed.split("\n---\n") if "kind: CronJob" in document]
    assert len(resumed_cronjobs) == 2
    assert all(re.search(r"(?m)^  suspend: false$", document) for document in resumed_cronjobs)


def test_lab_api_profile_wires_only_lab_foundation_and_local_storage_contracts():
    rendered = _render(
        "lab-config", "values-lab.example.yaml",
        "--set-string", "db.host=lab-config-mysql.ailab-operation.svc.cluster.local",
        "--set-string", "db.secretName=lab-config-server-db",
        "--set-string", "logDb.host=lab-config-mysql.ailab-operation.svc.cluster.local",
        "--set-string", "logDb.secretName=lab-config-server-log-db",
        "--set-string", "redis.host=lab-config-redis.ailab-operation.svc.cluster.local",
        "--set-string", "apiToken.secretName=lab-config-server-api-token",
    )
    for exact_value in (
        'value: "lab-config-mysql.ailab-operation.svc.cluster.local"',
        'value: "lab-config-redis.ailab-operation.svc.cluster.local"',
        'name: "lab-config-server-db"',
        'name: "lab-config-server-log-db"',
        'name: "lab-config-server-api-token"',
        "claimName: REPLACE_WITH_LAB_IMAGE_STORE_PVC",
        "claimName: REPLACE_WITH_LAB_KUBE_SHARE_PVC",
    ):
        assert exact_value in rendered
    assert "kind: PersistentVolume" not in rendered
    assert "kind: PersistentVolumeClaim" not in rendered
    assert "nfs:" not in rendered
    assert "192.168.2." not in rendered


def test_release_can_pin_the_config_server_image_by_registry_digest():
    digest = "sha256:" + ("a" * 64)
    rendered = _render(
        "lab-config",
        "values-lab.example.yaml",
        "--set-string", f"image.digest={digest}",
    )
    assert f'image: "dguailab/config-server@{digest}"' in rendered


def test_legacy_farm_digest_keeps_the_existing_service_name():
    digest = "sha256:" + ("b" * 64)
    rendered = _render(
        "containerssh-config-server",
        "fixtures/values-legacy-upgrade.yaml",
        "--set", "cluster.id=farm",
        "--set-string", "image.tag=0123456789abcdef0123456789abcdef01234567",
        "--set-string", f"image.digest={digest}",
    )
    assert "name: containerssh-config-service" in rendered
    assert f'image: "dguailab/config-server@{digest}"' in rendered


def test_release_rejects_an_invalid_registry_digest():
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("helm is not installed")
    version = subprocess.run([helm, "version", "--short"], capture_output=True, text=True)
    if version.returncode:
        pytest.skip(f"helm cannot run in this environment: {version.stderr.strip()}")
    rendered = subprocess.run(
        [helm, "template", "lab-config", str(CHART),
         "-f", str(CHART / "values-lab.example.yaml"),
         "--set-string", "image.digest=sha256:not-a-digest"],
        capture_output=True, text=True, timeout=30,
    )
    assert rendered.returncode != 0
    assert "image.digest" in rendered.stderr


def test_make_deploy_binds_helm_and_resource_namespace_and_selector_mode():
    makefile = (ROOT / "Makefile").read_text()
    assert "--set config.namespace=$(NAMESPACE)" in makefile
    assert "--set compatibility.deploymentSelectorMode=$(SELECTOR_MODE)" in makefile
    assert "SELECTOR_MODE ?= legacy" in makefile
