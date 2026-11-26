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

LAB에는 이 개별 스위치보다 상위의 Phase 7-B latch인
`lab.lifecycleDependenciesReady`가 있다. 기본값은 `false`다. 이 상태에서 차트는 다음
API-only 계약을 모두 만족하지 않으면 `helm template`과 `helm upgrade`를 즉시 중단한다.

- `api.readOnly=true`, controller와 두 CronJob 비활성화
- NFS 주소·경로와 Kerberos client 설정을 비우고 `krb5.kdcHosts`는 빈 list로 유지
- NAS SSH 주소·포트·key path를 비우고 NAS/노드/AD SSH 사용자를
  `disabled-until-phase7b`로 잠금
- 노드/AD SSH key path와 사용자 홈 mount template을 비우고 `cluster.adSsh.nodes`는
  빈 list로 유지
- Admin BE와 Prometheus를 `http://127.0.0.1:9` loopback sink로 고정
- FARM NFS와 `emptyDir` 대신 사용할 독립 원장·image-store PVC를 각각
  `kubeShare.claimName`, `imageStore.claimName`에 지정

Storage/Kerberos/NFS와 lifecycle 쓰기 경로를 한 번에 검증하는 Phase 7-B 변경에서만 latch를
`true`로 전환한다. latch만 먼저 열거나 의존성 하나만 미리 여는 중간 상태는 운영 배포 절차로
사용하지 않는다. 이 검증은 `cluster.id=lab`에만 적용되므로 FARM 렌더링에는 영향을 주지 않는다.

FARM controlled upgrade의 검증 revision은 `api.maintenance=true`, `controller.replicas=0`, 두 CronJob
`suspend=true`를 함께 사용한다. maintenance API는 health와 인증된 GET은 유지하지만 HTTP 쓰기 메서드를
503으로 거절한다. 차트는 세 조건 중 하나라도 빠진 maintenance 조합을 렌더링하지 않는다.
