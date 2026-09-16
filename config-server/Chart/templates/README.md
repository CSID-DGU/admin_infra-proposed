# config-server/Chart/templates 디렉토리

config-server Helm chart가 렌더링하는 Kubernetes 리소스 템플릿이다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `_helpers.tpl` | `containerssh-config-server.fullname` Helm helper를 정의한다. | `.Release.Name` | release 이름 기반 fullname 문자열 |
| `deployment.yaml` | config-server Deployment를 생성한다. | image repository/tag/pullPolicy, namespace, NFS server/path, resource, nodeSelector, tolerations | `/kube_share`, `/image-store`를 mount한 Flask/gunicorn Pod |
| `service.yaml` | config-server HTTP Service를 생성한다. | service type/port/targetPort/nodePort | `containerssh-config-service` Service |
| `serviceaccount.yaml` | config-server가 Kubernetes API를 호출할 ServiceAccount를 생성한다. | namespace | `config-server` ServiceAccount |
| `rbac.yaml` | Pod, Service, PVC, Pod exec/log, Node 조회, Secret(생성·조회·수정·삭제) 권한을 부여한다. | namespace, release name | Role/RoleBinding, ClusterRole/ClusterRoleBinding |
| `configmap.yaml` | config-server 설정값을 ConfigMap으로 만든다. | `values.yaml` | 컨테이너가 읽는 설정 |
| `controller.yaml` | 등록된 작업을 실행하는 제어기 Deployment를 생성한다. | 폴링 주기, 동시 처리 수 | API 서버와 분리된 제어기 Pod |
| `cronjob-gpu-check.yaml` | GPU 유실을 주기적으로 점검한다. | 알림 주소 | 점검 Job |
| `cronjob-krb5-reconcile.yaml` | 노드별 Kerberos 상태를 주기적으로 맞춘다. | 대상 노드 목록 | 재조정 Job |

클래스는 없다. Helm helper 함수는 다음 1개이다.

| 함수 | 역할 | 입력 | 출력 |
| --- | --- | --- | --- |
| `containerssh-config-server.fullname` | 리소스 이름에 사용할 fullname을 만든다. | `.Release.Name` | release name 문자열 |
