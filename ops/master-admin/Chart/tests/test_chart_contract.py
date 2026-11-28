from pathlib import Path
import subprocess

import yaml


CHART = Path(__file__).resolve().parents[1]
FIXTURE = CHART / "fixtures" / "values-shadow-test.yaml"


def helm_template(*extra: str, check: bool = True):
    return subprocess.run(
        [
            "helm",
            "template",
            "master-admin",
            str(CHART),
            "--namespace",
            "provi-admin",
            "--values",
            str(FIXTURE),
            *extra,
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def rendered_objects(*extra: str):
    result = helm_template(*extra)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def by_kind_name(objects):
    return {(obj["kind"], obj["metadata"]["name"]): obj for obj in objects}


def pod_template(obj):
    return obj["spec"]["template"]


def test_shadow_render_is_isolated_and_data_retaining():
    objects = rendered_objects()
    indexed = by_kind_name(objects)

    assert all(
        obj["metadata"].get("namespace", "provi-admin") == "provi-admin"
        for obj in objects
        if obj["kind"] not in {"PersistentVolume", "StorageClass"}
    )
    assert ("Ingress", "master-admin") not in indexed
    assert ("StatefulSet", "master-admin-mysql") in indexed
    assert ("StatefulSet", "master-admin-redis") in indexed
    assert ("Deployment", "master-admin-backend") in indexed
    assert ("Deployment", "master-admin-frontend") in indexed

    names = {obj["metadata"]["name"] for obj in objects}
    assert not any("config" in name and name != "master-admin-release-contract" for name in names)
    assert "redis-bg-master" not in names
    assert "infra-mysql" not in names
    assert "log-mysql" not in names
    assert not any(obj["kind"] == "Secret" for obj in objects)

    for suffix in ("mysql", "redis", "backup"):
        pv = indexed[("PersistentVolume", f"provi-master-admin-{suffix}")]
        pvc = indexed[("PersistentVolumeClaim", f"master-admin-{suffix}")]
        assert pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
        assert pv["spec"]["local"]["path"] == f"/var/lib/provi/master-admin/{suffix}"
        assert pv["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
        assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
        assert pvc["spec"]["volumeName"] == f"provi-master-admin-{suffix}"


def test_shadow_write_and_exposure_guards_are_rendered():
    indexed = by_kind_name(rendered_objects())
    mysql = pod_template(indexed[("StatefulSet", "master-admin-mysql")])
    redis = pod_template(indexed[("StatefulSet", "master-admin-redis")])
    backend = pod_template(indexed[("Deployment", "master-admin-backend")])

    mysql_args = mysql["spec"]["containers"][0]["args"]
    assert "--read-only=ON" in mysql_args
    assert "--super-read-only=ON" in mysql_args
    mysql_receipt = mysql["spec"]["initContainers"][0]["args"][0]
    assert "artifact_sha256=1111111111111111111111111111111111111111111111111111111111111111" in mysql_receipt
    assert "0:0:444" in mysql_receipt
    assert "verified_at=" in mysql_receipt

    redis_script = redis["spec"]["containers"][0]["args"][0]
    assert "+@read" in redis_script
    assert "+@all" not in redis_script
    redis_receipt = redis["spec"]["initContainers"][0]["args"][0]
    assert "artifact_sha256=2222222222222222222222222222222222222222222222222222222222222222" in redis_receipt
    assert "0:0:444" in redis_receipt

    backend_container = backend["spec"]["containers"][0]
    assert "--provi.runtime-mode=SHADOW" in backend_container["args"]
    assert "--spring.datasource.hikari.read-only=true" in backend_container["args"]
    assert "--spring.flyway.enabled=false" in backend_container["args"]
    assert "--spring.jpa.hibernate.ddl-auto=validate" in backend_container["args"]
    assert backend["spec"]["automountServiceAccountToken"] is False

    mounts = {mount["name"]: mount for mount in backend_container["volumeMounts"]}
    assert mounts["farm-readonly-kubeconfig"]["mountPath"] == (
        "/etc/provi/kubeconfigs/farm-readonly.yaml"
    )
    volumes = {volume["name"]: volume for volume in backend["spec"]["volumes"]}
    assert volumes["farm-readonly-kubeconfig"]["secret"]["secretName"] == (
        "master-admin-farm-readonly"
    )
    assert not any("lab" in volume["name"].lower() for volume in volumes.values())
    assert not any(
        "lab" in volume.get("secret", {}).get("secretName", "").lower()
        for volume in volumes.values()
    )


def test_provenance_and_immutable_images_are_live_workload_identity():
    objects = rendered_objects()
    indexed = by_kind_name(objects)
    expected = yaml.safe_load(FIXTURE.read_text())["provenance"]

    backend = pod_template(indexed[("Deployment", "master-admin-backend")])
    frontend = pod_template(indexed[("Deployment", "master-admin-frontend")])
    mysql = pod_template(indexed[("StatefulSet", "master-admin-mysql")])
    redis = pod_template(indexed[("StatefulSet", "master-admin-redis")])

    assert backend["metadata"]["annotations"]["provi.dgu.ac.kr/backend-sha"] == expected["backendSha"]
    assert frontend["metadata"]["annotations"]["provi.dgu.ac.kr/frontend-sha"] == expected["frontendSha"]
    for template in (backend, frontend, mysql, redis):
        assert template["metadata"]["annotations"]["provi.dgu.ac.kr/infra-sha"] == expected["infraSha"]
        image = template["spec"]["containers"][0]["image"]
        assert "@sha256:" in image
        assert ":latest" not in image


def test_active_stage_one_preserves_zero_replicas_and_no_ingress():
    objects = rendered_objects(
        "--set-string", "dataMode=ACTIVE",
        "--set-string", "runtimeMode=ACTIVE",
        "--set", "activation.confirmSingleWriter=true",
        "--set", "mysql.readOnly=false",
        "--set", "redis.readOnly=false",
        "--set", "adminBackend.replicas=0",
        "--set", "adminFrontend.replicas=0",
    )
    indexed = by_kind_name(objects)
    assert indexed[("Deployment", "master-admin-backend")]["spec"]["replicas"] == 0
    assert indexed[("Deployment", "master-admin-frontend")]["spec"]["replicas"] == 0
    assert ("Ingress", "master-admin") not in indexed

    mysql_args = pod_template(indexed[("StatefulSet", "master-admin-mysql")])["spec"]["containers"][0]["args"]
    redis_script = pod_template(indexed[("StatefulSet", "master-admin-redis")])["spec"]["containers"][0]["args"][0]
    assert "--read-only=OFF" in mysql_args
    assert "--super-read-only=OFF" in mysql_args
    assert "+@all" in redis_script
    assert "-@dangerous" in redis_script


def test_active_ingress_requires_explicit_host_and_renders_frontend_only():
    objects = rendered_objects(
        "--set-string", "dataMode=ACTIVE",
        "--set-string", "runtimeMode=ACTIVE",
        "--set", "activation.confirmSingleWriter=true",
        "--set", "mysql.readOnly=false",
        "--set", "redis.readOnly=false",
        "--set", "ingress.enabled=true",
        "--set-string", "ingress.host=admin.example.invalid",
    )
    ingress = by_kind_name(objects)[("Ingress", "master-admin")]
    rule = ingress["spec"]["rules"][0]
    assert rule["host"] == "admin.example.invalid"
    assert rule["http"]["paths"][0]["backend"]["service"]["name"] == "master-admin-frontend"


def test_unsafe_value_combinations_fail_before_render():
    cases = (
        ("--set", "ingress.enabled=true"),
        ("--set-string", "runtimeMode=ACTIVE"),
        ("--set-string", "mysql.expectedReceiptSha256="),
        ("--set-string", "redis.expectedReceiptSha256=bad"),
        ("--set", "networkPolicy.enabled=false"),
        ("--set-string", "adminBackend.image.digest=latest"),
        ("--set-string", "provenance.backendSha=short"),
    )
    for args in cases:
        result = helm_template(*args, check=False)
        assert result.returncode != 0, (args, result.stdout, result.stderr)


def test_wrong_release_namespace_is_rejected():
    result = subprocess.run(
        [
            "helm", "template", "master-admin", str(CHART),
            "--namespace", "default", "--values", str(FIXTURE),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "release namespace must be provi-admin" in result.stderr


def test_wrong_release_name_is_rejected():
    result = subprocess.run(
        [
            "helm", "template", "not-master-admin", str(CHART),
            "--namespace", "provi-admin", "--values", str(FIXTURE),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "release name must be master-admin" in result.stderr


def test_restore_mode_is_the_only_empty_pv_bootstrap_contract():
    objects = rendered_objects(
        "--set-string", "dataMode=RESTORE",
        "--set", "mysql.readOnly=false",
        "--set", "redis.readOnly=false",
        "--set", "adminBackend.replicas=0",
        "--set", "adminFrontend.replicas=0",
        "--set-string", "mysql.expectedReceiptSha256=",
        "--set-string", "redis.expectedReceiptSha256=",
    )
    indexed = by_kind_name(objects)
    assert indexed[("Deployment", "master-admin-backend")]["spec"]["replicas"] == 0
    assert indexed[("Deployment", "master-admin-frontend")]["spec"]["replicas"] == 0
    assert ("Ingress", "master-admin") not in indexed

    mysql = pod_template(indexed[("StatefulSet", "master-admin-mysql")])
    redis = pod_template(indexed[("StatefulSet", "master-admin-redis")])
    assert "initContainers" not in mysql["spec"]
    assert "initContainers" not in redis["spec"]
    assert "--read-only=OFF" in mysql["spec"]["containers"][0]["args"]
    assert "+@all" in redis["spec"]["containers"][0]["args"][0]
    assert "-@dangerous" not in redis["spec"]["containers"][0]["args"][0]


def test_restore_mode_rejects_any_application_replica_or_ingress():
    base = (
        "--set-string", "dataMode=RESTORE",
        "--set", "mysql.readOnly=false",
        "--set", "redis.readOnly=false",
        "--set", "adminBackend.replicas=0",
        "--set", "adminFrontend.replicas=0",
        "--set-string", "mysql.expectedReceiptSha256=",
        "--set-string", "redis.expectedReceiptSha256=",
    )
    for override in (
        ("--set", "adminBackend.replicas=1"),
        ("--set", "adminFrontend.replicas=1"),
        ("--set", "ingress.enabled=true"),
    ):
        result = helm_template(*base, *override, check=False)
        assert result.returncode != 0
