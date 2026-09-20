# 남은 작업 (2026-09-19 기준 인계)

`ailab-operation` 실운영 배포·점검 세션에서 나온 것 중 아직 안 끝난 작업만 정리했다. 이미 끝난 것(배포 자체, e2e 검증, 관리자 계정 이전, Slack 알림 연동, 프로덕션 하드닝 1차분)은 뺐다.

## 🔴 막힌 작업 — 다른 접근 권한 필요

### 1. 사용자 컨테이너 SSH 외부 포트포워딩이 지금 안 됨
- **증상**: 컨테이너 SSH NodePort(30000번대~)가 외부(`210.94.179.18`)에서 전혀 안 열린다. 실제로 리스너까지 띄워서 확인함.
- **구 운영 기록**: `test0830v1/v2/v3`·`test0712`·`test0903v1`·`test0907v4` 등 여러 컨테이너의 실제 기록을 보면 **내부 NodePort 30000~30099 ↔ 외부 9300~9399**로 일관되게 매핑돼 있었다(예: `test0830v2`는 내부 30017 ↔ 외부 9317 — 실사용자가 직접 확인해준 값과 일치). 그런데 그 자리(내부 30050)에 지금 실제로 리스너를 띄우고 외부 9350으로 접속해봤더니 **응답이 없다** — 이 포워딩 규칙 자체가 지금은 죽어 있거나 옛 운영 장비를 가리키고 있다.
- **필요한 것**: 라우터/방화벽 장비 접속 정보. 그게 있어야 (a) 지금 죽어있는 포워딩을 살리고 (b) 요청받은 새 대역(FARM3 9200~9299 / FARM4 9300~9399 / FARM5 9400~9499)으로 맞출 수 있다.
- **주의**: `9200~9499`는 클러스터 쿠버네티스 API 서버가 실제로 허용하는 NodePort 범위(30000~32767, 실측 확인됨) 밖이므로, 저 값은 절대 k8s NodePort 자체로 쓰면 안 되고 **외부 노출용 별도 번호**로만 써야 한다. FARM3·5 쪽의 정확한 내부 NodePort 대역(FARM4=30000~30099로 추정 확인됨)은 아직 못 찾았다 — 확인된 이력 데이터가 FARM4 대역 컨테이너뿐이었음.

### 2. HTTPS/TLS
- 지금 `30081`(HTTP)로만 서비스된다. cert-manager가 이 클러스터에 없는 것까지는 확인함(`kubectl get pod -A | grep cert-manager` 등으로 재확인 가능).
- **다른 담당자가 처리하기로 함** — 여기서는 코드/설정 변경 안 함. 담당자가 cert-manager 설치 여부·방식을 정하면 그에 맞춰 ingress 쪽만 반영하면 됨(`ops/proposed-stack/admin-fe.yaml`의 `ailab-frontend` Ingress에 `tls:` 블록·`ssl-redirect: "true"` 추가).

## 🟠 아직 안 한 프로덕션 보강 (2026-09-18 점검에서 발견, 승인된 3건만 반영했고 나머지는 미착수)

- **mysql-0 백업 체계 없음** — PVC는 있어 Pod 재시작엔 안전하지만, PVC 손상·디스크 장애 복구 경로가 없다. 백업 크론잡(mysqldump → NFS/외부 저장) 추가 필요.
- **모든 컴포넌트 replica=1, HPA 없음** — admin-prod·ailab-frontend·config-server-operation·controller 등 전부 단일 인스턴스. stateless한 것(admin-prod, ailab-frontend, config-server 본체)부터 2 replica로 늘리는 게 상대적으로 쉬움. mysql은 real replication 설계가 먼저 필요해서 더 큰 작업.
- **config-server 자체 ingress 방향 NetworkPolicy 없음** — NodePort로 외부에서도 접근되는 구조라(배포 스크립트 자체 검증, 다른 운영 도구 등) 섣불리 막으면 그 경로가 깨질 위험이 있어 보류함. 실제로 필요한 호출자 목록(admin-prod, ailab-frontend, 외부 NodePort 접근 주체)을 먼저 정리해야 함.
- **클러스터 전역 로그 수집기 없음** — fluent/promtail/loki 계열 파드가 클러스터 어디에도 없음. 이 스택 범위를 넘는 클러스터 인프라 문제.

## 🟡 admin_be#541 나머지 범위 (일부만 이번에 구현함)
- 완료: Slack 예외 로그에 실제 원인 기록(#545, 단 webhook URL 노출 버그가 있어 #546에서 재수정함), 웹훅 삭제·토큰 폐기(404/410/401/403)를 `SLACK_CONFIG_DEAD`로 구분해 ERROR 로그로 격상.
- **미완료**: 이슈 3번 — 주기적으로 무해한 헬스체크 메시지를 Slack으로 보내 채널이 살아있는지 스스로 확인하는 자가 점검 경로. 지금은 실제 알림이 실패해야만(즉 진짜 사용자 신청이 와야만) 죽은 걸 안다.

## 🟢 이식성 (다른 클러스터/기관 재사용 시 코드를 고쳐야 하는 하드코딩)
다른 분이 검토·판단 후 필요하면 손보면 됨:
- `ops/proposed-stack/stack-up.sh`에 공인 IP `210.94.179.18`이 스크립트 안에 직접 박혀 있음(config/secret이 아님)
- NFS 마운트 가능 노드가 `csid-dgu-desktop` 하나로 스크립트·`config-server/Chart/values.yaml` 양쪽에 하드코딩
- `mysql.yaml`/`redis.yaml`의 `storageClassName: local-path`가 이 클러스터 전용 StorageClass 이름
- NodePort 유효 범위 검사(`ops/proposed-stack/uid-ranges.yaml` 사용처, `stack-up.sh`)가 `30000~32767`(이 클러스터의 쿠버네티스 기본값)로 하드코딩돼 있음 — `--service-node-port-range`가 다른 클러스터로 가면 이 검사 자체를 고쳐야 함
- **더 근본적**: 이 쿠버네티스 클러스터엔 LAB 노드가 아예 등록돼 있지 않음(FARM만). `resource_groups` 데이터는 LAB을 언급하지만 실제 클러스터가 모름 — LAB·FARM이 지금은 물리적으로 별개 인프라라, "설정만 다른 같은 구조"라는 설계 전제가 아직 실현 안 됐음.

## 참고 — 관련 열린 이슈
- `admin_be#541` 운영 승격 전 Slack 알림 경로 생존 확인·설정 오류 격상 필요 (부분 완료, 위 참고)
- `admin_be#542` 처리 중 갇힌 신청을 관리자가 정리할 수 없음
- `admin_be#508` 운영 admin_be를 default에서 ailab-be 네임스페이스로 이전
- `admin_infra-proposed#108` 노드 자원 부족 시 사용자 컨테이너가 가장 먼저 축출됨
- `admin_infra-proposed#53` 사용자 접근 시험을 admin_infra_server server-state 컴포넌트로
- `admin_infra-proposed#52` 접근 검증 home_io 파싱이 stderr 줄을 마운트 정보로 오인
