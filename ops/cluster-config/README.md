# FARM/LAB Config Server 배포 경계

기존에 설치된 FARM 또는 LAB Config Server Helm release만 읽기 전용으로 점검하거나, 같은 보호된 소스
커밋의 chart와 digest 고정 이미지를 함께 적용한다.
Admin FE/BE, MySQL, Redis, Secret, PVC와 사용자 Pod는 이 경로의 배포 대상이 아니다.

```bash
export KUBECONFIG=/etc/kubernetes/ci-deployer.conf

# Kubernetes 쓰기 없음
bash ops/cluster-config/deploy.sh --target farm --action probe
bash ops/cluster-config/deploy.sh --target lab --action probe

# 보호된 tag의 정확한 chart와 digest-pinned 이미지를 기존 release에 적용
bash ops/cluster-config/deploy.sh --target farm --action upgrade \
  --image-tag 0123456789abcdef0123456789abcdef01234567 \
  --image-digest sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
bash ops/cluster-config/deploy.sh --target lab --action upgrade \
  --image-tag 0123456789abcdef0123456789abcdef01234567 \
  --image-digest sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

FARM 명령은 `farm-mgmt`의 `svmanager`, LAB 명령은 `lab-mgmt`의 `svmanager`에서만 실행된다. 두 호스트 모두
root 소유, `svmanager` 그룹, mode `0640`인 `/etc/kubernetes/ci-deployer.conf`를 사용한다.

LAB은 Phase 7-B 전 API bootstrap 계약을 강제한다. `api.readOnly=true`로 HTTP 쓰기 메서드와
Kubernetes mutation RBAC를 함께 닫고 `/health.read_only=true`를 확인한다.
`lab-config-ledger`·`lab-config-image-store` PVC를 사용하고,
LAB 전용 MySQL·Redis가 Ready인지를 확인하며, controller와 GPU/Kerberos CronJob, NFS, Kerberos, NAS, AD SSH,
사용자 홈 경로와 Admin BE callback은 비활성 상태여야 한다.
이 조건 중 하나라도 달라지면 probe와 upgrade 모두 중단한다.

기존 Phase 7-A release는 `api.readOnly`가 도입되기 전 이미지이므로 예외를 제한한다.
`probe`는 현재 Helm values의 `api.readOnly=true`와 현재 `/health.read_only=true`를 모두 요구해
구 bootstrap을 안전하다고 오판하지 않는다. `upgrade`만 한 번의 전환 경로로 기존 값의 부재를
허용하며, 렌더링과 Helm 적용에 `api.readOnly=true`를 강제한 뒤 values와 health를 다시 검증한다.

또한 Phase 7-A foundation이 이름만 남고 내용이 바뀐 상태를 허용하지 않는다.
`lab-config-local` StorageClass, 두 Retain PV/PVC의 정확한 volumeName·hostPath·용량·LAB-MGMT
affinity, MySQL `8.0.36`과 Redis `7.4-alpine`의 Recreate Deployment·hostPath·관리 노드 배치,
각 Service selector를 upgrade 전후에 모두 검증한다.

이 진입점은 다음 작업을 하지 않는다.

- Helm release 신규 설치 또는 삭제
- 전체 proposed stack 실행
- 애플리케이션 Secret 생성·복사·교체(Helm 자체 release 이력 Secret은 기존 release upgrade/rollback 과정에서 갱신됨)
- DB·Redis·PVC 생성 또는 schema 변경
- Storage/Kerberos/NFS 연동 활성화
- FARM과 LAB 간 설정 복사

`upgrade`는 기존 release의 계산된 values 전체를 rootless 임시 디렉터리에 mode `0600`으로 저장하고, 보호된
tag의 chart에 다시 주입하면서 image tag와 registry digest를 같은 source commit의 산출물로 고정한다. 즉 chart
변경도 명시적으로 적용되는 경로다. Helm 적용 중 실패하면 `--rollback-on-failure`를 사용하고, 적용 후의
health·values·writer gate·보호 Pod 검증이 실패해도 시작 직전 revision으로 명시적으로 rollback한다.

FARM은 새 작업과 CronJob 경쟁 조건을 막기 위해 Helm revision을 두 번 사용한다. 첫 revision이
`api.maintenance=true`, controller `replicas=0`, 두 CronJob `spec.suspend=true`를 함께 소유한 상태로
chart·image를 적용한다. API health와 조회는 유지하지만 새 HTTP 쓰기는 503으로 거절하며, 이 닫힌 구간에서
작업 원장·배치·사용자 workload 불변성을 검증한다. 모든 검증이 끝난 뒤 두 번째 revision이 동일한
chart·image에서 maintenance를 해제하고 controller 1개와 두 CronJob을 함께 복구한다. 이 최종 Helm 명령이
성공한 순간부터 새 사용자 작업이 접수될 수 있으므로 이후의 관찰 실패는 시작 전 revision으로 rollback하지
않는다. 재개 직후에는 missed schedule도 정상 실행될 수 있어 작업 idle이나 사용자 workload snapshot을 다시
실패 조건으로 삼지 않고 최종 values·배치 상태·관리 노드 배치·health만 확인한다.
FARM의 기존 immutable Deployment selector는 `legacy`, 신규 LAB release는 `release`로 각각 고정 검증한다.

`[CHANGE]`가 출력된 뒤에는 GitHub Actions run을 수동 취소하지 않는다. 스크립트는 오류·`INT`·`TERM`에서
직전 revision rollback과 API/controller/CronJob 복구를 시도하지만, runner 프로세스를 강제로 종료하면 그
trap을 완료할 수 없다. 이 경우 `/tmp/provi-cluster-config.*`의 mode `0700` 임시 디렉터리가 남을 수 있으므로
해당 run이 완전히 끝난 뒤 소유자·내용을 확인하고 별도로 정리한다. workflow 제한 시간은 정상 실패 경로의
drain·rollback·recovery를 포함하도록 120분이다.
