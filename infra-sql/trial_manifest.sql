-- trial_manifest (v1.0)
--
-- log-mysql 인스턴스의 operation_state_db에 적용한다. operation_log와 같은 데이터베이스여야
-- 한 번의 질의로 조인할 수 있다.
--   kubectl -n ailab-infra exec -it log-mysql-0 -- \
--     mysql -u root -p operation_state_db < infra-sql/trial_manifest.sql
--
-- 이 파일은 harness/trial.py(Trial Runner)의 INSERT 컬럼 목록과 1:1로 맞춰져 있다.
-- 컬럼을 추가하거나 이름을 바꾸면 두 곳을 함께 고쳐야 한다. 쓰는 쪽은 대상 시스템이 아니라
-- 하네스다. 계측 지점이 비교군마다 달라지면 지표가 오염되므로 대상 시스템 바깥에서 쓴다.
--
-- 작업 식별자를 새로 만들지 않는다. operation_log의 job_id와 attempt가 그 역할을 이미 한다.
-- 다만 baseline은 동기 경로여서 job_id가 NULL이므로, baseline의 작업 경계는 job_id가 아니라
-- trial의 시간창(started_at ~ ended_at)으로만 정해진다. 이것이 baseline 쪽 분해능의 한계다.
--
-- 조인 규칙: 어떤 operation_log 행이 어느 trial에 속하는지는 request_id가 같고
-- created_at이 started_at 이상이며 ended_at 이하일 때로 정한다. ended_at이 NULL이면
-- 현재 시각을 상한으로 본다. 한 신청에 대해 생성 trial과 회수 trial을 따로 돌려도
-- 두 시간창이 겹치지 않으므로 구분된다.
--
-- 외래 키는 걸지 않는다. operation_log는 append-only 저널이고 trial_manifest는 그보다
-- 먼저 쓰이므로, 제약을 걸면 신청 번호가 정해지기 전에 행을 넣을 수 없다.

CREATE TABLE IF NOT EXISTS trial_manifest (
  trial_id     VARCHAR(64) PRIMARY KEY,  -- 평가 실험 1회
  scenario_id  VARCHAR(16),              -- C06 같은 장애 시나리오. 장애를 주입하지 않는 대조 시행이면 NULL
  method       VARCHAR(16) NOT NULL,     -- baseline/noprobe/full
  server_group VARCHAR(8)  NOT NULL,     -- A 또는 B
  operation    VARCHAR(16) NOT NULL,     -- CREATE 또는 REVOKE
  request_id   VARCHAR(64),              -- 신청이 만들어지기 전에는 NULL, 만들어지면 채움
  horizon_sec  INT         NOT NULL,     -- 관측 구간 H
  repetition   SMALLINT    NOT NULL,     -- 같은 조건의 몇 번째 반복인가
  revisions    JSON        NOT NULL,     -- 구성요소별 커밋 sha
  started_at   DATETIME(3) NOT NULL,
  ended_at     DATETIME(3),              -- 끝나기 전에는 NULL
  INDEX idx_req (request_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
