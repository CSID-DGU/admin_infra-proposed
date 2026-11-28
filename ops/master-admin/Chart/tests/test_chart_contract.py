from pathlib import Path
import os
import shlex
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


def run_restore_empty_gate(script: str, data_dir: Path):
    """Execute the rendered gate against a same-owner 0700 fixture.

    Kubernetes mounts the real target as 999:999:0700 and the chart now runs
    the gate as 999:999.  The unprivileged test process cannot chown to 999, so
    this substitutes only the mount path and its expected owner with the
    current test identity while preserving the rendered glob and symlink
    checks byte-for-byte.
    """
    assert "test \"$(stat -Lc '%u:%g:%a' /data)\" = \"999:999:700\"" in script
    fixture_script = script.replace("/data", shlex.quote(str(data_dir)))
    fixture_script = fixture_script.replace(
        "999:999:700", f"{os.getuid()}:{os.getgid()}:700"
    )
    return subprocess.run(
        ["/bin/sh", "-ec", fixture_script],
        check=False,
        capture_output=True,
        text=True,
    )


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
    assert 'test ! -L "$receipt"' in mysql_receipt
    assert "0:0:444:1" in mysql_receipt
    assert "verified_at=" in mysql_receipt

    redis_script = redis["spec"]["containers"][0]["args"][0]
    assert "+@read" in redis_script
    assert "+@all" not in redis_script
    redis_receipt = redis["spec"]["initContainers"][0]["args"][0]
    assert "artifact_sha256=2222222222222222222222222222222222222222222222222222222222222222" in redis_receipt
    assert 'test ! -L "$receipt"' in redis_receipt
    assert "0:0:444:1" in redis_receipt

    # The retained local paths are provisioned as 999:999 before the first
    # mount.  fsGroup would recursively chgrp/chmod the root-owned receipt and
    # make the exact restart gate nondeterministic.
    for datastore in (mysql, redis):
        pod_security = datastore["spec"]["securityContext"]
        assert pod_security["runAsUser"] == 999
        assert pod_security["runAsGroup"] == 999
        assert "fsGroup" not in pod_security
        assert "fsGroupChangePolicy" not in pod_security

        receipt_gate_security = datastore["spec"]["initContainers"][0]["securityContext"]
        assert receipt_gate_security["runAsNonRoot"] is True
        assert receipt_gate_security["runAsUser"] == 999
        assert receipt_gate_security["runAsGroup"] == 999
        assert receipt_gate_security["allowPrivilegeEscalation"] is False
        assert receipt_gate_security["readOnlyRootFilesystem"] is True
        assert receipt_gate_security["capabilities"]["drop"] == ["ALL"]

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

    backend_policy = indexed[("NetworkPolicy", "master-admin-backend")]
    monitoring_egress = [
        rule
        for rule in backend_policy["spec"]["egress"]
        if any(
            peer.get("namespaceSelector", {}).get("matchLabels", {}).get(
                "kubernetes.io/metadata.name"
            )
            == "provi-monitoring"
            for peer in rule.get("to", [])
        )
    ]
    assert len(monitoring_egress) == 1
    monitoring_rule = monitoring_egress[0]
    assert monitoring_rule["to"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "provi-monitoring"}
            },
            "podSelector": {
                "matchLabels": {
                    "prometheus": "master-monitoring-kube-prometheus-prometheus"
                }
            },
        }
    ]
    assert monitoring_rule["ports"] == [{"protocol": "TCP", "port": 9090}]


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
        (
            "--set-string",
            "networkPolicy.backendMonitoringEgress.namespace=default",
        ),
        (
            "--set-string",
            "networkPolicy.backendMonitoringEgress.prometheusLabel=prometheus",
        ),
        ("--set", "networkPolicy.backendMonitoringEgress.port=9091"),
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
    for datastore in (mysql, redis):
        init_containers = datastore["spec"]["initContainers"]
        assert len(init_containers) == 1
        empty_gate = init_containers[0]
        assert empty_gate["name"] == "verify-empty-restore-target"
        assert empty_gate["image"] == datastore["spec"]["containers"][0]["image"]
        assert "@sha256:" in empty_gate["image"]
        assert empty_gate["imagePullPolicy"] == datastore["spec"]["containers"][0]["imagePullPolicy"]
        assert empty_gate["command"] == ["/bin/sh", "-ec"]
        empty_gate_script = empty_gate["args"][0]
        assert "test -d /data" in empty_gate_script
        assert "test ! -L /data" in empty_gate_script
        assert "stat -Lc '%u:%g:%a' /data" in empty_gate_script
        assert '999:999:700' in empty_gate_script
        assert "/data/.[!.]* /data/..?* /data/*" in empty_gate_script
        assert "RESTORE target is not empty" in empty_gate_script
        assert ".provi-restore-receipt" not in empty_gate_script
        for forbidden_mutation in ("rm ", "mv ", "chown ", "chmod ", "touch ", "> /data"):
            assert forbidden_mutation not in empty_gate_script

        gate_security = empty_gate["securityContext"]
        assert gate_security["runAsNonRoot"] is True
        assert gate_security["runAsUser"] == 999
        assert gate_security["runAsGroup"] == 999
        assert gate_security["allowPrivilegeEscalation"] is False
        assert gate_security["readOnlyRootFilesystem"] is True
        assert gate_security["capabilities"]["drop"] == ["ALL"]
        assert empty_gate["volumeMounts"] == [
            {"name": "data", "mountPath": "/data", "readOnly": True}
        ]
    assert "--read-only=OFF" in mysql["spec"]["containers"][0]["args"]
    assert "+@all" in redis["spec"]["containers"][0]["args"][0]
    assert "-@dangerous" not in redis["spec"]["containers"][0]["args"][0]


def test_restore_empty_gate_executes_same_owner_normal_hidden_and_symlink_fixtures(tmp_path):
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
    scripts = [
        pod_template(indexed[("StatefulSet", "master-admin-mysql")])["spec"]
        ["initContainers"][0]["args"][0],
        pod_template(indexed[("StatefulSet", "master-admin-redis")])["spec"]
        ["initContainers"][0]["args"][0],
    ]

    for index, script in enumerate(scripts):
        empty = tmp_path / f"empty-{index}"
        empty.mkdir(mode=0o700)
        empty.chmod(0o700)
        assert run_restore_empty_gate(script, empty).returncode == 0

        fixtures = (
            ("normal", lambda path: path.write_text("not-empty", encoding="utf-8")),
            (".hidden", lambda path: path.write_text("not-empty", encoding="utf-8")),
            ("..double-hidden", lambda path: path.write_text("not-empty", encoding="utf-8")),
            ("dangling", lambda path: path.symlink_to(path.parent / "missing-target")),
        )
        for fixture_name, create_fixture in fixtures:
            target = tmp_path / f"{fixture_name}-{index}"
            target.mkdir(mode=0o700)
            target.chmod(0o700)
            create_fixture(target / fixture_name)
            result = run_restore_empty_gate(script, target)
            assert result.returncode != 0, fixture_name
            assert "RESTORE target is not empty:" in result.stderr


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
