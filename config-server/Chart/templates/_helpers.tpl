{{- define "containerssh-config-server.fullname" -}}
{{- printf "%s" .Release.Name -}}
{{- end -}}

{{/*
API 서버(deployment.yaml)와 제어기(controller.yaml)가 공유하는 환경변수. 둘 다 같은 이미지·설정을 쓰므로
여기서 한 번만 관리한다.
*/}}
{{- define "containerssh-config-server.env" -}}
- name: NFS_SERVER
  value: "{{ .Values.nfs.server }}"
- name: NFS_USER_SHARE_PATH
  value: "{{ .Values.nfs.userSharePath }}"
- name: NAS_SSH_HOST
  value: "{{ .Values.nas.ssh.host }}"
- name: NAS_SSH_PORT
  value: "{{ .Values.nas.ssh.port }}"
- name: NAS_SSH_USER
  value: "{{ .Values.nas.ssh.user }}"
- name: NAS_SSH_KEY_PATH
  value: "{{ .Values.nas.ssh.keyPath }}"
- name: SUDO_ALLOWED_COMMANDS
  value: "{{ .Values.security.sudoAllowedCommands }}"
- name: NAMESPACE
  value: "{{ .Values.config.namespace }}"
- name: ADMIN_BE_INTERNAL_URL
  value: "{{ .Values.infra.adminBeInternalUrl }}"
- name: UID_MIN
  value: "{{ .Values.accounts.uidMin }}"
- name: UID_MAX
  value: "{{ .Values.accounts.uidMax }}"
- name: ACCOUNT_PREFIX
  value: "{{ .Values.accounts.prefix }}"
- name: NODEPORT_MIN
  value: "{{ .Values.nodeport.min }}"
- name: NODEPORT_MAX
  value: "{{ .Values.nodeport.max }}"
- name: VERIFY_MODE
  value: "{{ .Values.verifyMode }}"
- name: DB_HOST
  value: "{{ .Values.db.host }}"
- name: DB_NAME
  value: "{{ .Values.db.name }}"
- name: DB_USER
  value: "{{ .Values.db.user }}"
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: config-server-db-secret
      key: password
- name: LOG_DB_HOST
  value: "{{ .Values.logDb.host }}"
- name: LOG_DB_NAME
  value: "{{ .Values.logDb.name }}"
- name: LOG_DB_USER
  value: "{{ .Values.logDb.user }}"
- name: LOG_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: log-mysql-secret
      key: MYSQL_PASSWORD
- name: REDIS_HOST
  value: "{{ .Values.redis.host }}"
- name: REDIS_PORT
  value: "{{ .Values.redis.port }}"
- name: KRB5_REALM
  value: "{{ .Values.krb5.realm }}"
- name: FARM_SSH_USER
  value: "{{ .Values.farm.ssh.user }}"
- name: FARM_SSH_KEY_PATH
  value: "{{ .Values.farm.ssh.keyPath }}"
- name: FARM_NODES_JSON
  value: '{{ .Values.farm.ssh.nodes | toJson }}'
- name: FARM_AD_SSH_USER
  value: "{{ .Values.farm.adSsh.user }}"
- name: FARM_AD_SSH_KEY_PATH
  value: "{{ .Values.farm.adSsh.keyPath }}"
- name: FARM_AD_DC_NODES_JSON
  value: '{{ .Values.farm.adSsh.nodes | toJson }}'
# admin_be와 공유하는 내부 API 토큰. Secret이 없으면 비어 검사가 꺼진다.
- name: CONFIG_API_TOKEN
  valueFrom:
    secretKeyRef:
      name: "{{ .Values.apiToken.secretName }}"
      key: token
      optional: true
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
- name: farm-ssh-key
  mountPath: /etc/farm-ssh
  readOnly: true
- name: farm-ad-ssh-key
  mountPath: /etc/farm-ad-ssh
  readOnly: true
{{- end -}}

{{- define "containerssh-config-server.volumes" -}}
- name: nas-ssh-key
  secret:
    secretName: nas-ssh-key
    defaultMode: 0400
- name: farm-ssh-key
  secret:
    secretName: farm-ssh-key
    defaultMode: 0400
- name: farm-ad-ssh-key
  secret:
    secretName: farm-ad-ssh-key
    defaultMode: 0400
- name: image-store
  {{- if .Values.imageStore.claimName }}
  persistentVolumeClaim:
    claimName: {{ .Values.imageStore.claimName }}
  {{- else }}
  emptyDir: {}
  {{- end }}
- name: kube-share
  nfs:
    server: "{{ .Values.nfs.server }}"
    path: "{{ .Values.nfs.kubeSharePath }}"
- name: krb5-conf
  configMap:
    name: krb5-conf
{{- end -}}
