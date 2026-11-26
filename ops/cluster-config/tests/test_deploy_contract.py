from pathlib import Path
import os
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy.sh"


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
    assert "create secret" not in source


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
    assert "archive --format=tar HEAD:config-server/Chart" in source
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


def test_lab_api_bootstrap_writer_and_dependency_gates_are_explicit():
    source = SCRIPT.read_text()
    for value in (
        "disabled-until-phase7b",
        "lab-config-ledger",
        "lab-config-image-store",
        'controller.enabled == false',
        'cronjobs.gpuCheck.enabled == false',
        'cronjobs.krb5Reconcile.enabled == false',
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
    ):
        assert value in source
    assert '"$TARGET" == lab && "$ACTION" == probe' in source
    assert 'lab_bootstrap_args=(--set api.readOnly=true)' in source


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
