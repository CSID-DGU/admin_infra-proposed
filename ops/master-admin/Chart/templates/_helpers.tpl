{{- define "provi-master-admin.name" -}}
master-admin
{{- end -}}

{{- define "provi-master-admin.namespace" -}}
provi-admin
{{- end -}}

{{- define "provi-master-admin.commonLabels" -}}
app.kubernetes.io/name: {{ include "provi-master-admin.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: provi-master
provi.dgu.ac.kr/plane: management
{{- end -}}

{{- define "provi-master-admin.selectorLabels" -}}
app.kubernetes.io/name: {{ include "provi-master-admin.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "provi-master-admin.componentLabels" -}}
{{ include "provi-master-admin.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "provi-master-admin.nodeSelector" -}}
provi.dgu.ac.kr/role: master-management
{{- end -}}

{{- define "provi-master-admin.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "provi-master-admin.containerSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
{{- end -}}

{{- define "provi-master-admin.requireSha" -}}
{{- $label := index . 0 -}}
{{- $value := index . 1 -}}
{{- if not (regexMatch "^[0-9a-f]{40}$" $value) -}}
{{- fail (printf "%s must be an exact lowercase 40-hex commit SHA" $label) -}}
{{- end -}}
{{- $value -}}
{{- end -}}

{{- define "provi-master-admin.requireDigest" -}}
{{- $label := index . 0 -}}
{{- $repository := index . 1 -}}
{{- $digest := index . 2 -}}
{{- if or (not (kindIs "string" $repository)) (eq (trim $repository) "") -}}
{{- fail (printf "%s.repository is required" $label) -}}
{{- end -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" $digest) -}}
{{- fail (printf "%s.digest must be an exact sha256 digest" $label) -}}
{{- end -}}
{{- printf "%s@%s" $repository $digest -}}
{{- end -}}

{{- define "provi-master-admin.backendImage" -}}
{{ include "provi-master-admin.requireDigest" (list "adminBackend.image" .Values.adminBackend.image.repository .Values.adminBackend.image.digest) }}
{{- end -}}

{{- define "provi-master-admin.frontendImage" -}}
{{ include "provi-master-admin.requireDigest" (list "adminFrontend.image" .Values.adminFrontend.image.repository .Values.adminFrontend.image.digest) }}
{{- end -}}

{{- define "provi-master-admin.mysqlImage" -}}
{{ include "provi-master-admin.requireDigest" (list "mysql.image" .Values.mysql.image.repository .Values.mysql.image.digest) }}
{{- end -}}

{{- define "provi-master-admin.redisImage" -}}
{{ include "provi-master-admin.requireDigest" (list "redis.image" .Values.redis.image.repository .Values.redis.image.digest) }}
{{- end -}}

{{- define "provi-master-admin.validate" -}}
{{- if ne .Release.Name "master-admin" -}}
{{- fail "release name must be master-admin" -}}
{{- end -}}
{{- if ne .Release.Namespace (include "provi-master-admin.namespace" .) -}}
{{- fail "release namespace must be provi-admin" -}}
{{- end -}}
{{- if not (has .Values.runtimeMode (list "SHADOW" "ACTIVE")) -}}
{{- fail "runtimeMode must be SHADOW or ACTIVE" -}}
{{- end -}}
{{- if not (has .Values.dataMode (list "RESTORE" "SHADOW" "ACTIVE")) -}}
{{- fail "dataMode must be RESTORE, SHADOW, or ACTIVE" -}}
{{- end -}}
{{- $infraSha := include "provi-master-admin.requireSha" (list "provenance.infraSha" .Values.provenance.infraSha) -}}
{{- $backendSha := include "provi-master-admin.requireSha" (list "provenance.backendSha" .Values.provenance.backendSha) -}}
{{- $frontendSha := include "provi-master-admin.requireSha" (list "provenance.frontendSha" .Values.provenance.frontendSha) -}}
{{- $backendImage := include "provi-master-admin.backendImage" . -}}
{{- $frontendImage := include "provi-master-admin.frontendImage" . -}}
{{- $mysqlImage := include "provi-master-admin.mysqlImage" . -}}
{{- $redisImage := include "provi-master-admin.redisImage" . -}}
{{- if or (not (kindIs "string" .Values.adminBackend.applicationSecretName)) (eq (trim .Values.adminBackend.applicationSecretName) "") -}}
{{- fail "adminBackend.applicationSecretName is required" -}}
{{- end -}}
{{- if .Values.adminBackend.farmReadOnlyCredential.enabled -}}
{{- if or (eq (trim .Values.adminBackend.farmReadOnlyCredential.secretName) "") (eq (trim .Values.adminBackend.farmReadOnlyCredential.key) "") -}}
{{- fail "enabled farmReadOnlyCredential requires secretName and key" -}}
{{- end -}}
{{- if ne .Values.adminBackend.farmReadOnlyCredential.mountPath "/etc/provi/kubeconfigs/farm-readonly.yaml" -}}
{{- fail "farmReadOnlyCredential.mountPath is fixed at /etc/provi/kubeconfigs/farm-readonly.yaml" -}}
{{- end -}}
{{- end -}}
{{- if or (not (kindIs "string" .Values.database.secretName)) (eq (trim .Values.database.secretName) "") -}}
{{- fail "database.secretName is required" -}}
{{- end -}}
{{- if ne .Values.database.name "web_admin" -}}
{{- fail "database.name is fixed at the Admin-only web_admin schema" -}}
{{- end -}}
{{- range $label, $key := .Values.database.keys -}}
{{- if or (not (kindIs "string" $key)) (eq (trim $key) "") -}}
{{- fail (printf "database.keys.%s is required" $label) -}}
{{- end -}}
{{- end -}}
{{- range $label, $replicas := dict "adminBackend.replicas" .Values.adminBackend.replicas "adminFrontend.replicas" .Values.adminFrontend.replicas -}}
{{- if or (lt (int $replicas) 0) (gt (int $replicas) 1) -}}
{{- fail (printf "%s must be 0 or 1" $label) -}}
{{- end -}}
{{- end -}}
{{- if ne .Values.networkPolicy.enabled true -}}
{{- fail "networkPolicy.enabled must remain true" -}}
{{- end -}}
{{- range $entry := .Values.networkPolicy.backendExtraEgress -}}
{{- if not (regexMatch "^(?:[0-9]{1,3}\\.){3}[0-9]{1,3}/32$" $entry.cidr) -}}
{{- fail "networkPolicy.backendExtraEgress requires exact IPv4 /32 destinations" -}}
{{- end -}}
{{- if eq (len $entry.ports) 0 -}}
{{- fail "networkPolicy.backendExtraEgress entries require at least one port" -}}
{{- end -}}
{{- range $port := $entry.ports -}}
{{- if or (lt (int $port) 1) (gt (int $port) 65535) -}}
{{- fail "networkPolicy.backendExtraEgress ports must be between 1 and 65535" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if .Values.networkPolicy.backendMonitoringEgress.enabled -}}
{{- if ne .Values.networkPolicy.backendMonitoringEgress.namespace "provi-monitoring" -}}
{{- fail "networkPolicy.backendMonitoringEgress.namespace is fixed at provi-monitoring" -}}
{{- end -}}
{{- if ne .Values.networkPolicy.backendMonitoringEgress.prometheusLabel "master-monitoring-kube-prometheus-prometheus" -}}
{{- fail "networkPolicy.backendMonitoringEgress.prometheusLabel is fixed at the dedicated Master Prometheus" -}}
{{- end -}}
{{- if ne (int .Values.networkPolicy.backendMonitoringEgress.port) 9090 -}}
{{- fail "networkPolicy.backendMonitoringEgress.port is fixed at 9090" -}}
{{- end -}}
{{- end -}}
{{- if eq .Values.dataMode "RESTORE" -}}
{{- if ne .Values.runtimeMode "SHADOW" -}}
{{- fail "RESTORE requires runtimeMode=SHADOW" -}}
{{- end -}}
{{- if .Values.ingress.enabled -}}
{{- fail "RESTORE requires ingress.enabled=false" -}}
{{- end -}}
{{- if or (ne (int .Values.adminBackend.replicas) 0) (ne (int .Values.adminFrontend.replicas) 0) -}}
{{- fail "RESTORE requires Admin Backend and Frontend replicas=0" -}}
{{- end -}}
{{- if or .Values.mysql.readOnly .Values.redis.readOnly -}}
{{- fail "RESTORE requires writable MySQL and Redis for offline data loading" -}}
{{- end -}}
{{- if .Values.activation.confirmSingleWriter -}}
{{- fail "RESTORE must not set activation.confirmSingleWriter" -}}
{{- end -}}
{{- else if eq .Values.dataMode "SHADOW" -}}
{{- if ne .Values.runtimeMode "SHADOW" -}}
{{- fail "SHADOW dataMode requires runtimeMode=SHADOW" -}}
{{- end -}}
{{- if .Values.ingress.enabled -}}
{{- fail "SHADOW requires ingress.enabled=false" -}}
{{- end -}}
{{- if ne .Values.mysql.readOnly true -}}
{{- fail "SHADOW requires mysql.readOnly=true" -}}
{{- end -}}
{{- if ne .Values.redis.readOnly true -}}
{{- fail "SHADOW requires redis.readOnly=true" -}}
{{- end -}}
{{- if .Values.activation.confirmSingleWriter -}}
{{- fail "SHADOW must not set activation.confirmSingleWriter" -}}
{{- end -}}
{{- else -}}
{{- if ne .Values.runtimeMode "ACTIVE" -}}
{{- fail "ACTIVE dataMode requires runtimeMode=ACTIVE" -}}
{{- end -}}
{{- if ne .Values.activation.confirmSingleWriter true -}}
{{- fail "ACTIVE requires activation.confirmSingleWriter=true" -}}
{{- end -}}
{{- if ne .Values.mysql.readOnly false -}}
{{- fail "ACTIVE requires mysql.readOnly=false" -}}
{{- end -}}
{{- if ne .Values.redis.readOnly false -}}
{{- fail "ACTIVE requires redis.readOnly=false" -}}
{{- end -}}
{{- if and .Values.ingress.enabled (eq (trim .Values.ingress.host) "") -}}
{{- fail "enabled ingress requires ingress.host" -}}
{{- end -}}
{{- end -}}
{{- if ne .Values.dataMode "RESTORE" -}}
{{- if not (regexMatch "^[0-9a-f]{64}$" .Values.mysql.expectedReceiptSha256) -}}
{{- fail "mysql.expectedReceiptSha256 must be an exact lowercase 64-hex artifact SHA in SHADOW/ACTIVE" -}}
{{- end -}}
{{- if not (regexMatch "^[0-9a-f]{64}$" .Values.redis.expectedReceiptSha256) -}}
{{- fail "redis.expectedReceiptSha256 must be an exact lowercase 64-hex artifact SHA in SHADOW/ACTIVE" -}}
{{- end -}}
{{- else -}}
{{- if and (ne .Values.mysql.expectedReceiptSha256 "") (not (regexMatch "^[0-9a-f]{64}$" .Values.mysql.expectedReceiptSha256)) -}}
{{- fail "mysql.expectedReceiptSha256 is either empty or exact lowercase 64-hex in RESTORE" -}}
{{- end -}}
{{- if and (ne .Values.redis.expectedReceiptSha256 "") (not (regexMatch "^[0-9a-f]{64}$" .Values.redis.expectedReceiptSha256)) -}}
{{- fail "redis.expectedReceiptSha256 is either empty or exact lowercase 64-hex in RESTORE" -}}
{{- end -}}
{{- end -}}
{{- end -}}
