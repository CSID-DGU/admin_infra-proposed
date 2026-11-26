# config-server/Chart 디렉토리

config-server를 Kubernetes에 배포하기 위한 Helm chart이다.

| 파일/디렉토리 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `Chart.yaml` | Helm chart metadata이다. chart 이름은 `containerssh-config-server`이다. | Helm | chart 식별자와 버전 정보 |
| `values.yaml` | 이미지 repository/tag/digest, Service, 리소스, NFS 또는 독립 원장 PVC, namespace, Redis, nodeSelector/toleration 기본값이다. | Helm `--set` 또는 values override | template 렌더링 값 |
| `templates/` | Kubernetes manifest 템플릿이다. | `values.yaml`, release name | Deployment, Service, RBAC, ServiceAccount, ConfigMap, 제어기 Deployment, GPU 점검·Kerberos 재조정 CronJob |

이 디렉토리 자체에는 클래스나 함수가 없다. Helm helper 함수는 `templates/_helpers.tpl`에 있다.
`image.digest`가 있으면 Pod image는 `repository@sha256:...`로 렌더링되며 tag보다 우선한다. 기존 수동·개발
경로처럼 digest가 비어 있을 때만 `repository:tag`를 사용한다.

LAB API bootstrap은 `api.readOnly=true`를 사용한다. 이 값은 컨테이너에
`CONFIG_API_READ_ONLY=true`를 주입하고 `POST`·`PUT`·`PATCH`·`DELETE`를 403으로
거절하며, ServiceAccount Role도 Pod·Service·PVC·Event 조회만 허용한다. 이 모드에서는
controller와 두 CronJob을 함께 켤 수 없다. 기본값은 `false`이므로 기존 FARM 쓰기 API와
RBAC는 바뀌지 않는다.

FARM controlled upgrade의 검증 revision은 `api.maintenance=true`, `controller.replicas=0`, 두 CronJob
`suspend=true`를 함께 사용한다. maintenance API는 health와 인증된 GET은 유지하지만 HTTP 쓰기 메서드를
503으로 거절한다. 차트는 세 조건 중 하나라도 빠진 maintenance 조합을 렌더링하지 않는다.
