# 실험 스택 설치

제안 시스템을 `ailab-noprobe`, `ailab-full` 네임스페이스에 띄우는 스크립트다. 직접 실행하지 않고 **CSID-DGU/admin_infra의 Actions → "Deploy Proposed Stack"**에서 실행한다. 새로 등록할 시크릿은 없다.

| 입력 | 값 |
| --- | --- |
| stack | `noprobe` 또는 `full` |
| ref | 이 레포의 브랜치·태그·커밋 |
| action | `up`(띄우기, 이미 있으면 재배포) / `down`(테스트 계정 정리 후 내리기) |

```bash
gh workflow run deploy-proposed-stack.yaml -R CSID-DGU/admin_infra -f stack=noprobe -f ref=develop
```

두 스택에 같은 코드를 올릴 때는 `ref`에 같은 커밋 SHA를 준다. 두 스택의 차이는 `VERIFY_MODE` 하나여야 한다.

## 스택별 할당 값

| 항목 | `ailab-noprobe` | `ailab-full` |
| --- | --- | --- |
| config-server 릴리스 | `config-server-noprobe` | `config-server-full` |
| config-server nodePort | 30182 | 30282 |
| `VERIFY_MODE` | `noprobe` | `full` |
| UID/GID 대역 | 50000~54999 | 55000~59999 |
| 사용자 Pod NodePort 대역 | 32000~32249 | 32250~32499 |
| 테스트 계정 접두어 | `exp-np-` | `exp-fu-` |
| 계정 대장 경로 | 운영 `kubeSharePath`/`exp-noprobe` | 운영 `kubeSharePath`/`exp-full` |

운영은 UID 20000대, NodePort 30000~32767 전체, config-server 30082, admin_be 30083을 쓴다.

## `stack-up.sh`가 하는 일

| 순서 | 작업 |
| --- | --- |
| 1 | 운영 계정 대장에 이 스택의 UID 대역을 쓰는 계정이 없는지, nodePort가 비어 있는지 확인. 아니면 중단 |
| 2 | 네임스페이스 생성, 운영의 SSH 키 시크릿 3개 복사 |
| 3 | DB 비밀번호를 무작위로 만들어 `stack-db` 시크릿에 저장(한 번만) |
| 4 | MySQL 한 대에 `pod_port_db`, `operation_state_db`, `web_admin`을 만들고 `infra-sql`의 테이블 정의 적용 |
| 5 | Redis 두 대(config-server용 인증 없음, admin_be용 비밀번호) |
| 6 | 이미지 저장소는 임시 디스크를 씀(사용자 이미지 커밋·재시작용이라 실험에 필요 없음) |
| 7 | 계정 대장 경로 생성 |
| 8 | config-server 설치. NFS·NAS·Kerberos·farm 설정은 운영 릴리스 값을 그대로 쓰고 스택별 값만 덮어씀 |
| 9 | admin_be 설치. 운영 이미지를 digest로 고정하고 DB·Redis·config-server 주소, Slack·메일, JWT 서명키를 덮어씀. 같은 네임스페이스와 DNS 외에는 나가는 연결을 네트워크 정책으로 막음 |
| 10 | 테스트 계정 `<접두어>000`을 만들고 지워서 UID 대역, 작업 이력, 운영 대장에 흔적이 없는지 확인 |

여러 번 실행해도 결과가 같다. 비밀번호는 처음 만든 값을 유지한다.

## 운영과 공유하는 것

AD·Kerberos, NAS, farm 노드는 운영과 같이 쓴다. 테스트 계정은 반드시 접두어로 만들고, 반복 실험 사이에 계정을 지울 때도 접두어로 거른다. 계정 삭제는 NAS 홈을 지운다.

## 로그

admin_infra는 공개 레포라 Actions 로그를 누구나 볼 수 있다. 스크립트를 고칠 때 비밀번호, 운영 설정값, 실사용자 계정 이름을 출력하지 않는다.
