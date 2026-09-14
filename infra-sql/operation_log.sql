-- operation_log (v1.0)
--
-- log-mysql 인스턴스의 operation_state_db에 적용한다.
--   kubectl -n ailab-infra exec -it log-mysql-0 -- \
--     mysql -u root -p operation_state_db < infra-sql/operation_log.sql
--
-- 이 파일은 config-server/operation_log.py의 INSERT 컬럼 목록과 1:1로 맞춰져 있다.
-- 컬럼을 추가하거나 이름을 바꾸면 두 곳을 함께 고쳐야 한다.
--
-- 저장 항목은 승인 1건을 단위로 한다. request_id에는 admin_be의 신청 PK가 그대로 들어가며,
-- config-server가 자체 생성하지 않는다. 이 값이 있어야 계정 생성·Pod 생성·회수가 하나의
-- 승인 아래로 묶여 단계별 소요시간과 수렴 시간을 산출할 수 있다.

CREATE TABLE IF NOT EXISTS operation_log (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  job_id        BIGINT,                 -- 작업 번호(v2.0): 작업 시작 행의 id. 제어기가 실행한 작업의 모든 행에 붙음. 동기 경로는 NULL
  request_id    VARCHAR(64) NOT NULL,   -- 승인 번호. admin_be가 보내는 신청 PK를 그대로 사용
  username      VARCHAR(64) NOT NULL,
  pod_name      VARCHAR(255),           -- 알기 전엔 NULL, Pod 이름이 정해지면 채움
  node_name     VARCHAR(64),            -- 마찬가지로 노드가 정해지면 채움
  resource_type VARCHAR(32),            -- account/kerberos/storage/pod/service/nodeport
  action        VARCHAR(64) NOT NULL,   -- operation_log.py의 Action Enum 값
  phase         VARCHAR(16) NOT NULL,   -- START/SUCCESS/FAIL/RETRY (UNKNOWN은 v2.0에서 추가)
  attempt       SMALLINT DEFAULT 1,
  duration_ms   INT,                    -- SUCCESS/FAIL 행에만 채움: 같은 (request_id, action, attempt)의 START로부터 걸린 시간
  error_code    VARCHAR(64),
  error_detail  TEXT,
  target_state  JSON,                   -- 목표 상태(v2.0): 작업 시작 행에만. 비밀번호 해시는 넣지 않음
  created_at    DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  INDEX idx_req (request_id, created_at),
  INDEX idx_action_phase (action, phase),
  INDEX idx_job (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;  -- 운영 log-mysql과 같고 job_id·target_state만 추가

-- v2.0 이전에 만든 표에는 job_id·target_state가 없다. 설치 스크립트가 배포 때마다 이 파일을 다시
-- 적용하므로, 열이 없을 때만 추가한다(MySQL 8.0은 ADD COLUMN IF NOT EXISTS가 없다).
SET @has := (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'operation_log' AND COLUMN_NAME = 'job_id');
SET @ddl := IF(@has = 0, 'ALTER TABLE operation_log ADD COLUMN job_id BIGINT AFTER id, ADD INDEX idx_job (job_id)', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SET @has := (SELECT COUNT(*) FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'operation_log' AND COLUMN_NAME = 'target_state');
SET @ddl := IF(@has = 0, 'ALTER TABLE operation_log ADD COLUMN target_state JSON AFTER error_detail', 'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 작업 소유권(lease)과 진행 기록 (v2.1). operation_log는 append-only 저널이라 가변 상태
-- (누가 잡고 있나, 어느 단계까지 했나)는 여기 둔다. 행은 작업당 하나, 작업이 끝나면 지운다.
CREATE TABLE IF NOT EXISTS job_control (
  job_id      BIGINT PRIMARY KEY,      -- operation_log 작업 시작 행의 id
  request_id  VARCHAR(64)  NOT NULL,
  action      VARCHAR(64)  NOT NULL,   -- PROVISION / REVOKE
  owner       VARCHAR(128) NOT NULL,   -- 제어기 프로세스 식별자 (host-pid-난수)
  lease_until DOUBLE       NOT NULL,   -- epoch 초. 지나면 다른 제어기가 인수한다
  done_steps  TEXT,                    -- 끝난 단계 이름 JSON 배열
  saved_ctx   TEXT,                    -- 이어하기 컨텍스트 JSON (pod_name·uid·포트 등)
  updated_at  DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
