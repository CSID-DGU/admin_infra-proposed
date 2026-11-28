from pathlib import Path
from copy import deepcopy
import json
import os
import re
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy.sh"
PHASE7A_VALUES = Path(__file__).parent / "fixtures" / "lab-phase7a-values.json"


def test_shell_syntax_and_help_are_valid():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--target farm|lab" in result.stdout
    assert "--action probe" in result.stdout


def test_entrypoint_never_calls_legacy_or_destructive_paths():
    source = SCRIPT.read_text()
    assert "ops/proposed-stack" not in source
    assert "stack-up.sh" not in source
    assert "stack-down.sh" not in source
    assert "helm upgrade --install" not in source
    assert "helm uninstall" not in source
    assert "kubectl delete" not in source
    assert "kubectl apply" not in source
    assert "kubectl create secret" not in source


def test_existing_release_only_and_rollback_contract_is_explicit():
    source = SCRIPT.read_text()
    assert 'helm status "$RELEASE"' in source
    assert 'helm upgrade "$RELEASE"' in source
    assert "--rollback-on-failure" in source
    assert "--reset-values" in source
    assert '--set-string "image.tag=$IMAGE_TAG"' in source
    assert '--set-string "image.digest=$IMAGE_DIGEST"' in source
    assert "^sha256:[0-9a-f]{64}$" in source
    assert "^[0-9a-f]{40}$" in source
    assert '"$IMAGE_TAG" != "$SOURCE_SHA"' in source
    assert "--untracked-files=all" in source
    assert "archive --format=tar HEAD config-server/Chart" in source
    assert "HEAD:config-server/Chart" not in source
    assert 'helm rollback "$RELEASE" "$PREVIOUS_REVISION"' in source
    assert "CHANGE_STARTED=yes" in source
    assert "POSTCHECK_COMPLETE=yes" in source


def test_fixed_farm_and_lab_boundaries_are_present():
    source = SCRIPT.read_text()
    for value in (
        "farm-mgmt",
        "lab-mgmt",
        "https://192.168.2.198:6443",
        "https://192.168.1.198:6443",
        "config-server-operation",
        "containerssh-config-service",
        "lab-config-server",
        "provi.dgu.ac.kr/role",
        "farm-management",
        "lab-management",
    ):
        assert value in source
    assert '.compatibility.deploymentSelectorMode == "legacy"' in source
    assert '.compatibility.deploymentSelectorMode == "release"' in source


def test_farm_and_lab_user_nodeport_partitions_are_disjoint_and_enforced():
    source = SCRIPT.read_text()
    assert "EXPECTED_NODEPORT_MIN=30000" in source
    assert "EXPECTED_NODEPORT_MAX=30079" in source
    assert "EXPECTED_NODEPORT_MIN=30130" in source
    assert "EXPECTED_NODEPORT_MAX=32767" in source
    assert '.nodeport.min == $nodeport_min' in source
    assert '.nodeport.max == $nodeport_max' in source
    assert '--set "nodeport.min=$EXPECTED_NODEPORT_MIN"' in source
    assert '--set "nodeport.max=$EXPECTED_NODEPORT_MAX"' in source
    assert 'allow_legacy_lab_nodeport' in source


def test_lab_api_bootstrap_writer_and_dependency_gates_are_explicit():
    source = SCRIPT.read_text()
    for value in (
        "disabled-until-phase7b",
        "lab-config-ledger",
        "lab-config-image-store",
        'controller.enabled == false',
        'cronjobs.gpuCheck.enabled == false',
        'cronjobs.krb5Reconcile.enabled == false',
        '.lab.lifecycleDependenciesReady == false',
        '--set lab.lifecycleDependenciesReady=false',
        'nfs.server == ""',
        'krb5.realm == ""',
        'infra.adminBeInternalUrl == "http://127.0.0.1:9"',
        'lab-config-mysql-credentials',
        'lab-config-mysql.ailab-operation.svc.cluster.local',
        'lab-config-redis.ailab-operation.svc.cluster.local',
        '.verifyMode == "noprobe"',
        '.api.readOnly == true',
        'CONFIG_API_READ_ONLY',
        'read_only == true',
        'storageclass lab-config-local',
        'lab-config-ledger-pv',
        'lab-config-images-pv',
        '/var/lib/provi/lab-config-server/ledger',
        '/var/lib/provi/lab-config-server/image-store',
        'mysql:8.0.36',
        'redis:7.4-alpine',
        '/var/lib/provi/lab-config-server/mysql',
        '/var/lib/provi/lab-config-server/redis',
        'lab-config-mysql-ingress',
        'lab-config-redis-ingress',
        'LAB foundation NetworkPolicy drifted',
    ):
        assert value in source
    assert '"$TARGET" == lab && "$ACTION" == probe' in source
    assert 'lab_bootstrap_args=(' in source
    assert '--set api.readOnly=true' in source


def test_lab_phase7a_ssh_placeholder_bridge_is_exact_and_postcheck_is_strict():
    source = SCRIPT.read_text()
    bootstrap_start = source.index(
        "lab_bootstrap_args=(", source.index("lab_bootstrap_args=()") + 1
    )
    bootstrap_end = source.index("\n  )", bootstrap_start)
    bootstrap = source[bootstrap_start:bootstrap_end]

    for override in (
        '--set-string "cluster.ssh.keyPath="',
        '--set-string "cluster.adSsh.keyPath="',
        '--set-string "nas.ssh.keyPath="',
    ):
        assert bootstrap.count(override) == 1
    assert source.count('"${lab_bootstrap_args[@]}"') == 3

    validator_start = source.index("validate_lab_values() {")
    validator_end = source.index(
        "\n}\n\nvalidate_lab_read_only_contract", validator_start
    )
    validator = source[validator_start:validator_end]
    for legacy_path in (
        "/etc/infra-ssh/id_ed25519",
        "/etc/infra-ad-ssh/id_ed25519",
        "/etc/nas-ssh/id_ed25519",
    ):
        assert validator.count(legacy_path) == 1

    assert 'local allow_legacy_phase7a=${1:-false}' in validator
    assert '.cluster.ssh.keyPath == ""' in validator
    assert '.cluster.adSsh.keyPath == ""' in validator
    assert '.nas.ssh.keyPath == ""' in validator
    assert '.nas.ssh.port == ""' in validator
    assert '$allow_legacy_phase7a and' in validator


def _run_lab_values_validator(values, allow_legacy_phase7a):
    source = SCRIPT.read_text()
    validator_start = source.index("validate_lab_values() {")
    validator_end = source.index(
        "\n}\n\nvalidate_lab_read_only_contract", validator_start
    )
    validator = source[validator_start:validator_end]
    match = re.search(
        r"jq -e --argjson allow_legacy_phase7a "
        r'"\$allow_legacy_phase7a" \'(.*?)\' "\$WORK_DIR/current-values.json"',
        validator,
        re.DOTALL,
    )
    assert match, "validate_lab_values jq filter not found"
    return subprocess.run(
        [
            "jq", "-e", "--argjson", "allow_legacy_phase7a",
            str(allow_legacy_phase7a).lower(), match.group(1),
        ],
        input=json.dumps(values),
        text=True,
        capture_output=True,
    )


def test_lab_values_validator_executes_exact_phase7a_transition_matrix():
    legacy = json.loads(PHASE7A_VALUES.read_text())
    assert _run_lab_values_validator(legacy, True).returncode == 0
    assert _run_lab_values_validator(legacy, False).returncode != 0

    normalized = deepcopy(legacy)
    normalized["lab"]["lifecycleDependenciesReady"] = False
    normalized["cluster"]["ssh"]["keyPath"] = ""
    normalized["cluster"]["adSsh"]["keyPath"] = ""
    normalized["nas"]["ssh"]["keyPath"] = ""
    assert _run_lab_values_validator(normalized, True).returncode == 0
    assert _run_lab_values_validator(normalized, False).returncode == 0

    for field_path in (
        ("cluster", "ssh", "keyPath"),
        ("cluster", "adSsh", "keyPath"),
        ("nas", "ssh", "keyPath"),
    ):
        mixed = deepcopy(legacy)
        mixed[field_path[0]][field_path[1]][field_path[2]] = ""
        assert _run_lab_values_validator(mixed, True).returncode != 0

    arbitrary = deepcopy(legacy)
    arbitrary["cluster"]["ssh"]["keyPath"] = "/tmp/unreviewed-key"
    assert _run_lab_values_validator(arbitrary, True).returncode != 0

    nonempty_port = deepcopy(normalized)
    nonempty_port["nas"]["ssh"]["port"] = "22"
    assert _run_lab_values_validator(nonempty_port, True).returncode != 0
    assert _run_lab_values_validator(nonempty_port, False).returncode != 0


def test_lab_rendered_resource_set_is_an_exact_api_only_allowlist():
    source = SCRIPT.read_text()
    expected = {
        "clusterrole.rbac.authorization.k8s.io/lab-config-server-node-reader",
        "clusterrole.rbac.authorization.k8s.io/lab-config-server-service-reader",
        "clusterrolebinding.rbac.authorization.k8s.io/lab-config-server-node-reader-binding",
        "clusterrolebinding.rbac.authorization.k8s.io/lab-config-server-service-reader-binding",
        "configmap/lab-config-server-krb5",
        "deployment.apps/lab-config-server",
        "role.rbac.authorization.k8s.io/lab-config-server-role",
        "rolebinding.rbac.authorization.k8s.io/lab-config-server-rolebinding",
        "service/lab-config-server",
        "serviceaccount/lab-config-server",
    }
    start = source.index("expected_lab_rendered_resources() {")
    end = source.index("\n}\n", start)
    function = source[start:end]
    actual = {line for line in function.splitlines() if "/" in line}

    assert actual == expected
    assert '<(expected_lab_rendered_resources | LC_ALL=C sort)' in source
    assert '<(LC_ALL=C sort "$WORK_DIR/rendered-resources.txt")' in source


def test_lab_probe_and_post_upgrade_check_effective_serviceaccount_contract():
    source = SCRIPT.read_text()
    assert 'EXPECTED_SERVICE_ACCOUNT=lab-config-server' in source
    assert 'system:serviceaccount:${NAMESPACE}:${EXPECTED_SERVICE_ACCOUNT}' in source
    assert source.count("validate_lab_effective_rbac") == 3  # definition + two calls
    assert 'subresource_args=(--subresource="$subresource")' in source
    assert 'answer=$(kubectl auth can-i "$verb" "$resource" "${subresource_args[@]}" "${scope_args[@]}"' in source
    assert '[[ "$answer" == "$expected" ]]' in source
    assert 'namespace) scope_args=(--namespace "$NAMESPACE")' in source
    assert 'all-namespaces) scope_args=(--all-namespaces)' in source

    assert "for resource in pods services persistentvolumeclaims events" in source
    assert "for verb in get list watch" in source
    assert 'validate_lab_can_i "$identity" yes namespace "$verb" "$resource"' in source
    assert 'validate_lab_can_i "$identity" yes namespace "$verb" pods log' in source
    assert 'validate_lab_can_i "$identity" yes cluster "$verb" nodes' in source
    assert 'validate_lab_can_i "$identity" yes all-namespaces "$verb" services' in source

    assert "for resource in secrets configmaps" in source
    assert "for verb in get list watch create update patch delete deletecollection" in source
    assert 'validate_lab_can_i "$identity" no namespace "$verb" "$resource"' in source
    assert "for verb in get create" in source
    assert 'validate_lab_can_i "$identity" no namespace "$verb" pods exec' in source
    assert 'validate_lab_can_i "$identity" no namespace "$verb" pods/exec' not in source
    assert 'validate_lab_can_i "$identity" yes namespace "$verb" pods/log' not in source
    assert "for verb in create update patch delete deletecollection" in source
    assert 'validate_lab_can_i "$identity" no namespace "$verb" pods' in source

    probe_call = source.index("validate_lab_effective_rbac", source.index('"$ACTION" == probe'))
    upgrade_call = source.index("validate_lab_effective_rbac", probe_call + 1)
    post_upgrade_values = source.index("validate_lab_values false", probe_call)
    assert probe_call < post_upgrade_values < upgrade_call


def test_lab_chart_owned_rbac_objects_are_compared_exactly():
    source = SCRIPT.read_text()
    start = source.index("validate_lab_owned_rbac_objects() {")
    end = source.index("\n}\n\nvalidate_lab_can_i()", start)
    contract = source[start:end]

    assert 'get role "${RELEASE}-role" -o json' in contract
    assert 'get rolebinding "${RELEASE}-rolebinding" -o json' in contract
    assert 'get clusterrole "$cluster_role" -o json' in contract
    assert 'get clusterrolebinding "$binding" -o json' in contract
    assert '(.rules | length) == 1' in contract
    assert '(.rules[0] | keys | sort) == ["apiGroups", "resources", "verbs"]' in contract
    assert '["pods", "pods/log", "services", "persistentvolumeclaims", "events"]' in contract
    assert '["get", "list", "watch"]' in contract
    assert '.subjects == [{' in contract
    assert '"kind": "ServiceAccount"' in contract
    assert '"kind": "Role"' in contract
    assert '"kind": "ClusterRole"' in contract
    assert "${RELEASE}-node-reader|nodes" in contract
    assert "${RELEASE}-service-reader|services" in contract
    assert "${RELEASE}-node-reader-binding|${RELEASE}-node-reader" in contract
    assert "${RELEASE}-service-reader-binding|${RELEASE}-service-reader" in contract

    effective = source[source.index("validate_lab_effective_rbac() {"):]
    assert effective.index("validate_lab_owned_rbac_objects") < effective.index(
        'validate_lab_can_i "$identity" yes namespace'
    )


def _lab_can_i_function_source():
    source = SCRIPT.read_text()
    start = source.index("validate_lab_can_i() {")
    end = source.index("\n}\n\nvalidate_lab_effective_rbac()", start)
    return source[start:end + 2]


def _run_lab_can_i_harness(
    tmp_path, *, expected, output, command_rc, resource="pods", subresource=None
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    kubectl = fake_bin / "kubectl"
    kubectl.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        if [[ -n "${CAN_I_ARGS_FILE-}" ]]; then
          printf '%s\n' "$@" >"$CAN_I_ARGS_FILE"
        fi
        printf '%s' "${CAN_I_OUTPUT-}"
        exit "${CAN_I_RC-0}"
    """))
    kubectl.chmod(0o700)

    optional_subresource = "" if subresource is None else f" {subresource}"
    harness = tmp_path / "can-i-harness.sh"
    harness.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -Eeuo pipefail
        PATH={str(fake_bin)!r}:$PATH
        NAMESPACE=ailab-operation
        fail() {{ printf '[ERROR] %s\\n' "$*" >&2; exit 1; }}
        {_lab_can_i_function_source()}
        validate_lab_can_i identity {expected} namespace get {resource}{optional_subresource}
    """))
    return subprocess.run(
        ["bash", str(harness)],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CAN_I_OUTPUT": output,
            "CAN_I_RC": str(command_rc),
            "CAN_I_ARGS_FILE": str(tmp_path / "kubectl-args.txt"),
        },
        capture_output=True,
        text=True,
    )


def test_lab_can_i_accepts_only_the_exact_expected_answer(tmp_path):
    assert _run_lab_can_i_harness(
        tmp_path / "yes", expected="yes", output="yes", command_rc=0
    ).returncode == 0
    assert _run_lab_can_i_harness(
        tmp_path / "no", expected="no", output="no", command_rc=1
    ).returncode == 0

    empty = _run_lab_can_i_harness(
        tmp_path / "empty", expected="no", output="", command_rc=1
    )
    assert empty.returncode == 1
    assert "answer=error" in empty.stderr

    unexpected = _run_lab_can_i_harness(
        tmp_path / "unexpected", expected="yes", output="error", command_rc=1
    )
    assert unexpected.returncode == 1
    assert "answer=error" in unexpected.stderr


def test_lab_can_i_passes_subresources_with_the_explicit_kubectl_flag(tmp_path):
    for subresource, expected, output, command_rc in (
        ("log", "yes", "yes", 0),
        ("exec", "no", "no", 1),
    ):
        case_dir = tmp_path / subresource
        result = _run_lab_can_i_harness(
            case_dir,
            expected=expected,
            output=output,
            command_rc=command_rc,
            resource="pods",
            subresource=subresource,
        )
        assert result.returncode == 0
        assert (case_dir / "kubectl-args.txt").read_text().splitlines() == [
            "auth",
            "can-i",
            "get",
            "pods",
            f"--subresource={subresource}",
            "--namespace",
            "ailab-operation",
            "--as",
            "identity",
        ]

    ordinary_dir = tmp_path / "ordinary"
    result = _run_lab_can_i_harness(
        ordinary_dir, expected="yes", output="yes", command_rc=0
    )
    assert result.returncode == 0
    ordinary_args = (ordinary_dir / "kubectl-args.txt").read_text().splitlines()
    assert "--subresource=exec" not in ordinary_args
    assert ordinary_args[0:4] == ["auth", "can-i", "get", "pods"]


def test_lab_lifecycle_latch_bridge_is_upgrade_only_and_postcheck_is_strict():
    source = SCRIPT.read_text()
    initial_gate = source[source.index("validate_common_values"):source.index("current_tag=")]
    assert 'if [[ "$ACTION" == upgrade ]]' in initial_gate
    assert initial_gate.count("validate_lab_values true") == 1
    assert initial_gate.count("validate_lab_values false") == 1

    post_upgrade = source[source.index("helm get values", source.index("CHANGE_STARTED=yes")):]
    assert "validate_lab_values true" not in post_upgrade
    assert "validate_lab_values false" in post_upgrade
    assert '--set lab.lifecycleDependenciesReady=false' in source
    assert "Legacy Phase 7-A or a missing-latch release may use this recovery bridge" in source
    assert "every post-upgrade check requires that exact value" in source


def test_phase7a_networkpolicy_drift_checks_exact_selector_source_and_port():
    source = SCRIPT.read_text()
    start = source.index("local policy app port")
    end = source.index("! kubectl", start)
    contract = source[start:end]

    assert '.spec == {' in contract
    assert '"podSelector": {"matchLabels": {"app": $app}}' in contract
    assert '"policyTypes": ["Ingress"]' in contract
    assert '"matchLabels": {"app.kubernetes.io/instance": $release}' in contract
    assert '"ports": [{"protocol": "TCP", "port": $port}]' in contract
    assert "lab-config-mysql-ingress|lab-config-mysql|3306" in contract
    assert "lab-config-redis-ingress|lab-config-redis|6379" in contract


def _ordered(source, *needles):
    """Assert that safety-critical commands cannot silently change order."""
    offsets = []
    cursor = 0
    for needle in needles:
        offset = source.index(needle, cursor)
        offsets.append(offset)
        cursor = offset + len(needle)
    return offsets


def test_farm_upgrade_keeps_cronjobs_suspended_in_the_first_helm_revision():
    source = SCRIPT.read_text()
    assert source.count('--set "cronjobs.gpuCheck.suspend=true"') == 1
    assert source.count('--set "cronjobs.krb5Reconcile.suspend=true"') == 1
    assert source.count('--set "cronjobs.gpuCheck.suspend=false"') == 1
    assert source.count('--set "cronjobs.krb5Reconcile.suspend=false"') == 1
    assert source.count('--set "api.maintenance=true"') == 1
    assert source.count('--set "api.maintenance=false"') == 1
    assert source.count('--set "controller.replicas=0"') == 1
    assert source.count('--set "controller.replicas=1"') == 1

    maintenance = source[source.index("[CHANGE] Entering the controlled Config Server maintenance window."):]
    _ordered(
        maintenance,
        'set_farm_cronjobs_suspend true',
        '--set "api.maintenance=true"',
        '--set "controller.replicas=0"',
        '--set "cronjobs.gpuCheck.suspend=true"',
        "require_zero_unfinished_operations",
        "snapshot_protected_pods \"$WORK_DIR/protected-pods-after.tsv\"",
        '--set "api.maintenance=false"',
        '--set "controller.replicas=1"',
        '--set "cronjobs.gpuCheck.suspend=false"',
        "POSTCHECK_COMPLETE=yes",
    )


def test_farm_resume_is_the_final_helm_revision_and_has_no_idle_or_user_drift_gate_after_it():
    source = SCRIPT.read_text()
    resume = source.index('--set "cronjobs.gpuCheck.suspend=false"')
    final_window = source[resume:]

    # A missed schedule may start immediately after resume.  It must not make a
    # healthy deployment look failed, and user workload comparison must already
    # have completed while both CronJobs were suspended.
    assert "require_zero_unfinished_operations" not in final_window
    assert "require_no_active_jobs" not in final_window
    assert "snapshot_protected_pods" not in final_window
    assert "snapshot_managed_workloads" not in final_window
    assert "cmp -s" not in final_window
    assert "validate_farm_cronjobs false false" in final_window


def test_farm_write_admission_and_controller_stay_closed_until_final_revision():
    source = SCRIPT.read_text()
    first = source.index('--set "api.maintenance=true"')
    resume = source.index('--set "api.maintenance=false"', first)
    validation_window = source[first:resume]

    assert '--set "controller.replicas=0"' in validation_window
    assert "validate_farm_transition_values true 0 true" in validation_window
    assert "validate_farm_controller_replicas 0" in validation_window
    assert ".maintenance == true and .read_only == false" in validation_window
    assert "require_zero_unfinished_operations" in validation_window
    assert "snapshot_managed_workloads" in validation_window

    final_window = source[resume:]
    assert '--set "controller.replicas=1"' in final_window
    assert "validate_farm_transition_values false 1 false" in final_window
    assert "validate_farm_controller_replicas 1" in final_window


def test_farm_commit_point_prevents_rollback_under_newly_accepted_work():
    source = SCRIPT.read_text()
    resume_command = source.index('--set "api.maintenance=false"')
    helm_return = source.index("POSTCHECK_COMPLETE=yes", resume_command)
    final_observation = source.index("helm get values", helm_return)
    assert helm_return < final_observation
    assert "MAINTENANCE_STARTED=no" in source[helm_return:final_observation]


def test_api_and_controller_are_scaled_down_before_either_drain_wait():
    source = SCRIPT.read_text()
    scale_api = source.index('scale "deployment/$RELEASE" --replicas=0')
    scale_controller = source.index('scale "deployment/${RELEASE}-controller" --replicas=0')
    wait_api = source.index("wait_for_no_release_pods api")
    wait_controller = source.index("wait_for_no_release_pods controller")
    assert max(scale_api, scale_controller) < min(wait_api, wait_controller)


def _cleanup_function_source():
    source = SCRIPT.read_text()
    start = source.index("cleanup() {")
    end = source.index("\ntrap cleanup EXIT", start)
    return source[start:end]


def _run_cleanup_harness(
        tmp_path, *, change_started, maintenance_started, postcheck_complete="no", helm_rc=0):
    command_log = tmp_path / "commands.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command in ("helm", "kubectl"):
        executable = fake_bin / command
        executable.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            printf '%s %s\\n' {command!r} "$*" >> {str(command_log)!r}
            {'exit ' + str(helm_rc) if command == 'helm' else 'exit 0'}
        """))
        executable.chmod(0o700)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    harness = tmp_path / "cleanup-harness.sh"
    harness.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -u
        PATH={str(fake_bin)!r}:$PATH
        NAMESPACE=ailab-operation
        RELEASE=config-server-operation
        TARGET=farm
        PREVIOUS_REVISION=151
        CHANGE_STARTED={change_started}
        MAINTENANCE_STARTED={maintenance_started}
        POSTCHECK_COMPLETE={postcheck_complete}
        FARM_GPU_CRONJOB=config-server-operation-gpu-check
        FARM_KRB5_CRONJOB=config-server-operation-krb5-reconcile
        WORK_DIR={str(work_dir)!r}
        {_cleanup_function_source()}
        false
        cleanup
    """))
    result = subprocess.run(
        ["bash", str(harness)],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    commands = command_log.read_text() if command_log.exists() else ""
    return result, commands


def test_cleanup_rolls_back_original_revision_and_recovers_all_farm_workloads(tmp_path):
    result, commands = _run_cleanup_harness(
        tmp_path, change_started="yes", maintenance_started="yes"
    )
    assert result.returncode == 1
    assert "helm rollback config-server-operation 151 --namespace ailab-operation" in commands
    assert "kubectl -n ailab-operation scale deployment/config-server-operation --replicas=1" in commands
    assert "kubectl -n ailab-operation scale deployment/config-server-operation-controller --replicas=1" in commands
    assert commands.count("kubectl -n ailab-operation rollout status deployment/") == 2
    assert "cronjob config-server-operation-gpu-check --type=merge" in commands
    assert "cronjob config-server-operation-krb5-reconcile --type=merge" in commands
    assert "Previous Helm revision restored successfully" in result.stderr


def test_cleanup_does_not_rollback_when_mutating_helm_was_never_dispatched(tmp_path):
    result, commands = _run_cleanup_harness(
        tmp_path, change_started="no", maintenance_started="yes"
    )
    assert result.returncode == 1
    assert "helm rollback" not in commands
    assert "deployment/config-server-operation --replicas=1" in commands


def test_cleanup_does_not_rollback_or_restart_after_farm_write_admission_commit(tmp_path):
    result, commands = _run_cleanup_harness(
        tmp_path,
        change_started="yes",
        maintenance_started="no",
        postcheck_complete="yes",
    )
    assert result.returncode == 1
    assert "helm rollback" not in commands
    assert "kubectl" not in commands
