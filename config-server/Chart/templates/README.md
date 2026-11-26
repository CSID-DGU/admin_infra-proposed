# config-server/Chart/templates 디렉토리

config-server Helm chart가 렌더링하는 Kubernetes 리소스 템플릿이다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `_helpers.tpl` | 릴리스별 이름·라벨·Secret 이름, digest 우선 image reference와 공통 환경변수를 정의한다. | `.Release.Name`, `cluster.id`, FARM/LAB values | 릴리스별 fullname, labels, env, volume/image helpers |
| `deployment.yaml` | config-server Deployment를 생성한다. | image repository/tag/digest/pullPolicy, namespace, NFS server/path 또는 LAB 전용 원장 PVC, resource, nodeSelector, tolerations | `/kube_share`, `/image-store`를 mount한 Flask/gunicorn Pod |
| `service.yaml` | config-server HTTP Service를 생성한다. | release name, selector compatibility mode, service type/port/targetPort/nodePort | legacy FARM의 `containerssh-config-service` 또는 신규 릴리스 이름의 Service |
| `serviceaccount.yaml` | config-server가 Kubernetes API를 호출할 ServiceAccount를 생성한다. | release name, namespace | 릴리스 이름의 ServiceAccount |
| `rbac.yaml` | FARM에는 기존 Pod·Service·PVC·Pod exec/log·Secret 쓰기 권한을 유지한다. `api.readOnly=true`인 LAB bootstrap에는 Pod·Service·PVC·Event 조회만 부여한다. Node/Service cluster reader는 조회 전용이다. | namespace, release name, `api.readOnly` | Role/RoleBinding, ClusterRole/ClusterRoleBinding |
| `configmap.yaml` | 클러스터별 Kerberos 설정을 ConfigMap으로 만든다. | realm, KDC 목록, domain | 릴리스별 `krb5.conf` ConfigMap |
| `controller.yaml` | 등록된 작업을 실행하는 제어기 Deployment를 생성한다. | 폴링 주기, 동시 처리 수 | API 서버와 분리된 제어기 Pod |
| `cronjob-gpu-check.yaml` | GPU 유실을 주기적으로 점검한다. | 알림 주소 | 점검 Job |
| `cronjob-krb5-reconcile.yaml` | 노드별 Kerberos 상태를 주기적으로 맞춘다. | 대상 노드 목록 | 재조정 Job |

클래스는 없다. 주요 Helm helper 함수는 다음과 같다.

| 함수 | 역할 | 입력 | 출력 |
| --- | --- | --- | --- |
| `containerssh-config-server.fullname` | 리소스 이름에 사용할 fullname을 만든다. | `.Release.Name` | release name 문자열 |
| `containerssh-config-server.clusterId` | 필수 클러스터 식별자를 검증한다. | `cluster.id` | DNS label 형식의 FARM/LAB 식별자 |
| `containerssh-config-server.labels` | 모든 리소스에 릴리스와 클러스터 식별 라벨을 붙인다. | release, cluster id | 공통 Kubernetes labels |
| `containerssh-config-server.env` | API·controller·reconcile이 공유할 환경변수를 만든다. | cluster/NFS/DB/Redis/SSH values | `CLUSTER_ID`, `INFRA_*`를 포함한 env 목록 |
| `containerssh-config-server.apiReadOnly` | LAB bootstrap의 읽기 전용 API 모드를 검증한다. FARM 또는 background writer와 함께 사용하면 렌더링을 중단한다. | `api.readOnly`, controller/CronJob enable 값 | `true` 또는 `false` |
| `containerssh-config-server.apiMaintenance` | FARM controlled upgrade의 임시 쓰기 차단과 background writer 정지를 함께 검증한다. | `api.maintenance`, controller replicas, CronJob suspend | `true` 또는 `false` |
| `containerssh-config-server.image` | digest가 있으면 tag보다 우선해 불변 image reference를 만든다. | image repository/tag/digest | `repository@sha256:...` 또는 `repository:tag` |
