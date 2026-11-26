#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
  cat <<'EOF'
Usage:
  deploy.sh --target farm|lab --action probe
  deploy.sh --target farm|lab --action upgrade --image-tag <40-char-source-sha> \
    --image-digest <sha256:64-char-registry-digest>

This command only probes or upgrades an existing Config Server Helm release with the
exact chart and digest-pinned image from one protected source commit.
It never installs, deletes, or recreates a release, Secret, database, or PVC.
EOF
}

fail() {
  printf '[ERROR] %s\n' "$*" >&2
  exit 1
}

TARGET=
ACTION=
IMAGE_TAG=
IMAGE_DIGEST=
while (($#)); do
  case "$1" in
    --target)
      (($# >= 2)) || fail '--target requires a value'
      TARGET=$2
      shift 2
      ;;
    --action)
      (($# >= 2)) || fail '--action requires a value'
      ACTION=$2
      shift 2
      ;;
    --image-tag)
      (($# >= 2)) || fail '--image-tag requires a value'
      IMAGE_TAG=$2
      shift 2
      ;;
    --image-digest)
      (($# >= 2)) || fail '--image-digest requires a value'
      IMAGE_DIGEST=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unsupported argument: $1"
      ;;
  esac
done

case "$TARGET" in
  farm|lab) ;;
  *) fail '--target must be farm or lab' ;;
esac
case "$ACTION" in
  probe)
    [[ -z "$IMAGE_TAG" ]] || fail 'probe must not receive --image-tag'
    [[ -z "$IMAGE_DIGEST" ]] || fail 'probe must not receive --image-digest'
    ;;
  upgrade)
    [[ "$IMAGE_TAG" =~ ^[0-9a-f]{40}$ ]] || fail 'upgrade requires a lowercase 40-character source SHA image tag'
    [[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
      || fail 'upgrade requires a sha256 registry image digest'
    ;;
  *) fail '--action must be probe or upgrade' ;;
esac

for command in awk bash chmod cmp git grep helm hostname id jq kubectl mktemp rm rmdir sha256sum sleep sort stat tr; do
  command -v "$command" >/dev/null || fail "required command not found: $command"
done

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
CHART="$REPO_ROOT/config-server/Chart"
[[ -f "$CHART/Chart.yaml" && ! -L "$CHART/Chart.yaml" ]] || fail 'Config Server chart is missing or symlinked'
[[ $(git -C "$REPO_ROOT" rev-parse --show-toplevel) == "$REPO_ROOT" ]] || fail 'entrypoint is not inside the expected repository root'
[[ -z $(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all -- .) ]] \
  || fail 'refusing a modified, staged, or untracked source checkout'
SOURCE_SHA=$(git -C "$REPO_ROOT" rev-parse HEAD)
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail 'source checkout is not pinned to a full commit SHA'
if [[ "$ACTION" == upgrade && "$IMAGE_TAG" != "$SOURCE_SHA" ]]; then
  fail 'upgrade image tag must equal the exact checked-out source SHA'
fi
CHART_SHA256=$(git -C "$REPO_ROOT" archive --format=tar HEAD:config-server/Chart \
  | sha256sum | awk '{print $1}')
[[ "$CHART_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail 'failed to calculate the immutable chart digest'

readonly EXPECTED_KUBECONFIG=/etc/kubernetes/ci-deployer.conf
[[ ${KUBECONFIG:-} == "$EXPECTED_KUBECONFIG" ]] || fail "KUBECONFIG must be $EXPECTED_KUBECONFIG"
[[ -f "$KUBECONFIG" && ! -L "$KUBECONFIG" && -r "$KUBECONFIG" ]] || fail 'deployment kubeconfig is missing, symlinked, or unreadable'
[[ $(stat -Lc '%U:%G:%a' "$KUBECONFIG") == root:svmanager:640 ]] || fail 'unexpected deployment kubeconfig metadata'

NAMESPACE=ailab-operation
EXPECTED_IMAGE_REPOSITORY=dguailab/config-server-exp
FARM_GPU_CRONJOB=config-server-operation-gpu-check
FARM_KRB5_CRONJOB=config-server-operation-krb5-reconcile
case "$TARGET" in
  farm)
    EXPECTED_HOST=farm-mgmt
    EXPECTED_API=https://192.168.2.198:6443
    RELEASE=config-server-operation
    EXPECTED_SERVICE=containerssh-config-service
    EXPECTED_CLUSTER_ID=farm
    EXPECTED_NODE_ROLE=farm-management
    EXPECTED_NODE_PORT=30482
    ;;
  lab)
    EXPECTED_HOST=lab-mgmt
    EXPECTED_API=https://192.168.1.198:6443
    RELEASE=lab-config-server
    EXPECTED_SERVICE=lab-config-server
    EXPECTED_CLUSTER_ID=lab
    EXPECTED_NODE_ROLE=lab-management
    EXPECTED_NODE_PORT=30082
    ;;
esac

[[ $(hostname -s) == "$EXPECTED_HOST" ]] || fail "run $TARGET only on $EXPECTED_HOST"
[[ $(id -un) == svmanager ]] || fail 'run this entrypoint as svmanager'
[[ $(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}') == "$EXPECTED_API" ]] || fail 'unexpected Kubernetes API endpoint'
[[ $(kubectl --request-timeout=20s get --raw=/readyz) == ok ]] || fail 'Kubernetes API is not ready'
[[ $(kubectl get namespace "$NAMESPACE" -o json | jq -r '.metadata.name') == "$NAMESPACE" ]] || fail 'target namespace is absent'
kubectl get nodes -o json | jq -e '
  (.items | length) > 0 and
  all(.items[]; any(.status.conditions[]?; .type == "Ready" and .status == "True"))
' >/dev/null || fail 'one or more cluster Nodes are not Ready'

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/provi-cluster-config.XXXXXXXX")
# This flag means that the mutating Helm command has been dispatched, not that it
# returned successfully.  It is intentionally set immediately before `helm
# upgrade` so an error, timeout, or signal during that command still triggers an
# explicit rollback to the recorded revision.
CHANGE_STARTED=no
MAINTENANCE_STARTED=no
POSTCHECK_COMPLETE=no
PREVIOUS_REVISION=
cleanup() {
  local rc=$?
  local recovery_rc=0
  local cronjob
  trap - EXIT INT TERM
  set +e
  if (( rc != 0 )) && [[ "$CHANGE_STARTED" == yes && "$POSTCHECK_COMPLETE" != yes ]]; then
    printf '[ROLLBACK] Upgrade failed or was interrupted; restoring %s/%s to revision %s\n' \
      "$NAMESPACE" "$RELEASE" "$PREVIOUS_REVISION" >&2
    if helm rollback "$RELEASE" "$PREVIOUS_REVISION" \
      --namespace "$NAMESPACE" --wait --timeout 12m; then
      printf '[ROLLBACK] Previous Helm revision restored successfully.\n' >&2
    else
      printf '[CRITICAL] Automatic rollback failed; inspect %s/%s before rerunning.\n' \
        "$NAMESPACE" "$RELEASE" >&2
      recovery_rc=1
    fi
  fi
  if (( rc != 0 )) && [[ "$MAINTENANCE_STARTED" == yes ]]; then
    printf '[RECOVERY] Restoring the Config Server workloads after the stopped deployment.\n' >&2
    kubectl -n "$NAMESPACE" scale "deployment/$RELEASE" --replicas=1 >/dev/null 2>&1 \
      || recovery_rc=1
    if [[ "$TARGET" == farm ]]; then
      kubectl -n "$NAMESPACE" scale "deployment/${RELEASE}-controller" --replicas=1 \
        >/dev/null 2>&1 || recovery_rc=1
    fi
    kubectl -n "$NAMESPACE" rollout status "deployment/$RELEASE" --timeout=12m \
      >/dev/null 2>&1 || recovery_rc=1
    if [[ "$TARGET" == farm ]]; then
      kubectl -n "$NAMESPACE" rollout status "deployment/${RELEASE}-controller" --timeout=12m \
        >/dev/null 2>&1 || recovery_rc=1
      for cronjob in "$FARM_GPU_CRONJOB" "$FARM_KRB5_CRONJOB"; do
        kubectl -n "$NAMESPACE" patch cronjob "$cronjob" --type=merge \
          -p '{"spec":{"suspend":false}}' >/dev/null 2>&1 || recovery_rc=1
      done
    fi
    if (( recovery_rc == 0 )); then
      printf '[RECOVERY] API, controller, and CronJob state restored.\n' >&2
    else
      printf '[CRITICAL] Recovery was incomplete; inspect %s/%s before rerunning.\n' \
        "$NAMESPACE" "$RELEASE" >&2
    fi
  fi
  rm -f -- "$WORK_DIR/current-values.json" "$WORK_DIR/current-values.yaml" \
    "$WORK_DIR/rendered.yaml" "$WORK_DIR/rendered-resources.txt" \
    "$WORK_DIR/protected-pods-before.tsv" "$WORK_DIR/protected-pods-after.tsv" \
    "$WORK_DIR/managed-pods-before.tsv" "$WORK_DIR/managed-pods-after.tsv" \
    "$WORK_DIR/managed-services-before.tsv" "$WORK_DIR/managed-services-after.tsv" \
    "$WORK_DIR/helm-status.json"
  rmdir -- "$WORK_DIR" 2>/dev/null || true
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
chmod 700 "$WORK_DIR"

helm status "$RELEASE" --namespace "$NAMESPACE" --output json >"$WORK_DIR/helm-status.json" \
  || fail "existing Helm release not found: $NAMESPACE/$RELEASE"
jq -e '.info.status == "deployed"' "$WORK_DIR/helm-status.json" >/dev/null || fail 'existing Helm release is not deployed'
PREVIOUS_REVISION=$(jq -r '.version' "$WORK_DIR/helm-status.json")
[[ "$PREVIOUS_REVISION" =~ ^[1-9][0-9]*$ ]] || fail 'could not determine the current Helm revision'
helm get values "$RELEASE" --namespace "$NAMESPACE" --all --output json >"$WORK_DIR/current-values.json"
helm get values "$RELEASE" --namespace "$NAMESPACE" --all --output yaml >"$WORK_DIR/current-values.yaml"
chmod 600 "$WORK_DIR"/*

validate_common_values() {
  jq -e \
    --arg cluster "$EXPECTED_CLUSTER_ID" \
    --arg role "$EXPECTED_NODE_ROLE" \
    --arg repository "$EXPECTED_IMAGE_REPOSITORY" \
    --arg namespace "$NAMESPACE" \
    --argjson node_port "$EXPECTED_NODE_PORT" '
      .cluster.id == $cluster and
      .image.repository == $repository and
      (.image.tag | test("^[0-9a-f]{40}$")) and
      (((.image.digest // "") == "") or ((.image.digest // "") | test("^sha256:[0-9a-f]{64}$"))) and
      .image.pullPolicy == "IfNotPresent" and
      .config.namespace == $namespace and
      .service.type == "NodePort" and
      .service.nodePort == $node_port and
      .nodeSelector["provi.dgu.ac.kr/role"] == $role
    ' "$WORK_DIR/current-values.json" >/dev/null || fail "$TARGET release values violate the fixed deployment contract"
}

validate_farm_values() {
  jq -e '
      .compatibility.deploymentSelectorMode == "legacy" and
      .controller.enabled == true and
      .cronjobs.gpuCheck.enabled == true and
      .cronjobs.krb5Reconcile.enabled == true and
      ((.kubeShare.claimName // "") == "") and
      (.nfs.server | type == "string" and length > 0) and
      (.nfs.kubeSharePath | type == "string" and length > 1)
    ' "$WORK_DIR/current-values.json" >/dev/null || fail 'FARM release no longer matches the reviewed NFS/controller contract'
}

validate_farm_transition_values() {
  local expected_maintenance=$1
  local expected_controller_replicas=$2
  local expected_cron_suspend=$3
  jq -e \
    --argjson maintenance "$expected_maintenance" \
    --argjson controller_replicas "$expected_controller_replicas" \
    --argjson cron_suspend "$expected_cron_suspend" '
      ((.api.maintenance // false) == $maintenance) and
      ((.controller.replicas // 1) == $controller_replicas) and
      ((.cronjobs.gpuCheck.suspend // false) == $cron_suspend) and
      ((.cronjobs.krb5Reconcile.suspend // false) == $cron_suspend)
    ' "$WORK_DIR/current-values.json" >/dev/null \
    || fail 'FARM API/controller/CronJob transition values violate the deployment contract'
}

validate_farm_controller_replicas() {
  local expected=$1
  kubectl -n "$NAMESPACE" get deployment "${RELEASE}-controller" -o json | jq -e \
    --argjson expected "$expected" '
      .spec.replicas == $expected and
      ((.status.readyReplicas // 0) == $expected) and
      ((.status.availableReplicas // 0) == $expected)
    ' >/dev/null || fail "FARM controller replicas did not converge to $expected"
}

validate_lab_values() {
  jq -e '
      .compatibility.deploymentSelectorMode == "release" and
      .cluster.homeMountRootTemplate == "" and
      .cluster.ssh.user == "disabled-until-phase7b" and
      .cluster.adSsh.user == "disabled-until-phase7b" and
      ((.cluster.adSsh.nodes // []) | length == 0) and
      .cluster.ssh.nodes == [
        {"name":"lab1", "host":"192.168.1.11", "port":8081},
        {"name":"lab2", "host":"192.168.1.12", "port":8082},
        {"name":"lab3", "host":"192.168.1.13", "port":8083},
        {"name":"lab4", "host":"192.168.1.14", "port":8084},
        {"name":"lab5", "host":"192.168.1.15", "port":8085},
        {"name":"lab6", "host":"192.168.1.16", "port":8086},
        {"name":"lab7", "host":"192.168.1.17", "port":8087},
        {"name":"lab8", "host":"192.168.1.18", "port":8088},
        {"name":"lab9", "host":"192.168.1.19", "port":8089},
        {"name":"lab10", "host":"192.168.1.110", "port":8090}
      ] and
      .nfs.server == "" and
      .nfs.userSharePath == "" and
      .nfs.kubeSharePath == "" and
      .nas.ssh.host == "" and
      .nas.ssh.user == "disabled-until-phase7b" and
      .krb5.realm == "" and
      ((.krb5.kdcHosts // []) | length == 0) and
      .krb5.adminServer == "" and
      .krb5.domain == "" and
      .infra.adminBeInternalUrl == "http://127.0.0.1:9" and
      .monitoring.prometheusUrl == "http://127.0.0.1:9" and
      .db.host == "lab-config-mysql.ailab-operation.svc.cluster.local" and
      .db.name == "pod_port_db" and
      .db.secretName == "lab-config-server-db" and
      .logDb.host == "lab-config-mysql.ailab-operation.svc.cluster.local" and
      .logDb.name == "operation_state_db" and
      .logDb.secretName == "lab-config-server-log-db" and
      .redis.host == "lab-config-redis.ailab-operation.svc.cluster.local" and
      .apiToken.secretName == "lab-config-server-api-token" and
      .imageStore.claimName == "lab-config-image-store" and
      .kubeShare.claimName == "lab-config-ledger" and
      .verifyMode == "noprobe" and
      .faultInjection == false and
      .controller.enabled == false and
      .cronjobs.gpuCheck.enabled == false and
      .cronjobs.krb5Reconcile.enabled == false
    ' "$WORK_DIR/current-values.json" >/dev/null || fail 'LAB API-bootstrap values are no longer fail-closed'

  for secret in \
    lab-config-mysql-credentials lab-config-server-db lab-config-server-log-db lab-config-server-api-token \
    lab-config-server-infra-ssh lab-config-server-infra-ad-ssh lab-config-server-nas-ssh; do
    kubectl -n "$NAMESPACE" get secret "$secret" >/dev/null || fail "required LAB Secret is absent: $secret"
  done
  kubectl get storageclass lab-config-local -o json | jq -e '
    .provisioner == "kubernetes.io/no-provisioner" and
    .reclaimPolicy == "Retain" and
    .volumeBindingMode == "WaitForFirstConsumer"
  ' >/dev/null || fail 'LAB foundation StorageClass drifted from the exact Phase 7-A contract'

  local claim volume path size component
  while IFS='|' read -r claim volume path size component; do
    kubectl -n "$NAMESPACE" get pvc "$claim" -o json | jq -e \
      --arg volume "$volume" --arg size "$size" '
        .status.phase == "Bound" and
        .spec.storageClassName == "lab-config-local" and
        .spec.volumeName == $volume and
        .spec.accessModes == ["ReadWriteOnce"] and
        .spec.resources.requests.storage == $size
      ' >/dev/null || fail "LAB foundation PVC drifted: $claim"
    kubectl get pv "$volume" -o json | jq -e \
      --arg namespace "$NAMESPACE" --arg claim "$claim" --arg path "$path" \
      --arg size "$size" --arg component "$component" '
        .metadata.labels["provi.dgu.ac.kr/cluster-id"] == "lab" and
        .metadata.labels["provi.dgu.ac.kr/component"] == $component and
        .spec.persistentVolumeReclaimPolicy == "Retain" and
        .spec.storageClassName == "lab-config-local" and
        .spec.volumeMode == "Filesystem" and
        .spec.accessModes == ["ReadWriteOnce"] and
        .spec.capacity.storage == $size and
        .spec.hostPath.path == $path and
        .spec.hostPath.type == "DirectoryOrCreate" and
        .spec.claimRef.namespace == $namespace and
        .spec.claimRef.name == $claim and
        any(.spec.nodeAffinity.required.nodeSelectorTerms[]?.matchExpressions[]?;
          .key == "kubernetes.io/hostname" and .operator == "In" and .values == ["lab-mgmt"])
      ' >/dev/null || fail "LAB foundation PV drifted: $volume"
  done <<'EOF'
lab-config-ledger|lab-config-ledger-pv|/var/lib/provi/lab-config-server/ledger|1Gi|config-ledger
lab-config-image-store|lab-config-images-pv|/var/lib/provi/lab-config-server/image-store|50Gi|image-store
EOF

  local deployment image volume_name mount_path host_path service port
  while IFS='|' read -r deployment image volume_name mount_path host_path service port; do
    kubectl -n "$NAMESPACE" get deployment "$deployment" -o json | jq -e \
      --arg app "$deployment" --arg image "$image" --arg volume "$volume_name" \
      --arg mount "$mount_path" --arg path "$host_path" '
        .spec.replicas == 1 and
        .spec.strategy.type == "Recreate" and
        .spec.selector.matchLabels == {"app": $app} and
        .spec.template.metadata.labels.app == $app and
        .spec.template.metadata.labels["provi.dgu.ac.kr/cluster-id"] == "lab" and
        .spec.template.spec.nodeSelector == {"provi.dgu.ac.kr/role": "lab-management"} and
        (.spec.template.spec.containers | length) == 1 and
        .spec.template.spec.containers[0].image == $image and
        any(.spec.template.spec.containers[0].volumeMounts[]?;
          .name == $volume and .mountPath == $mount) and
        any(.spec.template.spec.volumes[]?;
          .name == $volume and .hostPath.path == $path and .hostPath.type == "DirectoryOrCreate") and
        .status.availableReplicas == 1 and
        .status.readyReplicas == 1
      ' >/dev/null || fail "LAB foundation Deployment drifted or is unavailable: $deployment"
    kubectl -n "$NAMESPACE" get service "$service" -o json | jq -e \
      --arg app "$deployment" --argjson port "$port" '
        .spec.selector == {"app": $app} and
        any(.spec.ports[]?; .port == $port)
      ' >/dev/null || fail "LAB foundation Service selector drifted: $service"
  done <<'EOF'
lab-config-mysql|mysql:8.0.36|mysql-data|/var/lib/mysql|/var/lib/provi/lab-config-server/mysql|lab-config-mysql|3306
lab-config-redis|redis:7.4-alpine|redis-data|/data|/var/lib/provi/lab-config-server/redis|lab-config-redis|6379
EOF
  ! kubectl -n "$NAMESPACE" get deployment "${RELEASE}-controller" >/dev/null 2>&1 \
    || fail 'LAB controller Deployment exists before deployment'
  [[ -z $(kubectl -n "$NAMESPACE" get cronjob -l "app.kubernetes.io/instance=$RELEASE" -o name) ]] \
    || fail 'LAB Config Server CronJob exists before deployment'
}

validate_lab_read_only_contract() {
  jq -e '.api.readOnly == true' "$WORK_DIR/current-values.json" >/dev/null \
    || fail 'LAB Config Server is not in the required read-only API-bootstrap mode'
}

snapshot_protected_pods() {
  local output=$1
  kubectl -n "$NAMESPACE" get pods -o json | jq -r --arg release "$RELEASE" '
    .items[]
    | select((.metadata.labels["app.kubernetes.io/instance"] // "") != $release)
    | select(((.metadata.ownerReferences // []) | any(.kind == "Job")) | not)
    | [
        .metadata.name,
        .metadata.uid,
        (.spec.nodeName // ""),
        (.status.phase // ""),
        ([.status.containerStatuses[]?.restartCount] | add // 0)
      ] | @tsv
  ' | LC_ALL=C sort >"$output"
}

snapshot_managed_workloads() {
  local pods_output=$1
  local services_output=$2
  kubectl get pods --all-namespaces -o json | jq -r '
    .items[]
    | select(
        (.metadata.labels.username // "") != ""
        or (.spec.priorityClassName // "") == "ailab-user-workload"
        or (.metadata.labels.app // "") == "ailab-guest"
      )
    | [
        .metadata.namespace,
        .metadata.name,
        .metadata.uid,
        (.metadata.labels.username // ""),
        (.spec.nodeName // ""),
        (.status.phase // ""),
        ([.status.containerStatuses[]?.restartCount] | add // 0)
      ] | @tsv
  ' | LC_ALL=C sort >"$pods_output"
  kubectl get services --all-namespaces -o json | jq -r '
    .items[]
    | select((.metadata.labels.username // "") != "")
    | [
        .metadata.namespace,
        .metadata.name,
        .metadata.uid,
        (.metadata.labels.username // ""),
        (.spec.type // ""),
        (.spec.clusterIP // ""),
        ((.spec.selector // {}) | to_entries | sort_by(.key) | from_entries | tojson)
      ] | @tsv
  ' | LC_ALL=C sort >"$services_output"
}

validate_management_placement() {
  kubectl get nodes -o json | jq -e \
    --arg role "$EXPECTED_NODE_ROLE" --arg host "$EXPECTED_HOST" '
      [.items[] | select(.metadata.labels["provi.dgu.ac.kr/role"] == $role)] as $nodes
      | ($nodes | length) == 1
        and $nodes[0].metadata.name == $host
        and any($nodes[0].status.conditions[]?; .type == "Ready" and .status == "True")
    ' >/dev/null || fail "the $EXPECTED_NODE_ROLE label is not unique on $EXPECTED_HOST"

  kubectl -n "$NAMESPACE" get pods \
    -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=api" -o json \
    | jq -e --arg host "$EXPECTED_HOST" '
        [.items[] | select(.metadata.deletionTimestamp == null)] as $pods
        | ($pods | length) == 1
          and $pods[0].spec.nodeName == $host
          and $pods[0].status.phase == "Running"
          and any($pods[0].status.conditions[]?; .type == "Ready" and .status == "True")
      ' >/dev/null || fail "the Config Server API Pod is not uniquely Ready on $EXPECTED_HOST"
}

unfinished_operation_count() {
  local query output
  query="SELECT COUNT(*) FROM operation_log s WHERE s.job_id=s.id AND s.phase='START' AND NOT EXISTS (SELECT 1 FROM operation_log e WHERE e.request_id=s.request_id AND e.action=s.action AND e.id>s.id AND e.phase IN ('SUCCESS','FAIL','UNKNOWN'));"
  if [[ "$TARGET" == farm ]]; then
    output=$(printf '%s\n' "$query" | kubectl -n "$NAMESPACE" exec -i mysql-0 -- \
      sh -ceu 'exec mysql --protocol=socket -uroot -p"$MYSQL_ROOT_PASSWORD" --batch --skip-column-names operation_state_db') \
      || fail 'could not query the FARM operation ledger'
  else
    output=$(printf '%s\n' "$query" | kubectl -n "$NAMESPACE" exec -i deployment/lab-config-mysql -- \
      sh -ceu 'exec mysql --protocol=socket -uroot -p"$MYSQL_ROOT_PASSWORD" --batch --skip-column-names operation_state_db') \
      || fail 'could not query the LAB operation ledger'
  fi
  output=$(tr -d '[:space:]' <<<"$output")
  [[ "$output" =~ ^[0-9]+$ ]] || fail 'operation ledger returned a non-numeric result'
  printf '%s\n' "$output"
}

require_zero_unfinished_operations() {
  [[ $(unfinished_operation_count) == 0 ]] \
    || fail 'an unfinished Config Server operation exists; refusing deployment'
}

require_no_active_jobs() {
  kubectl -n "$NAMESPACE" get jobs -o json | jq -e '
    [.items[] | select((.status.active // 0) > 0)] | length == 0
  ' >/dev/null || fail "an active Job exists in $NAMESPACE; refusing deployment"
}

set_farm_cronjobs_suspend() {
  local value=$1
  local cronjob
  for cronjob in "$FARM_GPU_CRONJOB" "$FARM_KRB5_CRONJOB"; do
    kubectl -n "$NAMESPACE" patch cronjob "$cronjob" --type=merge \
      -p "{\"spec\":{\"suspend\":$value}}" >/dev/null
  done
}

validate_farm_cronjobs() {
  local expected_suspend=$1
  local require_idle=${2:-true}
  kubectl -n "$NAMESPACE" get cronjob "$FARM_GPU_CRONJOB" "$FARM_KRB5_CRONJOB" -o json \
    | jq -e --argjson expected "$expected_suspend" --argjson require_idle "$require_idle" '
        (.items | length) == 2
          and all(.items[]; (.spec.suspend // false) == $expected)
          and (($require_idle == false) or all(.items[]; ((.status.active // []) | length) == 0))
      ' >/dev/null || fail 'FARM Config Server CronJobs are not in the required state'
}

wait_for_farm_cronjobs_idle() {
  local attempt active
  for ((attempt=0; attempt<450; attempt++)); do
    active=$(kubectl -n "$NAMESPACE" get cronjob "$FARM_GPU_CRONJOB" "$FARM_KRB5_CRONJOB" \
      -o json | jq '[.items[].status.active[]?] | length')
    [[ "$active" == 0 ]] && return 0
    sleep 2
  done
  fail 'timed out waiting for FARM Config Server CronJobs to become idle'
}

wait_for_no_release_pods() {
  local component=$1
  local attempt count
  for ((attempt=0; attempt<720; attempt++)); do
    count=$(kubectl -n "$NAMESPACE" get pods \
      -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=$component" \
      -o json | jq '.items | length')
    [[ "$count" == 0 ]] && return 0
    if (( attempt == 0 || attempt % 30 == 0 )); then
      printf '[WAIT] %s Pod drain: elapsed=%ss remaining=%s\n' "$component" "$attempt" "$count"
    fi
    sleep 1
  done
  fail "timed out waiting for $component Pods to terminate"
}

validate_common_values
if [[ "$TARGET" == farm ]]; then
  validate_farm_values
  validate_farm_transition_values false 1 false
else
  validate_lab_values
fi

current_tag=$(jq -r '.image.tag' "$WORK_DIR/current-values.json")
current_digest=$(jq -r '.image.digest // ""' "$WORK_DIR/current-values.json")
if [[ -n "$current_digest" ]]; then
  current_image="$EXPECTED_IMAGE_REPOSITORY@$current_digest"
else
  current_image="$EXPECTED_IMAGE_REPOSITORY:$current_tag"
fi
[[ $(kubectl -n "$NAMESPACE" get deployment "$RELEASE" -o jsonpath='{.spec.template.spec.containers[0].image}') == "$current_image" ]] \
  || fail 'live API Deployment image differs from the Helm values'
kubectl -n "$NAMESPACE" get deployment "$RELEASE" -o json | jq -e '
  .spec.replicas == 1 and .status.availableReplicas == 1 and .status.readyReplicas == 1
' >/dev/null || fail 'Config Server API Deployment is not a single available replica'
if [[ "$TARGET" == farm ]]; then
  kubectl -n "$NAMESPACE" get deployment "${RELEASE}-controller" -o json | jq -e '
    .spec.replicas == 1 and .status.availableReplicas == 1 and .status.readyReplicas == 1
  ' >/dev/null || fail 'FARM Config Server controller is not a single available replica'
fi

validate_management_placement
snapshot_protected_pods "$WORK_DIR/protected-pods-before.tsv"
snapshot_managed_workloads \
  "$WORK_DIR/managed-pods-before.tsv" "$WORK_DIR/managed-services-before.tsv"

lab_bootstrap_args=()
if [[ "$TARGET" == lab ]]; then
  # Existing Phase 7-A releases predate this value. Force the structural gate in
  # the candidate and upgrade; only the post-upgrade release must already report it.
  lab_bootstrap_args=(--set api.readOnly=true)
fi
render_args=(template "$RELEASE" "$CHART" --namespace "$NAMESPACE" --values "$WORK_DIR/current-values.yaml" "${lab_bootstrap_args[@]}")
if [[ "$ACTION" == upgrade ]]; then
  render_args+=(--set-string "image.tag=$IMAGE_TAG" --set-string "image.digest=$IMAGE_DIGEST")
fi
helm lint "$CHART" --values "$WORK_DIR/current-values.yaml" "${lab_bootstrap_args[@]}" >/dev/null
helm "${render_args[@]}" >"$WORK_DIR/rendered.yaml"
kubectl create --dry-run=client --filename "$WORK_DIR/rendered.yaml" --output name >"$WORK_DIR/rendered-resources.txt"

[[ $(grep -c "^deployment.apps/$RELEASE$" "$WORK_DIR/rendered-resources.txt") == 1 ]] \
  || fail 'rendered API Deployment is missing or duplicated'
[[ $(grep -c "^service/$EXPECTED_SERVICE$" "$WORK_DIR/rendered-resources.txt") == 1 ]] \
  || fail 'rendered Config Server Service is missing or duplicated'
if [[ "$TARGET" == farm ]]; then
  [[ $(grep -c "^deployment.apps/${RELEASE}-controller$" "$WORK_DIR/rendered-resources.txt") == 1 ]] \
    || fail 'rendered FARM controller Deployment is missing or duplicated'
  [[ $(grep -c '^cronjob.batch/' "$WORK_DIR/rendered-resources.txt") == 2 ]] \
    || fail 'rendered FARM CronJob count differs from two'
else
  ! grep -q "^deployment.apps/${RELEASE}-controller$" "$WORK_DIR/rendered-resources.txt" \
    || fail 'rendered LAB controller is unexpectedly enabled'
  ! grep -q '^cronjob.batch/' "$WORK_DIR/rendered-resources.txt" \
    || fail 'rendered LAB CronJob is unexpectedly enabled'
  grep -q 'claimName: lab-config-ledger' "$WORK_DIR/rendered.yaml" \
    || fail 'rendered LAB ledger PVC is absent'
  ! grep -Eq '^[[:space:]]+nfs:' "$WORK_DIR/rendered.yaml" || fail 'rendered LAB chart unexpectedly contains an NFS volume'
  ! grep -q 'FARM_NODES_JSON' "$WORK_DIR/rendered.yaml" || fail 'rendered LAB chart contains the legacy FARM node variable'
  ! grep -q '192\.168\.2\.' "$WORK_DIR/rendered.yaml" || fail 'rendered LAB chart contains a FARM network address'
  grep -A1 'name: CONFIG_API_READ_ONLY' "$WORK_DIR/rendered.yaml" | grep -q 'value: "true"' \
    || fail 'rendered LAB API does not enable the structural read-only gate'
  ! grep -q 'resources: \["pods", "pods/exec"' "$WORK_DIR/rendered.yaml" \
    || fail 'rendered LAB Role unexpectedly grants pods/exec'
  ! grep -q 'verbs: \["get", "list", "watch", "create"' "$WORK_DIR/rendered.yaml" \
    || fail 'rendered LAB Role unexpectedly grants Kubernetes mutation verbs'
fi

health=$(kubectl get --raw "/api/v1/namespaces/$NAMESPACE/services/http:$EXPECTED_SERVICE:80/proxy/health")
expected_run_mode=$(jq -r '.verifyMode' "$WORK_DIR/current-values.json")
jq -e --arg cluster "${EXPECTED_CLUSTER_ID^^}" --arg run_mode "$expected_run_mode" \
  '.status == "OK" and .cluster_id == $cluster and .run_mode == $run_mode' <<<"$health" >/dev/null \
  || fail 'Config Server health identity is invalid'

if [[ "$TARGET" == lab && "$ACTION" == probe ]]; then
  # A probe must never certify the historical Phase 7-A image which predates the
  # structural gate. Only upgrade is allowed to bridge that release once.
  validate_lab_read_only_contract
  jq -e '.read_only == true' <<<"$health" >/dev/null \
    || fail 'LAB probe refuses a release whose health does not confirm read-only mode'
fi

if [[ "$ACTION" == probe ]]; then
  validate_management_placement
  snapshot_protected_pods "$WORK_DIR/protected-pods-after.tsv"
  snapshot_managed_workloads \
    "$WORK_DIR/managed-pods-after.tsv" "$WORK_DIR/managed-services-after.tsv"
  cmp -s "$WORK_DIR/protected-pods-before.tsv" "$WORK_DIR/protected-pods-after.tsv" \
    || fail 'a protected non-Config Pod changed during the read-only probe'
  cmp -s "$WORK_DIR/managed-pods-before.tsv" "$WORK_DIR/managed-pods-after.tsv" \
    || fail 'a managed user Pod changed during the read-only probe'
  cmp -s "$WORK_DIR/managed-services-before.tsv" "$WORK_DIR/managed-services-after.tsv" \
    || fail 'a username-labelled Service changed during the read-only probe'
  printf '[SUCCESS] %s Config Server probe passed; source=%s chart_sha256=%s Kubernetes mutation performed: no\n' \
    "${TARGET^^}" "$SOURCE_SHA" "$CHART_SHA256"
  exit 0
fi

require_zero_unfinished_operations
require_no_active_jobs
if [[ "$TARGET" == farm ]]; then
  validate_farm_cronjobs false
fi

printf '[CHANGE] Entering the controlled Config Server maintenance window.\n'
MAINTENANCE_STARTED=yes
if [[ "$TARGET" == farm ]]; then
  set_farm_cronjobs_suspend true
  wait_for_farm_cronjobs_idle
  validate_farm_cronjobs true
fi
kubectl -n "$NAMESPACE" scale "deployment/$RELEASE" --replicas=0
if [[ "$TARGET" == farm ]]; then
  kubectl -n "$NAMESPACE" scale "deployment/${RELEASE}-controller" --replicas=0
fi
wait_for_no_release_pods api
if [[ "$TARGET" == farm ]]; then
  wait_for_no_release_pods controller
fi
require_zero_unfinished_operations
require_no_active_jobs

printf '[CHANGE] Applying the exact chart and digest-pinned image to %s/%s\n' "$NAMESPACE" "$RELEASE"
# Set before dispatch: cleanup must roll back even when Helm itself errors, times
# out, or receives INT/TERM before returning to this script.
CHANGE_STARTED=yes
if [[ "$TARGET" == farm ]]; then
  # CronJob suspend is part of this Helm revision.  A plain temporary patch can
  # be cleared by Helm while the upgrade is still waiting, which would allow a
  # scheduled writer to race with the validation window.
  helm upgrade "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" \
    --reset-values \
    --values "$WORK_DIR/current-values.yaml" \
    --set-string "image.tag=$IMAGE_TAG" \
    --set-string "image.digest=$IMAGE_DIGEST" \
    --set "api.maintenance=true" \
    --set "controller.replicas=0" \
    --set "cronjobs.gpuCheck.suspend=true" \
    --set "cronjobs.krb5Reconcile.suspend=true" \
    --wait \
    --timeout 12m \
    --rollback-on-failure
else
  helm upgrade "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" \
    --reset-values \
    --values "$WORK_DIR/current-values.yaml" \
    --set-string "image.tag=$IMAGE_TAG" \
    --set-string "image.digest=$IMAGE_DIGEST" \
    "${lab_bootstrap_args[@]}" \
    --wait \
    --timeout 12m \
    --rollback-on-failure
fi

if [[ "$TARGET" == farm ]]; then
  # The first FARM revision owns suspend=true for the whole validation window.
  wait_for_farm_cronjobs_idle
  validate_farm_cronjobs true
  validate_farm_controller_replicas 0
fi
require_no_active_jobs

kubectl -n "$NAMESPACE" rollout status "deployment/$RELEASE" --timeout=5m
[[ $(kubectl -n "$NAMESPACE" get deployment "$RELEASE" -o jsonpath='{.spec.template.spec.containers[0].image}') == "$EXPECTED_IMAGE_REPOSITORY@$IMAGE_DIGEST" ]] \
  || fail 'Config Server API Deployment did not adopt the requested registry digest'
helm get values "$RELEASE" --namespace "$NAMESPACE" --all --output json >"$WORK_DIR/current-values.json"
chmod 600 "$WORK_DIR/current-values.json"
validate_common_values
[[ $(jq -r '.image.tag' "$WORK_DIR/current-values.json") == "$IMAGE_TAG" ]] \
  || fail 'Helm release values did not retain the requested image tag'
[[ $(jq -r '.image.digest' "$WORK_DIR/current-values.json") == "$IMAGE_DIGEST" ]] \
  || fail 'Helm release values did not retain the requested registry digest'
if [[ "$TARGET" == farm ]]; then
  validate_farm_values
  validate_farm_transition_values true 0 true
  validate_farm_controller_replicas 0
else
  validate_lab_values
  validate_lab_read_only_contract
fi

validate_management_placement
health=$(kubectl get --raw "/api/v1/namespaces/$NAMESPACE/services/http:$EXPECTED_SERVICE:80/proxy/health")
expected_run_mode=$(jq -r '.verifyMode' "$WORK_DIR/current-values.json")
jq -e --arg cluster "${EXPECTED_CLUSTER_ID^^}" --arg run_mode "$expected_run_mode" \
  '.status == "OK" and .cluster_id == $cluster and .run_mode == $run_mode' <<<"$health" >/dev/null \
  || fail 'Config Server health failed after upgrade'
if [[ "$TARGET" == lab ]]; then
  jq -e '.read_only == true' <<<"$health" >/dev/null \
    || fail 'LAB Config Server health does not confirm read-only API-bootstrap mode'
else
  jq -e '.maintenance == true and .read_only == false' <<<"$health" >/dev/null \
    || fail 'FARM Config Server did not keep write admission closed during validation'
fi

require_zero_unfinished_operations
require_no_active_jobs
snapshot_protected_pods "$WORK_DIR/protected-pods-after.tsv"
snapshot_managed_workloads \
  "$WORK_DIR/managed-pods-after.tsv" "$WORK_DIR/managed-services-after.tsv"
cmp -s "$WORK_DIR/protected-pods-before.tsv" "$WORK_DIR/protected-pods-after.tsv" \
  || fail 'a protected non-Config Pod changed during the upgrade'
cmp -s "$WORK_DIR/managed-pods-before.tsv" "$WORK_DIR/managed-pods-after.tsv" \
  || fail 'a managed user Pod changed during the upgrade'
cmp -s "$WORK_DIR/managed-services-before.tsv" "$WORK_DIR/managed-services-after.tsv" \
  || fail 'a username-labelled Service changed during the upgrade'

if [[ "$TARGET" == farm ]]; then
  printf '[CHANGE] Finalizing FARM with a Helm-owned CronJob resume revision.\n'
  helm upgrade "$RELEASE" "$CHART" \
    --namespace "$NAMESPACE" \
    --reset-values \
    --values "$WORK_DIR/current-values.yaml" \
    --set-string "image.tag=$IMAGE_TAG" \
    --set-string "image.digest=$IMAGE_DIGEST" \
    --set "api.maintenance=false" \
    --set "controller.replicas=1" \
    --set "cronjobs.gpuCheck.suspend=false" \
    --set "cronjobs.krb5Reconcile.suspend=false" \
    --wait \
    --timeout 12m \
    --rollback-on-failure

  # This is the commit point: the final Helm revision has successfully restored
  # write admission, the controller and the CronJobs together. A new user operation may
  # be accepted immediately, so a later observational failure must not roll the
  # release back underneath it or restart its controller.
  POSTCHECK_COMPLETE=yes
  MAINTENANCE_STARTED=no

  # A missed schedule may start immediately after resume.  From here on, do not
  # use operation-idle, active-Job, or user-workload snapshots as failure gates.
  # Validate only the final desired values, placement, and serving health.
  helm get values "$RELEASE" --namespace "$NAMESPACE" --all --output json \
    >"$WORK_DIR/current-values.json"
  chmod 600 "$WORK_DIR/current-values.json"
  validate_common_values
  validate_farm_values
  validate_farm_transition_values false 1 false
  [[ $(jq -r '.image.tag' "$WORK_DIR/current-values.json") == "$IMAGE_TAG" ]] \
    || fail 'final FARM values did not retain the requested image tag'
  [[ $(jq -r '.image.digest' "$WORK_DIR/current-values.json") == "$IMAGE_DIGEST" ]] \
    || fail 'final FARM values did not retain the requested registry digest'
  validate_farm_cronjobs false false
  validate_management_placement
  validate_farm_controller_replicas 1
  kubectl -n "$NAMESPACE" rollout status "deployment/${RELEASE}-controller" --timeout=5m
  health=$(kubectl get --raw "/api/v1/namespaces/$NAMESPACE/services/http:$EXPECTED_SERVICE:80/proxy/health")
  expected_run_mode=$(jq -r '.verifyMode' "$WORK_DIR/current-values.json")
  jq -e --arg cluster "${EXPECTED_CLUSTER_ID^^}" --arg run_mode "$expected_run_mode" \
    '.status == "OK" and .cluster_id == $cluster and .run_mode == $run_mode
      and .maintenance == false and .read_only == false' <<<"$health" >/dev/null \
    || fail 'Config Server health failed after the FARM CronJob resume revision'
fi

if [[ "$TARGET" == lab ]]; then
  POSTCHECK_COMPLETE=yes
  MAINTENANCE_STARTED=no
fi
printf '[SUCCESS] %s Config Server existing release upgraded to %s@%s; source=%s chart_sha256=%s\n' \
  "${TARGET^^}" "$EXPECTED_IMAGE_REPOSITORY" "$IMAGE_DIGEST" "$SOURCE_SHA" "$CHART_SHA256"
