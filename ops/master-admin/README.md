# provi-master Admin 차트

`ops/master-admin/Chart`는 `provi-master` 단일 노드 관리 클러스터에 **Admin FE/BE와
Admin 전용 MySQL·Redis만** 배포한다. FARM/LAB Config Server의 DB, Redis, 사용자 Pod
자원은 만들지 않는다.

## 고정 경계

- Helm release: `master-admin`
- namespace: `provi-admin`
- node label: `provi.dgu.ac.kr/role=master-management`
- local PV: `/var/lib/provi/master-admin/{mysql,redis,backup}`
- 모든 PV의 reclaim policy: `Retain`
- 모든 PV/PVC와 StorageClass: `helm.sh/resource-policy: keep`
- Admin BE ServiceAccount token: 자동 마운트 금지
- 이미지: tag가 아닌 `repository@sha256:digest`만 허용

Helm은 비밀번호나 애플리케이션 설정 Secret을 만들지 않는다. 아래 Secret은 배포 전에
별도 root-only 절차로 만들며, 값은 Git·values·Helm release history에 넣지 않는다.

| Secret | 계약 |
| --- | --- |
| `master-admin-database` | `mysql-root-password`, `mysql-username`, `mysql-password`, `redis-password` |
| `master-admin-be-config` | 비밀값을 제거·치환한 Admin BE 설정 및 필요한 환경변수 |
| `master-admin-farm-readonly` | 선택 사항. `kubeconfig` 키 하나인 단기 FARM 읽기 전용 kubeconfig |

LAB kubeconfig mount는 차트에 존재하지 않는다.

## 데이터 보존 원칙

이 차트는 DB/Redis를 복사하거나 복구하지 않는다. 먼저 원본의 일관된 백업과 해시를
확보하고, `dataMode=RESTORE`에서 offline restore helper로 Retain PV 경로에 복구한다.
RESTORE는 FE/BE가 0대이고 Ingress가 없을 때만 렌더되며, MySQL/Redis를 데이터 적재에만
쓸 수 있게 연다. Redis RDB는 AOF가 켜진 빈 대상에 파일만 복사하면 로드되지 않으므로,
격리된 RESTORE 창에서 native `DUMP`/`RESTORE`로 이관해 대상 AOF에 기록하고 재시작 후
다시 검증해야 한다. 복구가 끝나면 각 PV 루트에 아래 root-owned `0444` 영수증을 원자적으로
설치한다. 시각은 UTC RFC3339 초 단위다.

```text
artifact_sha256=<백업 아티팩트의 소문자 64-hex SHA-256>
verified_at=2026-11-28T12:34:56Z
```

SHADOW/ACTIVE의 datastore initContainer는 정확히 두 줄인지, `root:root:444`인지,
`expectedReceiptSha256`와 같은지 모두 확인한다. 하나라도 다르면 MySQL/Redis 본체가 시작되지
않는다. SHADOW의 MySQL은 `read_only`와 `super_read_only`, Redis는 read-only ACL로 다시
쓰기를 막는다.
Admin BE도 `PROVI_RUNTIME_MODE=SHADOW`, Flyway 비활성화, Hibernate `validate`, Hikari
read-only로 뜨며 Ingress를 만들 수 없다.

`helm uninstall`은 데이터 삭제 절차가 아니다. keep annotation과 Retain policy로 PV/PVC를
남기지만, 운영에서는 uninstall 대신 검증된 이전 revision으로 rollback한다. PV 디렉터리를
지우거나 초기화하는 명령은 이 경로에 두지 않는다.

## 전환 순서

1. RESTORE: FE/BE 없이 일관된 MySQL/Redis 백업을 적재하고 영수증을 설치한다.
2. SHADOW: 원본은 writer로 유지하고, 영수증과 일치하는 clone을 read-only로 검증한다.
3. old writer를 정지하고 최종 MySQL dump/Redis snapshot을 만든 뒤 해시와 row/keyspace
   계약을 확인해 Master에 한 번 더 복구한다.
4. ACTIVE stage 1: `activation.confirmSingleWriter=true`, MySQL/Redis write 허용,
   `adminBackend.replicas=0`, `adminFrontend.replicas=0`, `ingress.enabled=false`로 DB만 검증한다.
5. ACTIVE stage 2: 같은 source SHA와 image digest를 유지한 채 FE/BE를 1대로 올린다.
6. 내부 probe가 끝난 뒤에만 Ingress를 켜고 기존 `30081` 경로를 넘긴다.

ACTIVE는 `activation.confirmSingleWriter=true`, `mysql.readOnly=false`,
`redis.readOnly=false`가 모두 아니면 렌더 단계에서 실패한다. source SHA와 이미지 digest는
Pod template annotation과 release contract ConfigMap에 기록되어 SHADOW와 ACTIVE 사이의
코드 drift를 검사할 수 있다.

## 로컬 검증

```bash
helm lint ops/master-admin/Chart \
  --namespace provi-admin \
  -f ops/master-admin/Chart/fixtures/values-shadow-test.yaml

helm template master-admin ops/master-admin/Chart \
  --namespace provi-admin \
  -f ops/master-admin/Chart/fixtures/values-shadow-test.yaml

pytest -q ops/master-admin/Chart/tests
```

fixture의 digest는 렌더 계약 검사용이며 실제 배포 image 선택을 대신하지 않는다. 특히
Admin BE는 `PROVI_RUNTIME_MODE` 안전장치를 포함한 새 commit으로 다시 build한 digest를 쓴다.
