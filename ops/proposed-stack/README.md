# 실험 스택 설치

제안 시스템을 `ailab-baseline`, `ailab-noprobe`, `ailab-full`(실험)과 `ailab-operation`(운영) 네임스페이스에 띄우는 스크립트다. 직접 실행하지 않고 **CSID-DGU/admin_infra의 Actions → "Deploy Proposed Stack"**에서 실행한다. 새로 등록할 시크릿은 없다.

| 입력 | 값 |
| --- | --- |
| stack | `baseline`, `noprobe`, `full`, `operation` |
| ref | 이 레포의 브랜치·태그·커밋 (기본 `main`) |
| fe_ref / be_ref | admin_fe / admin_be의 브랜치·태그·커밋 (기본 `main`) |
| action | `up`(띄우기, 이미 있으면 재배포) / `down`(테스트 계정 정리 후 내리기) |

```bash
gh workflow run deploy-proposed-stack.yaml -R CSID-DGU/admin_infra -f stack=baseline
# 운영: 네 저장소에 같은 릴리스 태그를 찍고 그 태그만 준다(워크플로 guard가 막는다)
gh workflow run deploy-proposed-stack.yaml -R CSID-DGU/admin_infra -f stack=operation -f ref=v3.0.0 -f fe_ref=v3.0.0 -f be_ref=v3.0.0
```

세 스택에는 같은 커밋(`ref`, `fe_ref`, `be_ref` 모두)을 올린다. 세 스택의 차이는 config-server의 실행 방식(`VERIFY_MODE`) 하나여야 한다.

## 스택별 할당 값

| 항목 | `ailab-baseline` | `ailab-noprobe` | `ailab-full` |
| --- | --- | --- | --- |
| config-server 릴리스 | `config-server-baseline` | `config-server-noprobe` | `config-server-full` |
| config-server nodePort | 30382 | 30182 | 30282 |
| 화면 주소 | `http://baseline.210.94.179.18.nip.io:30081` | `http://noprobe.210.94.179.18.nip.io:30081` | `http://full.210.94.179.18.nip.io:30081` |
| 실행 방식(`VERIFY_MODE`) | `baseline` | `noprobe` | `full` |
| UID/GID 대역 | 60000~64999 | 50000~54999 | 55000~59999 |
| 사용자 Pod NodePort 대역 | 32500~32749 | 32000~32249 | 32250~32499 |
| 테스트 계정 접두어 | `exp-bl-` | `exp-np-` | `exp-fu-` |
| 계정 대장 경로 | 운영 `kubeSharePath`/`exp-baseline` | 운영 `kubeSharePath`/`exp-noprobe` | 운영 `kubeSharePath`/`exp-full` |

| 실행 방식 | 단계 실패 | 결과 불명 | 제어기 재시작 | 접근 시험 |
| --- | --- | --- | --- | --- |
| `baseline` | 재시도 없이 운영과 같은 뒷정리(이번 작업이 만든 계정 되돌리기) 후 FAIL | 조회 없이 FAIL | 이어하지 않고 노드 미상으로 뒷정리 후 FAIL(`INTERRUPTED`) | 없음 |
| `noprobe` | 재시도, 한도 넘으면 DEGRADED | 먼저 실제 상태 조회 | 멈춘 단계부터 이어서 | 없음 |
| `full` | noprobe와 같음 | noprobe와 같음 | noprobe와 같음 | 있음 |

운영은 UID 20000대, NodePort 30000~32767 전체, config-server 30082, admin_be 30083을 쓴다.

## `stack-up.sh`가 하는 일

| 순서 | 작업 |
| --- | --- |
| 1 | 운영 계정 대장에 이 스택의 UID 대역을 쓰는 계정이 없는지, nodePort가 비어 있는지 확인. 아니면 중단 |
| 2 | 네임스페이스 생성, 운영의 SSH 키 시크릿 3개 복사 |
| 3 | DB 비밀번호를 무작위로 만들어 `stack-db` 시크릿에 저장(한 번만) |
| 4 | MySQL 한 대에 `pod_port_db`, `operation_state_db`, `web_admin`을 만들고 `infra-sql`의 테이블 정의 적용 |
| 5 | Redis 두 대(config-server용 인증 없음, admin_be용 비밀번호). config-server용(`redis-bg-master`)은 제어기(v2.0)가 처리 전 대기열을 잃지 않도록 AOF+RDB로 영속화하고 PVC 1Gi를 씀 |
| 6 | 이미지 저장소는 임시 디스크를 씀(사용자 이미지 커밋·재시작용이라 실험에 필요 없음) |
| 7 | 계정 대장 경로 생성 |
| 8 | config-server 설치(`controller.enabled=true`로 v2.0 제어기도 같이 설치). NFS·NAS·Kerberos·farm 설정은 운영 릴리스 값을 그대로 쓰고 스택별 값만 덮어씀 |
| 9 | 프론트엔드 설치. `nginx.conf`의 `/api/`·`/pod-status/` 대상만 스택으로 바꿔 빌드한 이미지 |
| 9-1 | admin_be 설치. `be_ref`로 빌드한 이미지에 DB·Redis·config-server 주소, Slack·메일, JWT 서명키를 덮어씀. 같은 네임스페이스·DNS·메일·쿠버네티스 API 외에는 나가는 연결을 네트워크 정책으로 막음 |
| 10 | 테스트 계정 `<접두어>000`을 만들고 지워서 UID 대역, 작업 이력, 운영 대장에 흔적이 없는지 확인(동기 API) |
| 11 | 테스트 계정 `<접두어>001`로 v2.0 비동기 작업(`/operations/provision`·`/operations/revoke`)을 등록하고, 제어기가 실제로 처리해 계정을 만들고 지우는지 확인 |

여러 번 실행해도 결과가 같다. 비밀번호는 처음 만든 값을 유지한다.

## 운영과 공유하는 것

AD·Kerberos, NAS, farm 노드는 운영과 같이 쓴다. 테스트 계정은 반드시 접두어로 만들고, 반복 실험 사이에 계정을 지울 때도 접두어로 거른다. 계정 삭제는 NAS 홈을 지운다.

## 로그

admin_infra는 공개 레포라 Actions 로그를 누구나 볼 수 있다. 스크립트를 고칠 때 비밀번호, 운영 설정값, 실사용자 계정 이름을 출력하지 않는다.

## 화면 접속과 관리자 계정

방화벽이 새 포트를 막고 있어, 화면은 운영 프론트엔드가 쓰는 30081에 호스트 이름 규칙으로 연다. 위 표의 주소로 접속한다.

가입은 운영과 같이 메일 인증을 거친다. **우분투 사용자명은 반드시 스택 접두어(`exp-bl-`, `exp-np-`, `exp-fu-`)로 시작해야 한다.** AD·Kerberos, NAS 홈, farm keytab을 운영과 같이 쓰고 이름으로 구분하기 때문에, config-server가 접두어 없는 이름을 거절한다. 스택 admin_be는 운영 메일 설정으로 인증 코드를 보내고, Slack 알림은 보내지 않는다. 가입한 계정을 관리자로 지정하려면 배포 서버에서 실행한다(이메일이 공개 로그에 남지 않도록 워크플로로 돌리지 않는다).

```bash
bash ops/proposed-stack/make-admin.sh noprobe <가입한 이메일>
```

## kubectl로 직접 시험

```bash
kubectl -n ailab-noprobe port-forward svc/containerssh-config-service 8000:80   # config-server
kubectl -n ailab-noprobe port-forward svc/ailab-frontend 8080:80                # 화면
kubectl -n ailab-noprobe get pods -o wide
```

## 스택 admin DB의 기준 데이터

신청 화면에 필요한 기준 데이터(자원 그룹 `resource_groups`, 노드 `nodes`, GPU `gpus`, 이미지 `container_image`/`resource_group_images`, 메일 문구 `message_templates`)는 스택에 이미 있는 값을 그대로 쓴다. 운영은 평소 0대로 내려가 있는 게 정상이라(9/15~) 설치 때마다 운영 DB에서 새로 복사하지 않는다. 새 스택을 처음 띄워 기준 데이터가 하나도 없으면 다른 스택에서 옮기거나 운영을 잠시 띄워 수동으로 넣어야 한다.

## 스택 admin_be (브랜치 빌드)

admin_be는 세 스택 모두 작업 등록 인터페이스(`POST /operations/provision`·`revoke`, `GET /operations/{kind}/{신청번호}`)만 쓴다. baseline도 같은 be를 쓰고, 방식 차이는 config-server 제어기에서만 난다(9/14 결정). 옛 동기 경로는 admin_be `legacy-sync` 태그에 남아 있다.

워크플로의 `be_ref`(기본 `main`)로 `admin-prod-exp:<sha>` 이미지를 빌드해 올린다. 어느 저장소에도 push로 도는 배포는 없다.

스택 전용 이미지는 설정 파일을 굽지 않는다. 설정은 운영 `admin-prod-config` Secret 복사본을 `/app/config`에 마운트해서 받고, 스택 자원을 가리키는 값만 `SPRING_APPLICATION_JSON`으로 덮어쓴다.
