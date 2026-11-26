{{- define "containerssh-config-server.fullname" -}}
{{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "containerssh-config-server.clusterId" -}}
{{- $id := required "cluster.id를 지정해야 합니다(예: farm 또는 lab)" .Values.cluster.id -}}
{{- if not (regexMatch "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$" $id) -}}
{{- fail "cluster.id는 소문자 영숫자와 하이픈만 사용할 수 있습니다" -}}
{{- end -}}
{{- if not (has $id (list "farm" "lab")) -}}
{{- fail "cluster.id는 farm 또는 lab이어야 합니다" -}}
{{- end -}}
{{- if eq $id "lab" -}}
{{- include "containerssh-config-server.labLifecycleContract" . -}}
{{- end -}}
{{- $id -}}
{{- end -}}

{{/*
Phase 7-B 전 LAB은 저장소·Kerberos·NFS·원격 작업을 일부만 연 혼합 상태로
렌더링되어서는 안 된다. latch가 false인 동안 아래의 완전한 API-only 계약을
차트 자체가 강제한다. FARM은 이 helper를 호출하지 않으므로 기존 동작을 유지한다.
*/}}
{{- define "containerssh-config-server.labLifecycleContract" -}}
{{- if not (kindIs "bool" .Values.lab.lifecycleDependenciesReady) -}}
{{- fail "lab.lifecycleDependenciesReady는 boolean이어야 합니다" -}}
{{- end -}}
{{- if not .Values.lab.lifecycleDependenciesReady -}}
{{- if ne .Values.api.readOnly true -}}
{{- fail "Phase 7-B 전 LAB은 api.readOnly=true여야 합니다" -}}
{{- end -}}
{{- if ne .Values.controller.enabled false -}}
{{- fail "Phase 7-B 전 LAB은 controller.enabled=false여야 합니다" -}}
{{- end -}}
{{- if or (ne .Values.cronjobs.gpuCheck.enabled false) (ne .Values.cronjobs.krb5Reconcile.enabled false) -}}
{{- fail "Phase 7-B 전 LAB은 Config Server CronJob을 모두 비활성화해야 합니다" -}}
{{- end -}}
{{- if or (ne .Values.nfs.server "") (ne .Values.nfs.userSharePath "") (ne .Values.nfs.kubeSharePath "") -}}
{{- fail "Phase 7-B 전 LAB은 nfs.server/userSharePath/kubeSharePath를 비워야 합니다" -}}
{{- end -}}
{{- if not (kindIs "slice" .Values.krb5.kdcHosts) -}}
{{- fail "Phase 7-B 전 LAB의 krb5.kdcHosts는 빈 list여야 합니다" -}}
{{- end -}}
{{- if or (ne .Values.krb5.realm "") (ne (len .Values.krb5.kdcHosts) 0) (ne .Values.krb5.adminServer "") (ne .Values.krb5.domain "") -}}
{{- fail "Phase 7-B 전 LAB은 Kerberos client 설정을 비워야 합니다" -}}
{{- end -}}
{{- if or (ne .Values.nas.ssh.host "") (ne (toString .Values.nas.ssh.port) "") (ne .Values.nas.ssh.user "disabled-until-phase7b") (ne .Values.nas.ssh.keyPath "") -}}
{{- fail "Phase 7-B 전 LAB NAS SSH는 host/port/keyPath를 비우고 user=disabled-until-phase7b로 잠가야 합니다" -}}
{{- end -}}
{{- if ne .Values.cluster.homeMountRootTemplate "" -}}
{{- fail "Phase 7-B 전 LAB은 cluster.homeMountRootTemplate을 비워야 합니다" -}}
{{- end -}}
{{- if or (ne .Values.cluster.ssh.user "disabled-until-phase7b") (ne .Values.cluster.adSsh.user "disabled-until-phase7b") -}}
{{- fail "Phase 7-B 전 LAB SSH/AD SSH user는 disabled-until-phase7b로 잠가야 합니다" -}}
{{- end -}}
{{- if or (ne .Values.cluster.ssh.keyPath "") (ne .Values.cluster.adSsh.keyPath "") -}}
{{- fail "Phase 7-B 전 LAB은 cluster.ssh/adSsh.keyPath를 비워야 합니다" -}}
{{- end -}}
{{- if not (kindIs "slice" .Values.cluster.adSsh.nodes) -}}
{{- fail "Phase 7-B 전 LAB의 cluster.adSsh.nodes는 빈 list여야 합니다" -}}
{{- end -}}
{{- if ne (len .Values.cluster.adSsh.nodes) 0 -}}
{{- fail "Phase 7-B 전 LAB은 cluster.adSsh.nodes를 비워야 합니다" -}}
{{- end -}}
{{- if ne .Values.infra.adminBeInternalUrl "http://127.0.0.1:9" -}}
{{- fail "Phase 7-B 전 LAB Admin BE callback은 http://127.0.0.1:9 loopback sink여야 합니다" -}}
{{- end -}}
{{- if ne .Values.monitoring.prometheusUrl "http://127.0.0.1:9" -}}
{{- fail "Phase 7-B 전 LAB Prometheus endpoint는 http://127.0.0.1:9 loopback sink여야 합니다" -}}
{{- end -}}
{{- if or (not (kindIs "string" .Values.kubeShare.claimName)) (eq (trim .Values.kubeShare.claimName) "") -}}
{{- fail "Phase 7-B 전 LAB은 kubeShare.claimName에 독립 원장 PVC를 지정해야 합니다" -}}
{{- end -}}
{{- if or (not (kindIs "string" .Values.imageStore.claimName)) (eq (trim .Values.imageStore.claimName) "") -}}
{{- fail "Phase 7-B 전 LAB은 imageStore.claimName에 독립 image-store PVC를 지정해야 합니다" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "containerssh-config-server.selectorMode" -}}
{{- $mode := default "legacy" .Values.compatibility.deploymentSelectorMode -}}
{{- if not (has $mode (list "legacy" "release")) -}}
{{- fail "compatibility.deploymentSelectorMode는 legacy 또는 release여야 합니다" -}}
{{- end -}}
{{- if and (eq (include "containerssh-config-server.clusterId" .) "lab") (eq $mode "legacy") -}}
{{- fail "LAB은 FARM 전용 legacy selector를 사용할 수 없습니다. compatibility.deploymentSelectorMode=release를 지정하세요" -}}
{{- end -}}
{{- $mode -}}
{{- end -}}

{{- define "containerssh-config-server.isLegacySelector" -}}
{{- eq (include "containerssh-config-server.selectorMode" .) "legacy" -}}
{{- end -}}

{{- define "containerssh-config-server.labels" -}}
app.kubernetes.io/name: containerssh-config-server
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
provi.dgu.ac.kr/cluster-id: {{ include "containerssh-config-server.clusterId" . | quote }}
{{- end -}}

{{- define "containerssh-config-server.selectorLabels" -}}
app.kubernetes.io/name: containerssh-config-server
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}

{{- define "containerssh-config-server.serviceAccountName" -}}
{{- if eq (include "containerssh-config-server.selectorMode" .) "legacy" -}}
config-server
{{- else -}}
{{- include "containerssh-config-server.fullname" . -}}
{{- end -}}
{{- end -}}

{{- define "containerssh-config-server.serviceName" -}}
{{- if eq (include "containerssh-config-server.selectorMode" .) "legacy" -}}
containerssh-config-service
{{- else -}}
{{- include "containerssh-config-server.fullname" . -}}
{{- end -}}
{{- end -}}

{{- define "containerssh-config-server.krb5ConfigMapName" -}}
{{- if eq (include "containerssh-config-server.selectorMode" .) "legacy" -}}
krb5-conf
{{- else -}}
{{- printf "%s-krb5" (include "containerssh-config-server.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "containerssh-config-server.gpuStateConfigMapName" -}}
{{- if eq (include "containerssh-config-server.selectorMode" .) "legacy" -}}
gpu-check-state
{{- else -}}
{{- printf "%s-gpu-check-state" (include "containerssh-config-server.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "containerssh-config-server.dbSecretName" -}}
{{- $defaultName := ternary "config-server-db-secret" (printf "%s-db" (include "containerssh-config-server.fullname" .)) (eq (include "containerssh-config-server.selectorMode" .) "legacy") -}}
{{- default $defaultName .Values.db.secretName | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "containerssh-config-server.logDbSecretName" -}}
{{- $defaultName := ternary "log-mysql-secret" (printf "%s-log-db" (include "containerssh-config-server.fullname" .)) (eq (include "containerssh-config-server.selectorMode" .) "legacy") -}}
{{- default $defaultName .Values.logDb.secretName | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "containerssh-config-server.nasSshSecretName" -}}
{{- $defaultName := ternary "nas-ssh-key" (printf "%s-nas-ssh" (include "containerssh-config-server.fullname" .)) (eq (include "containerssh-config-server.selectorMode" .) "legacy") -}}
{{- default $defaultName .Values.nas.ssh.secretName | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "containerssh-config-server.infraSshSecretName" -}}
{{- $defaultName := ternary "farm-ssh-key" (printf "%s-infra-ssh" (include "containerssh-config-server.fullname" .)) (eq (include "containerssh-config-server.selectorMode" .) "legacy") -}}
{{- default $defaultName .Values.cluster.ssh.secretName | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "containerssh-config-server.infraAdSshSecretName" -}}
{{- $defaultName := ternary "farm-ad-ssh-key" (printf "%s-infra-ad-ssh" (include "containerssh-config-server.fullname" .)) (eq (include "containerssh-config-server.selectorMode" .) "legacy") -}}
{{- default $defaultName .Values.cluster.adSsh.secretName | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "containerssh-config-server.apiTokenSecretName" -}}
{{- $defaultName := ternary "config-server-api-token" (printf "%s-api-token" (include "containerssh-config-server.fullname" .)) (eq (include "containerssh-config-server.selectorMode" .) "legacy") -}}
{{- default $defaultName .Values.apiToken.secretName | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "containerssh-config-server.knownHostsSecretName" -}}
{{- $defaultName := ternary "config-server-ssh-known-hosts" (printf "%s-ssh-known-hosts" (include "containerssh-config-server.fullname" .)) (eq (include "containerssh-config-server.selectorMode" .) "legacy") -}}
{{- default $defaultName .Values.sshKnownHosts.secretName | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "containerssh-config-server.slackSecretName" -}}
{{- $defaultName := ternary "config-server-infra-slack-secret" (printf "%s-infra-slack" (include "containerssh-config-server.fullname" .)) (eq (include "containerssh-config-server.selectorMode" .) "legacy") -}}
{{- default $defaultName .Values.infra.slackSecretName | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "containerssh-config-server.infraSshUser" -}}
{{- $values := .Values | toJson | fromJson -}}
{{- $legacy := "" -}}
{{- if eq (include "containerssh-config-server.clusterId" .) "farm" -}}
{{- $legacy = dig "farm" "ssh" "user" "" $values -}}
{{- end -}}
{{- default $legacy .Values.cluster.ssh.user -}}
{{- end -}}
{{- define "containerssh-config-server.infraSshKeyPath" -}}
{{- $values := .Values | toJson | fromJson -}}
{{- $legacy := "" -}}
{{- if eq (include "containerssh-config-server.clusterId" .) "farm" -}}
{{- $legacy = dig "farm" "ssh" "keyPath" "" $values -}}
{{- end -}}
{{- default $legacy .Values.cluster.ssh.keyPath -}}
{{- end -}}
{{- define "containerssh-config-server.infraSshNodesJson" -}}
{{- $values := .Values | toJson | fromJson -}}
{{- $legacy := list -}}
{{- if eq (include "containerssh-config-server.clusterId" .) "farm" -}}
{{- $legacy = dig "farm" "ssh" "nodes" (list) $values -}}
{{- end -}}
{{- default $legacy .Values.cluster.ssh.nodes | toJson -}}
{{- end -}}
{{- define "containerssh-config-server.infraAdSshUser" -}}
{{- $values := .Values | toJson | fromJson -}}
{{- $legacy := "" -}}
{{- if eq (include "containerssh-config-server.clusterId" .) "farm" -}}
{{- $legacy = dig "farm" "adSsh" "user" "" $values -}}
{{- end -}}
{{- default $legacy .Values.cluster.adSsh.user -}}
{{- end -}}
{{- define "containerssh-config-server.infraAdSshKeyPath" -}}
{{- $values := .Values | toJson | fromJson -}}
{{- $legacy := "" -}}
{{- if eq (include "containerssh-config-server.clusterId" .) "farm" -}}
{{- $legacy = dig "farm" "adSsh" "keyPath" "" $values -}}
{{- end -}}
{{- default $legacy .Values.cluster.adSsh.keyPath -}}
{{- end -}}
{{- define "containerssh-config-server.infraAdSshNodesJson" -}}
{{- $values := .Values | toJson | fromJson -}}
{{- $legacy := list -}}
{{- if eq (include "containerssh-config-server.clusterId" .) "farm" -}}
{{- $legacy = dig "farm" "adSsh" "nodes" (list) $values -}}
{{- end -}}
{{- default $legacy .Values.cluster.adSsh.nodes | toJson -}}
{{- end -}}

{{- define "containerssh-config-server.env" -}}
- name: CLUSTER_ID
  value: {{ include "containerssh-config-server.clusterId" . | quote }}
- name: CONFIG_API_READ_ONLY
  value: {{ include "containerssh-config-server.apiReadOnly" . | quote }}
- name: CONFIG_API_MAINTENANCE
  value: {{ include "containerssh-config-server.apiMaintenance" . | quote }}
- name: KUBERNETES_CLIENT_MODE
  value: "in-cluster"
- name: KUBERNETES_CLIENT_VALIDATE_AT_STARTUP
  value: "true"
- name: HOME_MOUNT_ROOT_TEMPLATE
  value: {{ .Values.cluster.homeMountRootTemplate | quote }}
- name: NFS_SERVER
  value: {{ .Values.nfs.server | quote }}
- name: NFS_USER_SHARE_PATH
  value: {{ .Values.nfs.userSharePath | quote }}
- name: NAS_SSH_HOST
  value: {{ .Values.nas.ssh.host | quote }}
- name: NAS_SSH_PORT
  value: {{ .Values.nas.ssh.port | quote }}
- name: NAS_SSH_USER
  value: {{ .Values.nas.ssh.user | quote }}
- name: NAS_SSH_KEY_PATH
  value: {{ .Values.nas.ssh.keyPath | quote }}
- name: SUDO_ALLOWED_COMMANDS
  value: {{ .Values.security.sudoAllowedCommands | quote }}
- name: NAMESPACE
  value: {{ .Values.config.namespace | quote }}
- name: ADMIN_BE_INTERNAL_URL
  value: {{ .Values.infra.adminBeInternalUrl | quote }}
- name: PROMETHEUS_URL
  value: {{ .Values.monitoring.prometheusUrl | quote }}
- name: UID_MIN
  value: {{ .Values.accounts.uidMin | quote }}
- name: UID_MAX
  value: {{ .Values.accounts.uidMax | quote }}
- name: SHARED_GID_MIN
  value: {{ .Values.accounts.sharedGidMin | quote }}
- name: SHARED_GID_MAX
  value: {{ .Values.accounts.sharedGidMax | quote }}
- name: ACCOUNT_PREFIX
  value: {{ .Values.accounts.prefix | quote }}
- name: NODEPORT_MIN
  value: {{ .Values.nodeport.min | quote }}
- name: NODEPORT_MAX
  value: {{ .Values.nodeport.max | quote }}
- name: IMAGE_HELPER_IMAGE
  value: {{ include "containerssh-config-server.image" . | quote }}
- name: VERIFY_MODE
  value: {{ .Values.verifyMode | quote }}
{{- if .Values.faultInjection }}
- name: FAULT_INJECTION
  value: "1"
{{- end }}
- name: DB_HOST
  value: {{ .Values.db.host | quote }}
- name: DB_PORT
  value: {{ .Values.db.port | quote }}
- name: DB_NAME
  value: {{ .Values.db.name | quote }}
- name: DB_USER
  value: {{ .Values.db.user | quote }}
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "containerssh-config-server.dbSecretName" . | quote }}
      key: {{ .Values.db.passwordKey | quote }}
- name: LOG_DB_HOST
  value: {{ .Values.logDb.host | quote }}
- name: LOG_DB_PORT
  value: {{ .Values.logDb.port | quote }}
- name: LOG_DB_NAME
  value: {{ .Values.logDb.name | quote }}
- name: LOG_DB_USER
  value: {{ .Values.logDb.user | quote }}
- name: LOG_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "containerssh-config-server.logDbSecretName" . | quote }}
      key: {{ .Values.logDb.passwordKey | quote }}
- name: REDIS_HOST
  value: {{ .Values.redis.host | quote }}
- name: REDIS_PORT
  value: {{ .Values.redis.port | quote }}
- name: REDIS_DB
  value: {{ .Values.redis.db | quote }}
- name: KRB5_REALM
  value: {{ .Values.krb5.realm | quote }}
- name: INFRA_SSH_USER
  value: {{ include "containerssh-config-server.infraSshUser" . | quote }}
- name: INFRA_SSH_KEY_PATH
  value: {{ include "containerssh-config-server.infraSshKeyPath" . | quote }}
- name: INFRA_NODES_JSON
  value: {{ include "containerssh-config-server.infraSshNodesJson" . | quote }}
- name: INFRA_AD_SSH_USER
  value: {{ include "containerssh-config-server.infraAdSshUser" . | quote }}
- name: INFRA_AD_SSH_KEY_PATH
  value: {{ include "containerssh-config-server.infraAdSshKeyPath" . | quote }}
- name: INFRA_AD_DC_NODES_JSON
  value: {{ include "containerssh-config-server.infraAdSshNodesJson" . | quote }}
- name: GPU_CHECK_STATE_NAME
  value: {{ include "containerssh-config-server.gpuStateConfigMapName" . | quote }}
- name: CONFIG_API_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "containerssh-config-server.apiTokenSecretName" . | quote }}
      key: token
{{- end -}}

{{- define "containerssh-config-server.apiReadOnly" -}}
{{- $readOnly := .Values.api.readOnly -}}
{{- if and $readOnly (ne (include "containerssh-config-server.clusterId" .) "lab") -}}
{{- fail "api.readOnly=true는 LAB API-bootstrap 릴리스에서만 사용할 수 있습니다" -}}
{{- end -}}
{{- if and $readOnly .Values.controller.enabled -}}
{{- fail "api.readOnly=true일 때 controller.enabled는 false여야 합니다" -}}
{{- end -}}
{{- if and $readOnly (or .Values.cronjobs.gpuCheck.enabled .Values.cronjobs.krb5Reconcile.enabled) -}}
{{- fail "api.readOnly=true일 때 Config Server CronJob은 모두 비활성화해야 합니다" -}}
{{- end -}}
{{- ternary "true" "false" $readOnly -}}
{{- end -}}

{{- define "containerssh-config-server.apiMaintenance" -}}
{{- $maintenance := .Values.api.maintenance -}}
{{- if and $maintenance (ne (include "containerssh-config-server.clusterId" .) "farm") -}}
{{- fail "api.maintenance=true는 FARM controlled upgrade에서만 사용할 수 있습니다" -}}
{{- end -}}
{{- if and $maintenance .Values.api.readOnly -}}
{{- fail "api.maintenance와 api.readOnly는 동시에 true일 수 없습니다" -}}
{{- end -}}
{{- if and $maintenance (or (not .Values.controller.enabled) (ne (int .Values.controller.replicas) 0)) -}}
{{- fail "api.maintenance=true일 때 FARM controller는 enabled=true, replicas=0이어야 합니다" -}}
{{- end -}}
{{- if and $maintenance (or (not .Values.cronjobs.gpuCheck.suspend) (not .Values.cronjobs.krb5Reconcile.suspend)) -}}
{{- fail "api.maintenance=true일 때 FARM CronJob은 모두 suspend=true여야 합니다" -}}
{{- end -}}
{{- ternary "true" "false" $maintenance -}}
{{- end -}}

{{- define "containerssh-config-server.volumeMounts" -}}
- name: nas-ssh-key
  mountPath: /etc/nas-ssh
  readOnly: true
- name: image-store
  mountPath: /image-store
- name: kube-share
  mountPath: /kube_share
- name: krb5-conf
  mountPath: /etc/krb5.conf
  subPath: krb5.conf
  readOnly: true
- name: infra-ssh-key
  mountPath: /etc/infra-ssh
  readOnly: true
- name: infra-ssh-key
  mountPath: /etc/farm-ssh
  readOnly: true
- name: infra-ad-ssh-key
  mountPath: /etc/infra-ad-ssh
  readOnly: true
- name: infra-ad-ssh-key
  mountPath: /etc/farm-ad-ssh
  readOnly: true
- name: ssh-known-hosts
  mountPath: /etc/ssh-known-hosts
  readOnly: true
{{- end -}}

{{- define "containerssh-config-server.volumes" -}}
- name: nas-ssh-key
  secret:
    secretName: {{ include "containerssh-config-server.nasSshSecretName" . | quote }}
    defaultMode: 0400
- name: infra-ssh-key
  secret:
    secretName: {{ include "containerssh-config-server.infraSshSecretName" . | quote }}
    defaultMode: 0400
- name: infra-ad-ssh-key
  secret:
    secretName: {{ include "containerssh-config-server.infraAdSshSecretName" . | quote }}
    defaultMode: 0400
- name: ssh-known-hosts
  secret:
    secretName: {{ include "containerssh-config-server.knownHostsSecretName" . | quote }}
    optional: true
- name: image-store
  {{- if .Values.imageStore.claimName }}
  persistentVolumeClaim:
    claimName: {{ .Values.imageStore.claimName }}
  {{- else }}
  emptyDir: {}
  {{- end }}
- name: kube-share
  {{- if .Values.kubeShare.claimName }}
  persistentVolumeClaim:
    claimName: {{ .Values.kubeShare.claimName }}
  {{- else }}
  nfs:
    server: {{ .Values.nfs.server | quote }}
    path: {{ .Values.nfs.kubeSharePath | quote }}
  {{- end }}
- name: krb5-conf
  configMap:
    name: {{ include "containerssh-config-server.krb5ConfigMapName" . | quote }}
{{- end -}}

{{- define "containerssh-config-server.image" -}}
{{- $repository := required "image.repository를 지정해야 합니다" .Values.image.repository -}}
{{- if .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" .Values.image.digest) -}}
{{- fail "image.digest는 sha256:<64자리 소문자 16진수> 형식이어야 합니다" -}}
{{- end -}}
{{- printf "%s@%s" $repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repository (required "image.tag를 지정해야 합니다" .Values.image.tag) -}}
{{- end -}}
{{- end -}}
