# infra-sql 디렉토리

실험 스택이 쓰는 데이터베이스 스키마(DDL)와 그 저장소용 StorageClass 정의다. 설치 스크립트가 데이터베이스를 띄운 뒤 스키마 파일을 적용한다.

운영 원장 MySQL 매니페스트(`infra-mysql.yaml`)는 이 저장소에서 쓰이지 않아 지웠다 — 실험 스택은 `ops/proposed-stack/mysql.yaml`이 자체 인스턴스를 띄운다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `log-mysql.yaml` | 작업 이력 전용 MySQL StatefulSet과 Service. 운영 원장과 물리적으로 분리해 실험·기록 트래픽이 실사용자 요청 경로에 영향을 주지 않게 한다. 자격증명은 파일에 두지 않고 별도로 만든 Secret을 참조한다. | MySQL 이미지, 자격증명 Secret, StorageClass | `log-mysql` StatefulSet, PVC, Service |
| `nfs-mysql.yaml` | MySQL StatefulSet에서 사용할 NFS CSI StorageClass `sc-mysql`을 정의한다. | NFS 서버·공유 경로 | 확장 가능한 Retain StorageClass |
| `operation_log.sql` | `operation_log` 테이블 DDL. 전용 MySQL은 데이터베이스(`operation_state_db`)만 만들고 스키마는 만들지 않으므로, 인스턴스를 새로 띄운 뒤 이 파일을 적용해야 한다. `config-server/adapters/operation_log.py`의 INSERT 컬럼 목록과 1:1로 대응한다. | `operation_state_db` | `operation_log`·`job_control` 테이블 |
| `pod_port_db.sql` | `infra-mysql`의 `pod_port_db` 테이블 DDL(`krb5_cleanup_pending`, `nodeport_allocations`, `server_nodes`). 운영에서 스키마만 추출한 것이다. 코드가 테이블을 자동으로 만들지 않으므로 인스턴스를 새로 띄우면 이 파일을 적용한다. `server_nodes`는 현재 어떤 코드도 읽지 않는다. | `pod_port_db` | 테이블 3개 |
| `trial_manifest.sql` | `trial_manifest` 테이블 DDL. 평가 실험 1회의 조건(비교군·서버 그룹·작업 종류·관측 구간·반복 회차·구성요소 revision)과 시간창을 기록한다. Trial Runner가 대상 시스템 바깥에서 쓰며, `operation_log`와 같은 데이터베이스에 두어 `request_id`와 시간창으로 조인한다. | `operation_state_db` | `trial_manifest` 테이블 |

클래스나 함수는 없다.
