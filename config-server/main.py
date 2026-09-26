from flask import Flask, request, jsonify, Blueprint
import fcntl
import re
import time
from typing import List, Optional
from kubernetes import client, config as k8s_config, watch
import pymysql
import os
import requests
import urllib3
import logging, sys

from flasgger import Swagger

from dotenv import load_dotenv
load_dotenv()

import base64
import hmac
import json
import subprocess
from datetime import datetime

from error import infra_error, k8s_error_fields
from request_models import (is_valid_unix_name, validate_body, check_values, swagger_definitions, ProvisionRequest, RevokeRequest,
                            DeletePodRequest, MigrateRequest, AddGroupRequest, AddUserGroupsRequest,
                            ChangePasswordRequest, SHA512_CRYPT_RE)
from adapters.pod_status import (
    set_pod_creation_status, get_pod_creation_status,
    save_job_input, load_job_input, mark_job_running, mark_job_done, delete_job_input,
    save_job_result, load_job_result,
)
from adapters.operation_log import (Action, Phase, log_operation, current_job_id,
                                    current_attempt, write_failure_count)

from adapters import job_control
from adapters.job_control import LeaseLost
from lifecycle_steps import verify

from utils import (
    get_db_connection, get_log_db_connection, is_pod_ready, get_pod_failure_reason, get_pod_progress_stage, summarize_pod_start_events,
    get_existing_pod, generate_pod_name, delete_pod_util,
    LockedFile, ledger_lock, get_node_gpu_score,
    read_issued_id_max, record_issued_id,
    ensure_etc_layout, ensure_sudoers_file,
    read_passwd_lines, write_passwd_lines,
    read_group_lines, write_group_lines,
    read_shadow_lines, write_shadow_lines,
    parse_passwd_line, format_passwd_entry,
    parse_group_line, format_group_entry,
    parse_shadow_line, format_shadow_entry,
    create_user_home_directory,
    user_home_owner_uid,
    other_homes_owned_by,
    delete_user_home_directory,
    HomeOwnerMismatch,
    create_team_directory,
    TeamDirGroupMismatch,
    sync_running_pod_groups,
    remove_running_pod_groups,
    sync_running_pod_password,
    update_account_secrets,
    select_best_node_from_prometheus,
    resolve_k8s_node_name,
    resolve_farm_home_mount_root,
    load_user_image,
    commit_and_save_user_image,
    create_nodeport_services,
    delete_nodeport_services,
)

app = Flask(__name__)

# 로그 설정 — Flask가 app.logger에 붙여 둔 기본 출력(stderr)을 떼고 하나만 쓴다. 둘 다 두면 모든 로그가 두 줄씩 찍힌다.
from flask.logging import default_handler  # noqa: E402
app.logger.removeHandler(default_handler)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
handler.setFormatter(formatter)
app.logger.addHandler(handler)
app.logger.setLevel(logging.DEBUG)


# ---- Global app configuration ----
BASE_ETC_DIR = "/kube_share"

# 같은 코드를 다른 네임스페이스에 한 벌 더 띄울 때(제안 시스템 실험 스택 등) 운영과 겹치면 안
# 되는 값들. 기본값은 운영의 현재 동작과 같다.
# admin_be 주소 — Pod 생성 시 사용자 설정 조회(WAS)와 내부 알림 중계가 모두 여기로 간다.
ADMIN_BE_INTERNAL_URL = os.getenv("ADMIN_BE_INTERNAL_URL", "http://admin-prod.default").rstrip("/")
# UID/GID 대역 — AD uidNumber와 NAS 홈 소유권은 스택이 달라도 공유되므로 대역이 겹치면 안 된다.
UID_MIN = int(os.getenv("UID_MIN", "20000"))
UID_MAX = int(os.getenv("UID_MAX", "0")) or None
# 공용(팀) 그룹 gid 대역 — 개인 그룹은 gid=uid라 uid 대역을 쓴다. 두 할당기가 같은 대역을 나눠
# 쓰면 공용 그룹이 다음 uid 번호를 선점해 그 계정의 primary 그룹이 남의 팀 그룹이 된다(#148).
# 대역을 떼면 충돌이 구조적으로 생기지 않는다. AD·NAS를 스택끼리 공유하므로 여기도 스택별로 나눈다.
SHARED_GID_MIN = int(os.getenv("SHARED_GID_MIN", "70000"))
SHARED_GID_MAX = int(os.getenv("SHARED_GID_MAX", "0")) or None
# 사용자 Pod NodePort 대역 — 클러스터 전체에서 공유되므로 스택끼리 같은 순간 같은 포트를 고르지 않게 나눈다.
NODEPORT_MIN = int(os.getenv("NODEPORT_MIN", "30000"))
NODEPORT_MAX = int(os.getenv("NODEPORT_MAX", "32767"))
# 실행 방식(baseline | noprobe | full). 세 방식은 같은 제어기·같은 단계 함수를 쓰고 이 값 하나로만 갈린다.
# baseline: 재시도·결과 확인·이어하기 없이 운영과 같은 뒷정리 후 종료 / noprobe: 복구 기능 /
# full: noprobe + 실제 접근 시험. 이름은 RUN_MODE 가 정본이고 VERIFY_MODE 는 기존 배포 호환용 별칭이다.
# 허용 밖의 값(대문자 FULL, 오타)이 조용히 다른 방식으로 돌면 비교 결과가 틀어지므로 기동 시점에 즉시 죽는다.
_ALLOWED_RUN_MODES = ("baseline", "noprobe", "full")


def _resolve_run_mode(env=None):
    env = os.environ if env is None else env
    mode = env.get("RUN_MODE") or env.get("VERIFY_MODE") or "noprobe"
    if mode not in _ALLOWED_RUN_MODES:
        raise SystemExit(f"RUN_MODE must be one of {_ALLOWED_RUN_MODES}, got {mode!r}")
    return mode


VERIFY_MODE = _resolve_run_mode()
# 실험 스택은 AD·Kerberos, NAS 홈, farm keytab을 운영과 같이 쓰고 이것들은 이름으로 식별된다.
# 접두어를 주면 그 접두어로 시작하지 않는 이름은 받지 않아, 운영 계정을 덮어쓰거나 지우지 못하게 한다.
# 비워 두면(운영) 제한 없음.
ACCOUNT_PREFIX = os.getenv("ACCOUNT_PREFIX", "")


# 내부 API 토큰. config-server에는 사용자 인증이 없으므로 admin_be만 부르도록 공유 토큰을 요구한다.
# 비어 있으면 검사하지 않는다(로컬 개발·테스트). 상태 확인과 진행 상황 조회(화면이 nginx를 거쳐 GET으로 부름),
# API 문서만 토큰 없이 연다.
API_TOKEN = os.getenv("CONFIG_API_TOKEN", "")
_TOKEN_FREE_GET = re.compile(r"^/(health|requests/[^/]+/status|apispec_1\.json|apidocs/.*|flasgger_static/.*)$")


def admin_be_headers():
    """admin_be의 내부 전용 API(/api/requests/config/**, /api/internal/**)도 같은 토큰으로 호출자를 확인한다."""
    return {"X-Internal-Token": API_TOKEN} if API_TOKEN else {}


@app.before_request
def _require_api_token():
    if not API_TOKEN:
        return None
    if request.method == "GET" and _TOKEN_FREE_GET.match(request.path):
        return None
    # 헤더 값과 토큰을 바이트로 맞춰 비교한다. 문자열끼리 비교하면 헤더에 비ASCII 문자가 섞였을 때
    # 예외가 나서 401 대신 500이 나갔다 — 바깥에서 쉽게 유발할 수 있는 경로다.
    sent = request.headers.get("X-Internal-Token", "").encode("utf-8", "surrogateescape")
    if hmac.compare_digest(sent, API_TOKEN.encode("utf-8")):
        return None
    app.logger.warning(f"[AUTH] 내부 API 토큰 없음 또는 불일치: {request.method} {request.path}")
    return jsonify(infra_error("AUTHENTICATE", "UNAUTHORIZED", "내부 API 토큰이 없거나 틀립니다")), 401


@app.before_request
def _enforce_account_prefix():
    if not ACCOUNT_PREFIX or request.method not in ("POST", "PUT", "DELETE"):
        return None
    names = []
    if request.view_args and "username" in request.view_args:
        names.append(request.view_args["username"])
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        if request.path in ("/operations/migrate", "/operations/provision", "/operations/revoke"):
            names.append(body.get("username"))
        if request.path == "/operations/revoke" and str(body.get("pod_name") or "").startswith("ailab-"):
            names.append(_pod_username(body["pod_name"]))
    bad = [n for n in names if n is not None and not str(n).startswith(ACCOUNT_PREFIX)]
    if bad:
        app.logger.warning(f"[PREFIX] 접두어 없는 계정 거절: {bad} ({request.method} {request.path})")
        return jsonify(infra_error("CHECK_ACCOUNT_PREFIX", "ACCOUNT_PREFIX_MISMATCH",
                                   f"이 환경은 '{ACCOUNT_PREFIX}'로 시작하는 계정만 다룹니다")), 403
    return None
app.config.from_mapping({
    # Namespace
    # 사용자 Pod·Service·keytab 시크릿을 만들고 지우는 네임스페이스. 차트가 config.namespace를 넣어 준다.
    "NAMESPACE": os.getenv("NAMESPACE", "ailab-infra"),

    # External endpoints & timeouts
    "PROM_URL": "http://monitoring-kube-prometheus-prometheus.monitoring:9090",
    "WAS_URL_TEMPLATE": ADMIN_BE_INTERNAL_URL + "/api/requests/config/{username}",
    "HTTP_TIMEOUT_SEC": 3.0,
    # 타임아웃 체인은 안쪽 레이어가 바깥쪽보다 항상 짧아야 한다 (그래야 바깥쪽이
    # 포기하기 전에 안쪽이 먼저 정상적으로 응답을 만들 기회를 가진다):
    #   config-server(여기, 500s) < admin_be podWebClient(550s)
    #     < nginx/ingress-nginx(570s) < 프론트(600s, "10분"으로 표시)
    # 여기서 max_wait를 다 채운 뒤에도 실패 정리(Pod 삭제/nodeport 해제/krb5 정리)
    # 시간이 추가로 필요해서, admin_be와의 버퍼(50s)를 남겨둔다.
    "POD_READY_MAX_WAIT_SEC": 500,

    # Default resources
    "DEFAULT_CPU_REQUEST": "1000m",
    "DEFAULT_MEM_REQUEST": "1024Mi",
    "DEFAULT_CPU_LIMIT":  "1000m",
    "DEFAULT_MEM_LIMIT":  "1024Mi",
    # ephemeral-storage request가 없으면(예전 상태) kubelet이 노드 디스크 압박 시 그 Pod의
    # 실제 사용량과 무관하게 "request 대비 초과 사용"으로 잡아 무조건 축출 1순위로 삼는다 —
    # 노드가 꽉 찬 진짜 원인(오래된 이미지 등)과 무관한 Pod가 대신 죽는 문제가 있었다.
    # request를 걸어두면 그만큼은 이 Pod 몫으로 확보되고, 축출 판단도 실제 사용량 기준으로
    # 공평해진다. limit은 한 Pod가 노드 디스크를 독점하지 못하게 막는 안전장치다.
    "DEFAULT_EPHEMERAL_STORAGE_REQUEST": "5Gi",
    "DEFAULT_EPHEMERAL_STORAGE_LIMIT": "50Gi",
    # 사용자 컨테이너에 붙일 우선순위 등급. 노드 디스크가 쪼들릴 때 축출 순서를 뒤로 미룬다.
    # 설치 스크립트가 같은 이름으로 만든다. 등급이 없는 클러스터에서는 비워 두면 붙이지 않는다.
    "POD_PRIORITY_CLASS": os.getenv("POD_PRIORITY_CLASS", "ailab-user-workload"),

    # NFS
    "NFS_USER_SHARE_PATH": os.getenv("NFS_USER_SHARE_PATH", "/volume1/share/user"),

    # Kerberos (비어있으면 비활성)
    "KRB5_REALM":           os.getenv("KRB5_REALM", ""),

    # farm 노드 keytab/timer 자동 배포용 SSH (전용 서비스 계정)
    "FARM_SSH_USER":     os.getenv("FARM_SSH_USER", ""),
    "FARM_SSH_KEY_PATH": os.getenv("FARM_SSH_KEY_PATH", ""),
    "FARM_NODES":        json.loads(os.getenv("FARM_NODES_JSON", "[]")),

    # AD 계정 생성/삭제용 SSH (전용 서비스 계정, 별도 DC 노드 세트에만 배포됨)
    "FARM_AD_SSH_USER":     os.getenv("FARM_AD_SSH_USER", ""),
    "FARM_AD_SSH_KEY_PATH": os.getenv("FARM_AD_SSH_KEY_PATH", ""),
    "FARM_AD_DC_NODES":     json.loads(os.getenv("FARM_AD_DC_NODES_JSON", "[]")),

    # GPU 유실 점검(check_gpu_pods.py)이 admin_be 내부 API(/api/internal/slack/notify)로
    # 보낼 때 지정할 Slack webhook URL. 비어있으면 알림을 스킵하고 로그만 남긴다.
    "INFRA_SLACK_WEBHOOK_URL": os.getenv("INFRA_SLACK_WEBHOOK_URL", ""),
    "ADMIN_BE_INTERNAL_URL":   ADMIN_BE_INTERNAL_URL,

    # image store
    "IMAGE_STORE_DIR": "/image-store/images",

    "NVIDIA_AUX_DEVICES": [
        "nvidiactl", "nvidia-uvm", "nvidia-uvm-tools", "nvidia-modeset"
    ],
    "BASE_ETC_DIR": BASE_ETC_DIR,
    "SUDO_ALLOWED_COMMANDS": [
        cmd.strip() for cmd in os.getenv("SUDO_ALLOWED_COMMANDS", "").split(",") if cmd.strip()
    ],
    "PASSWD_PATH": BASE_ETC_DIR + "/passwd",
    "GROUP_PATH": BASE_ETC_DIR + "/group",
    "SHADOW_PATH": BASE_ETC_DIR + "/shadow",
    "SUDOERS_DIR": BASE_ETC_DIR + "/sudoers.d",
    "BASH_LOGOUT_PATH": BASE_ETC_DIR + "/bash.bash_logout",
    "BASHRC_PATH": BASE_ETC_DIR + "/bashrc",
    "ISSUED_ID_MAX_PATH": BASE_ETC_DIR + "/issued_id_max.json",
})

@app.route("/health", methods=["GET"])
def health():
    """
    서버 상태 확인 API

    ---
    tags:
    - System

    summary: 서버 상태 확인

    responses:

      200:
        description: 서버 정상. run_mode 는 실행 중인 조건.
        schema:
          type: object
          example: {"status": "OK", "run_mode": "noprobe"}
    """
    # 이 응답은 준비 상태 점검(5초)이 읽는다. 그래서 이 프로세스가 요청을 받을 수 있는지만 답하고
    # 바깥 자원은 건드리지 않는다. 예전에는 작업 이력 기록 실패 건수를 Redis에서 함께 읽었는데,
    # Redis가 사라지자 그 조회가 17초씩 걸려 준비 상태 점검이 계속 실패했고, 설치가 대기 한도
    # 10분을 채우고 중단됐다. 부가 정보는 /health/details 로 옮겼다.
    return jsonify(status="OK", run_mode=VERIFY_MODE), 200


@app.route("/health/details", methods=["GET"])
def health_details():
    """
    서버 상태 + 운영 참고 값

    ---
    tags:
    - System

    summary: 서버 상태와 작업 이력 기록 실패 누적 건수

    responses:

      200:
        description: oplog_write_failures 는 작업 이력 기록 실패 누적 건수
          (조회 불능이면 null — 0 과 구분).
        schema:
          type: object
          example: {"status": "OK", "run_mode": "noprobe", "oplog_write_failures": 0}
    """
    # 부가 저장소를 조회하므로 저장소가 멈추면 이 경로도 함께 느려진다. 준비 상태 점검에는 쓰지 않는다.
    return jsonify(status="OK", run_mode=VERIFY_MODE,
                   oplog_write_failures=write_failure_count()), 200

def load_k8s():
    try:
        k8s_config.load_incluster_config()
    except:
        k8s_config.load_kube_config()


def wait_for_pod_deleted(v1, pod_name, namespace, timeout_sec=60):
    """
    delete_namespaced_pod() 이후 실제 파드 삭제 완료를 watch 이벤트로 확인한다.
    """
    w = watch.Watch()
    field_selector = f"metadata.name={pod_name}"
    try:
        for event in w.stream(
            v1.list_namespaced_pod,
            namespace=namespace,
            field_selector=field_selector,
            timeout_seconds=timeout_sec,
        ):
            if event.get("type") == "DELETED":
                return True
        return False
    finally:
        w.stop()


class PodSpecBuildError(Exception):
    def __init__(self, message, progress=None):
        super().__init__(message)
        self.progress = progress or {}

# ////////////////////// 단계 실행 (v2.0) //////////////////////
# 생성·회수 흐름을 단계 함수로 나눈다. 제어기(controller.py)가 작업마다 단계 함수를 차례로 부른다.
# 남아 있는 동기 경로(/delete-pod)도 같은 단계 함수를 요청 안에서 끝까지 실행한다.

class StepFailed(Exception):
    """단계가 실패로 끝남. body·status는 동기 엔드포인트가 그대로 돌려주는 응답이다."""

    def __init__(self, body, status, cause=None, retry=True, restart_from=None):
        super().__init__(body.get("error") if isinstance(body, dict) else str(body))
        self.body = body
        self.status = status
        # 요청은 나갔지만 응답을 못 받아 실제로 실행됐는지 알 수 없는 실패인지(UNKNOWN)
        self.unknown = _is_unknown_result(cause)
        # 다시 해 봐야 같은 결과인 실패. 단계가 자기 자원을 이미 정리했다면 재시도는 그 자원을
        # 못 찾아 실패하고, 그 오류가 처음 원인을 덮어쓴다.
        self.retry = retry
        # 단계가 앞 단계의 자원까지 정리하고 실패했을 때, 다시 시작해야 할 단계 이름.
        # 같은 단계만 다시 돌리면 정리된 자원(계정·포트 배정)을 전제로 실행돼 기록이 어긋난다.
        self.restart_from = restart_from


def _is_unknown_result(e) -> bool:
    """요청은 나갔는데 응답을 못 받은 경우(read timeout, 원격 명령 timeout). 실제로 실행됐는지
    알 수 없으므로 FAIL과 구분해 UNKNOWN으로 기록한다. 연결 자체가 안 된 경우(connect timeout,
    연결 거부)는 요청이 나가지 않았으므로 FAIL이다. 감싼 예외는 __cause__를 따라가 확인한다."""
    depth = 0
    while e is not None and depth < 10:
        if isinstance(e, (requests.exceptions.ReadTimeout, subprocess.TimeoutExpired,
                          urllib3.exceptions.ReadTimeoutError)):
            return True
        e = e.reason if isinstance(e, urllib3.exceptions.MaxRetryError) else e.__cause__
        depth += 1
    return False


def _fail_phase(e):
    return Phase.UNKNOWN if _is_unknown_result(e) else Phase.FAIL


# ////////////////////// 포트 할당 //////////////////////

# reconcile 쓰로틀: 마지막 실행 시각(Unix timestamp). 앱 시작 시 0으로 초기화.
_last_reconcile_ts: float = 0.0
# 최소 실행 간격(초). allocate_nodeports() 호출 시 in-flight pod 오탐을 방지하기 위해
# 5분 간격으로 제한한다. (allocate -> Service 생성까지 통상 30초 이내이므로 충분한 여유)
_RECONCILE_INTERVAL_SEC: int = 300



    


# ---------- Pod 생성 단계 (v2.0) ----------
# 순서: 사용자 설정 조회 → Pod 이름·후보 노드 → 노드 선택 → Pod spec(NodePort 할당·keytab 배포)
#       → k8s Pod 생성 → Ready 대기 → Service 생성



@app.route("/requests/<request_id>/status", methods=["GET"])
def get_pod_status(request_id):
    """
    신청(request) 단위 Pod 생성 진행 상황 조회

    생성 작업은 이미지 pull 등으로 오래(최대 POD_READY_MAX_WAIT_SEC초) 걸릴 수 있어, 작업이 끝나기 전에
    진행 상황만 가볍게 조회하기 위한 엔드포인트. 한 사용자가 Pod를 여러 개 동시에 생성할 수 있어
    username이 아니라 request_id로 조회한다(생성 작업을 등록할 때 넘긴 request_id와 같은 값).

    stage는 다음 순서로 진행되며, 최종 상태는 ready 또는 failed다:
      - unknown            : 생성 이력 없음 (생성 작업을 등록한 적 없음)
      - started             : 요청 접수
      - selecting_node      : GPU 노드 선택 중 (Prometheus 스코어링)
      - building_pod_spec   : pod spec 생성 시작 (바로 아래 두 단계로 넘어가는 과도 상태)
      - allocating_nodeport : NodePort 할당 중
      - deploying_krb5      : farm 노드에 krb5 keytab 배포 중 (KRB5_REALM 설정 시에만 거침)
      - creating_pod        : k8s에 pod 생성 요청 중
      - pulling_image       : 이미지 다운로드 중 (보통 가장 오래 걸리는 단계. 진입 시 기본값이며,
                                    k8s 이벤트를 아직 못 받았을 때도 이 상태로 노출된다)
      - starting_container  : 이미지 준비 완료, 컨테이너 생성/시작 중
      - mount_retrying      : 볼륨 마운트 재시도 중 (아직 최종 실패는 아님)
      - creating_services   : NodePort Service 생성 중
      - ready               : 컨테이너 생성 완료 (성공, 최종 상태)
      - failed              : 실패 (최종 상태. message에는 "krb5 배포 실패" 같은 카테고리만 담기며,
                                    보안상 상세 예외/k8s 에러 원문은 노출하지 않는다 — 상세 원인은 서버 로그 참조)

    ---
    tags:
    - Pod

    summary: Pod 생성 진행 상황 조회

    parameters:
      - in: path
        name: request_id
        required: true
        type: string

    responses:
      200:
        description: 진행 상황 조회 성공
        schema:
          type: object
          properties:
            request_id:
              type: string
            stage:
              type: string
              enum:
                - unknown
                - started
                - selecting_node
                - building_pod_spec
                - allocating_nodeport
                - deploying_krb5
                - creating_pod
                - pulling_image
                - starting_container
                - mount_retrying
                - creating_services
                - ready
                - failed
            message:
              type: string
              description: 사람이 읽는 짧은 요약. failed 상태여도 상세 예외/에러 원문은 담지 않음
            updated_at:
              type: string
              description: ISO8601 UTC (unknown일 때는 없음)
      500:
        description: 서버 내부 오류
    """
    try:
        status = get_pod_creation_status(request_id)
    except Exception as e:
        app.logger.exception("[POD STATUS] lookup failed")
        return jsonify(infra_error(
            "GET_POD_STATUS",
            "POD_STATUS_LOOKUP_FAILED",
            str(e),
        )), 500

    if status is None:
        return jsonify({"request_id": request_id, "stage": "unknown", "message": "생성 이력 없음"}), 200

    return jsonify({"request_id": request_id, **status}), 200



# //////////////////////// Pod 삭제 //////////////////////

# ---------- Pod 회수 단계 (v2.0) ----------
# 순서: NodePort Service 삭제 → NodePort 반환 → k8s Pod 삭제 → 그 노드의 keytab 정리



@app.route("/pods/<pod_name>", methods=["DELETE"])
def delete_pod(pod_name):
    """
    고아 Pod 삭제 API

    신청 기록이 없는 사용자 Pod를 지우고 딸린 자원을 정리한다. 신청이 있는 Pod는 회수 작업
    (POST /operations/revoke)으로 지운다.
    - NodePort Service
    - NodePort DB allocation
    - Kubernetes Pod
    ---
    tags:
    - Pod
    parameters:
      - {in: path, name: pod_name, required: true, type: string}
      - {in: query, name: request_id, required: false, type: string, description: 이 Pod를 만든 신청 번호}
    responses:
      200:
        description: 삭제됨(이미 없던 Pod는 already_absent)
      400:
        description: 잘못된 Pod 이름
      500:
        description: 삭제 실패
    """
    body, error = check_values(DeletePodRequest, {"pod_name": pod_name, "request_id": request.args.get("request_id")})
    if error is not None:
        return error
    pod_name = body.pod_name
    app.logger.info(f"[DELETE POD] request received - pod_name={pod_name}")
    rollback = _new_delete_rollback()

    try:
        username = _pod_username(pod_name)
        # admin_be는 이 Pod를 만든 신청 PK를 request_id로 보낸다. 대응하는 신청이 없는
        # 고아 Pod 정리처럼 값이 없는 호출은 이 삭제 호출 하나만을 묶는 임시 키로 기록한다.
        request_id = body.request_id or f"{username}-DELETE-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:-3]}"

        app.logger.info(f"[DELETE POD] parsed username={username}")
        ctx = {"request_id": request_id, "username": username, "pod_name": pod_name, "rollback": rollback}
        try:
            for step in POD_DELETE_STEPS:
                step(ctx)
        except StepFailed as e:
            return jsonify(e.body), e.status

        if ctx.get("already_absent"):
            return jsonify({
                "status": "deleted",
                "pod_name": pod_name,
                "already_absent": True,
                "progress": rollback,
            }), 200

        return jsonify({
            "status": "deleted",
            "pod_name": pod_name,
            "progress": rollback,
        }), 200

    except Exception as e:
        app.logger.exception("[DELETE POD] deletion failed")
        return jsonify(infra_error(
            "DELETE_POD",
            "DELETE_POD_FAILED",
            str(e),
            rollback=rollback,
            pod_name=pod_name,
        )), 500


def _pod_username(pod_name: str) -> str:
    """ailab-<username>-<suffix> 형식의 Pod 이름에서 username을 꺼낸다."""
    return pod_name[len("ailab-"):].rsplit("-", 1)[0]


# ---- SSH 호스트 키 ----
# farm·AD·NAS 접속은 배포 때 수집한 호스트 키(known_hosts, Secret)로 상대 서버를 확인한다. 확인하지 않으면
# 중간에 끼어든 서버가 keytab(deploy 입력)이나 계정 명령을 받아 갈 수 있다. 파일이 없는 환경(수집 전)은
# 예전처럼 확인하지 않고 경고만 남긴다.
SSH_KNOWN_HOSTS_FILE = os.getenv("SSH_KNOWN_HOSTS_FILE", "/etc/ssh-known-hosts/known_hosts")


def _known_hosts_available() -> bool:
    return os.path.isfile(SSH_KNOWN_HOSTS_FILE) and os.path.getsize(SSH_KNOWN_HOSTS_FILE) > 0


def _ssh_host_key_options() -> list:
    if _known_hosts_available():
        return ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS_FILE}"]
    app.logger.warning(f"[SSH] 호스트 키 파일이 없어 상대 서버를 확인하지 않음: {SSH_KNOWN_HOSTS_FILE}")
    return ["-o", "StrictHostKeyChecking=no"]


# ---- Kerberos AD helpers ----

# ssh 가 원격 명령이 아니라 자기 문제(연결·인증·호스트 키)로 끝낼 때 쓰는 코드.
# 그 외의 0 아닌 코드는 원격 명령이 돌려준 값이다.
SSH_TRANSPORT_ERROR = 255


def _farm_ad_ssh(remote_command: str, stdin_data: str = "", timeout: float = 30) -> str:
    """전용 서비스 계정으로 AD DC에 접속한다. forced-command가 걸려 있어 remote_command는
    그대로 실행되지 않고 원격 스크립트가 참고하는 값으로만 쓰인다.
    DC 하나가 실패하면 다음 DC로 넘어간다."""
    nodes = app.config["FARM_AD_DC_NODES"]
    if not nodes:
        raise RuntimeError("FARM_AD_DC_NODES가 설정되지 않음")
    last_error: Exception | None = None
    for node in nodes:
        cmd = ["ssh",
               "-i", app.config["FARM_AD_SSH_KEY_PATH"],
               *_ssh_host_key_options(),
               "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=10",
               "-p", str(node["port"]),
               f"{app.config['FARM_AD_SSH_USER']}@{node['host']}",
               remote_command]
        try:
            result = subprocess.run(cmd, input=stdin_data, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            last_error = e
            app.logger.warning(f"[FARM AD SSH] {node['name']} 타임아웃")
            continue
        if result.returncode == SSH_TRANSPORT_ERROR:
            # ssh 가 자기 문제(연결·인증·호스트 키)로 실패한 경우다. 이 DC 만의 사정이므로
            # 다음 DC 로 넘어간다.
            last_error = RuntimeError(f"AD DC 접속 실패 ({node['name']}): {result.stderr.strip()}")
            app.logger.warning(f"[FARM AD SSH] {node['name']} 접속 실패: {result.stderr.strip()}")
            continue
        if result.returncode != 0:
            # 원격 스크립트가 요청을 평가해서 거절했다. DC 들은 같은 samdb 를 복제하므로
            # 다른 DC 도 같은 판단을 한다 — 넘어가 봐야 왕복만 늘고, 마지막 DC 의 메시지가
            # 진짜 이유를 덮어쓴다(#146 에서 실측으로 드러났다). 즉시 실패시킨다.
            raise RuntimeError(f"AD DC 거절 ({node['name']}): {result.stderr.strip()}")
        return result.stdout
    raise last_error or RuntimeError("모든 AD DC 접속 실패")


def _create_krb5_principal_and_secret(username: str, uid: int, gid: int) -> None:
    keytab_b64 = _farm_ad_ssh(f"create {username} {uid} {gid}").strip()
    if not keytab_b64:
        raise RuntimeError(f"AD 계정 생성 결과 keytab이 비어 있음: {username}")
    load_k8s()  # 계정 CRUD 경로는 load_k8s()를 안 거쳐 k8s 기본값 localhost:80으로 붙음 → in-cluster config 보장
    v1 = client.CoreV1Api()
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=f"krb5-keytab-{username}",
            namespace=app.config["NAMESPACE"],
        ),
        data={"krb5.keytab": keytab_b64},
    )
    v1.create_namespaced_secret(namespace=app.config["NAMESPACE"], body=secret)

# DC 스크립트는 최대 45초 기다리고 마지막 조회에 15초를 더 쓸 수 있다. 그보다 먼저 끊으면 다음 DC 에서
# 같은 대기를 처음부터 다시 한다.
AD_REPLICATION_SSH_TIMEOUT_SEC = 75


def _await_ad_replicated(username: str, uid: int) -> None:
    """도메인의 모든 DC가 이 사용자를 uid로 돌려줄 때까지 기다린다. 읽기만 한다."""
    _farm_ad_ssh(f"await-replicated {username} {int(uid)}", timeout=AD_REPLICATION_SSH_TIMEOUT_SEC)


def _delete_krb5_principal_and_secret(username: str) -> None:
    try:
        _farm_ad_ssh(f"delete {username}")
    except Exception as e:
        app.logger.warning(f"[KRB5] AD 계정 삭제 실패 (무시): {e}")
    load_k8s()  # 계정 CRUD 경로 in-cluster config 보장 (create와 동일 구멍)
    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_secret(
            name=f"krb5-keytab-{username}",
            namespace=app.config["NAMESPACE"],
        )
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise


def _ad_enabled() -> bool:
    """AD 연동이 꺼진 환경(KRB5_REALM 미설정)에서는 그룹도 AD 에 올리지 않는다."""
    return bool(app.config.get("KRB5_REALM"))


def _create_ad_group(name: str, gid: int) -> None:
    """공용 그룹을 AD 에 만든다. 홈이 sec=krb5 라 그룹 권한은 NAS 가 AD 를 보고 판정하므로,
    AD 에 없는 그룹은 파일에만 있고 실제로는 없는 것과 같다(#146). 원격 스크립트가 멱등이라
    이미 있으면 gid 만 맞춘다."""
    if not _ad_enabled():
        return
    _farm_ad_ssh(f"group-create {name} {int(gid)}")


def _add_ad_group_member(groupname: str, username: str) -> None:
    """AD 그룹에 사용자를 넣는다. 이미 멤버면 아무 일도 하지 않는다.

    주의: 이것만으로는 이미 떠 있는 Pod 에 반영되지 않는다. NAS 는 GSS 컨텍스트를 맺을
    때 winbind 로 그룹을 풀어 auth.rpcsec.context 에 고정하고, 컨텍스트 수명(= 티켓 수명
    24h) 동안 다시 보지 않는다. 반영하려면 NAS 에서 그 캐시를 비워야 한다(#153). 티켓
    PAC 은 쓰이지 않으므로 클라이언트에서 kinit 을 다시 해도 소용없다 — 2026-09-22 실측."""
    if not _ad_enabled():
        return
    _farm_ad_ssh(f"group-addmember {groupname} {username}")


def _remove_ad_group_member(groupname: str, username: str) -> None:
    """AD 그룹에서 사용자를 뺀다. 이미 빠져 있으면 아무 일도 하지 않는다. 떠 있는 Pod 에 반영되는
    시점은 _add_ad_group_member 와 같다 — NAS 캐시를 비워야 한다."""
    if not _ad_enabled():
        return
    _farm_ad_ssh(f"group-removemember {groupname} {username}")


def _ensure_team_dir(name: str, gid: int) -> None:
    """팀 공유 디렉터리를 만든다(#154). NAS 가 AD 그룹으로 권한을 판정하므로 AD 연동이
    꺼진 환경에서는 만들어도 팀에게 열리지 않는다 — 그룹 동기화와 같은 조건으로 건너뛴다."""
    if not _ad_enabled():
        return
    create_team_directory(name, int(gid))


def _remove_group_line(name: str) -> None:
    """그룹 파일에서 한 줄을 지운다. AD 반영 실패 시 방금 쓴 줄을 되돌리는 용도."""
    with ledger_lock():
        write_group_lines([l for l in read_group_lines()
                           if (parse_group_line(l) or {}).get("name") != name])


def _set_group_membership(groupnames, username: str, member: bool) -> None:
    """group 파일에서 username의 멤버십을 더하거나 뺀다. AD 호출 동안 원장 잠금을 쥐지 않도록
    호출자가 앞서 읽은 내용을 쓰지 않고 잠금 안에서 다시 읽어, 그 사이 다른 Pod가 쓴 줄을 덮어쓰지 않는다."""
    names = set(groupnames)
    with ledger_lock():
        lines = read_group_lines()
        out, changed = [], False
        for gl in lines:
            rec = parse_group_line(gl)
            if rec and rec["name"] in names:
                current = rec.get("members", [])
                wanted = sorted(set(current) | {username}) if member else [m for m in current if m != username]
                if wanted != current:
                    rec["members"] = wanted
                    gl, changed = format_group_entry(rec), True
            out.append(gl)
        if changed:
            write_group_lines(out)


def _get_farm_node_info(node_name: str) -> dict:
    for node in app.config["FARM_NODES"]:
        if node["name"] == node_name:
            return node
    raise ValueError(f"unknown farm node: {node_name!r}")


FARM_SSH_TIMEOUT_SEC = float(os.getenv("FARM_SSH_TIMEOUT_SEC", "150"))


def _farm_ssh(host: str, port: str, remote_command: str, stdin_data: str = "") -> str:
    """전용 서비스 계정으로 접속한다. 계정 쪽에 forced-command가 걸려 있어
    remote_command는 그대로 실행되지 않고 원격 스크립트가 참고하는 값으로만 쓰인다.
    간헐적으로 최초 접속만 타임아웃되고 곧바로 재시도하면 성공하는 현상이 있었다 —
    이 farm 환경엔 Kerberos(KRB5_REALM)가 있어 OpenSSH 클라이언트가 기본적으로
    GSSAPI(Kerberos) 인증을 먼저 시도한 뒤 실패해야 키 인증으로 폴백하는데, 이 계정은
    처음부터 -i 키 인증만 쓰므로 그 GSSAPI 협상 자체가 불필요한 지연 요인이었다.
    명시적으로 꺼서 재시도에 의존하지 않고 매번 빠르게 붙도록 한다."""
    cmd = ["ssh",
           "-v",
           "-i", app.config["FARM_SSH_KEY_PATH"],
           *_ssh_host_key_options(),
           "-o", "BatchMode=yes",
           "-o", "GSSAPIAuthentication=no",
           "-o", "ConnectTimeout=10",
           "-p", str(port),
           f"{app.config['FARM_SSH_USER']}@{host}",
           remote_command]

    result = None
    last_error = None
    for attempt in range(2):
        app.logger.info(f"[FARM SSH] {host}:{port} 접속 시도 {attempt+1}/2")
        start = time.monotonic()
        try:
            # 원격 ailab-krb5-admin 스크립트는 systemd 호출을 최대 120초까지 기다린다(노드에
            # 로그인이 생기면 소리 서버가 블루투스를 기다리는 동안 systemd 본체가 최대 90초
            # 멈출 수 있다). 클라이언트가 그보다 먼저 끊으면 원격은 계속 일하는데 우리만
            # 재시도해 같은 대기를 두 번 겪는다. 원격 한도보다 넉넉한 150초로 둔다.
            result = subprocess.run(
                cmd, input=stdin_data, capture_output=True, text=True, timeout=FARM_SSH_TIMEOUT_SEC,
            )
            app.logger.info(f"[FARM SSH] {host}:{port} 접속 성공, {time.monotonic() - start:.1f}초 소요")
            break
        except subprocess.TimeoutExpired as e:
            last_error = e
            # -v로 캡처된 ssh 자체의 디버그 트레이스를 그대로 남긴다. 어느 단계(TCP 연결/
            # 배너 교환/키 교환/인증)에서 멈췄는지 이 로그만으로 바로 알 수 있어야,
            # 다음에 같은 증상이 재발했을 때 원인 후보를 추론이 아니라 로그로 확인할 수 있다.
            trace = (e.stderr or "").strip()
            app.logger.warning(
                f"[FARM SSH] {host}:{port} 타임아웃, 재시도 {attempt+1}/2, "
                f"{time.monotonic() - start:.1f}초 경과 시점 ssh 트레이스 마지막 부분:\n"
                f"{trace[-2000:]}"
            )
    if result is None:
        raise last_error

    if result.returncode != 0:
        raise RuntimeError(f"farm SSH 실패 ({host}:{port}): {result.stderr.strip()}")
    return result.stdout


# 새 계정(또는 삭제 뒤 다시 만든 계정)은 모든 DC에 들어간 뒤에도 노드·NAS에서 한동안 틀리게 보인다. 계정이
# 생기기 전의 조회 결과를 노드 winbind(300초)·노드 커널 idmap(600초)·NAS가 각각 캐시하고, 우리 쪽에서는
# 이 캐시들을 비울 수 없다(2026-09-26 실측: DC 복제 뒤 노드에서 본 홈 소유자가 맞기까지 약 6분). 그 사이
# 컨테이너가 뜨면 사용자가 자기 홈에 쓰지 못하므로, 노드에서 한 번씩 확인해 준비될 때까지 기다린다.
# 한도는 캐시 수명을 모두 넘기도록 잡는다. 확인은 읽기만 하므로 오래 기다려도 남는 것이 없다.
NODE_IDENTITY_WAIT_SEC = float(os.getenv("NODE_IDENTITY_WAIT_SEC", "900"))
NODE_IDENTITY_POLL_SEC = float(os.getenv("NODE_IDENTITY_POLL_SEC", "10"))


class NodeIdentityTimeout(RuntimeError):
    """노드가 한도 안에 사용자와 홈 소유자를 기대한 uid로 보지 못함."""


def _wait_node_identity(node: dict, username: str, uid: int) -> None:
    deadline = time.monotonic() + NODE_IDENTITY_WAIT_SEC
    last = None
    while True:
        out = _farm_ssh(node["host"], node["port"], f"check-identity {username} {int(uid)}")
        state = next((line.strip() for line in out.splitlines() if line.startswith("identity_state=")), "")
        if state == "identity_state=ready":
            return
        if not state:
            raise RuntimeError(f"check-identity 응답을 해석하지 못함: {out.strip()[-200:]!r}")
        if state != last:
            app.logger.info(f"[KRB5] 노드 신원 대기 {username} → {node['name']}: {state}")
            last = state
        if time.monotonic() >= deadline:
            raise NodeIdentityTimeout(
                f"{node['name']}에서 {username}(uid {uid})이 {int(NODE_IDENTITY_WAIT_SEC)}초 안에 준비되지 않음: {state}")
        time.sleep(NODE_IDENTITY_POLL_SEC)


def _deploy_krb5_to_farm(username: str, uid: int, node_name: str) -> None:
    """k8s Secret에서 keytab을 꺼내 원격 관리 스크립트의 deploy 액션으로 전달한다.
    keytab/env 작성, timer 기동, TGT 발급 확인까지 전부 원격에서 끝난다."""
    node = _get_farm_node_info(node_name)

    v1 = client.CoreV1Api()
    secret = v1.read_namespaced_secret(
        name=f"krb5-keytab-{username}",
        namespace=app.config["NAMESPACE"],
    )
    keytab_b64 = secret.data["krb5.keytab"]

    _wait_node_identity(node, username, uid)
    _farm_ssh(node["host"], node["port"], f"deploy {username} {uid}", stdin_data=keytab_b64)
    app.logger.info(f"[KRB5] farm 배포 완료 + TGT 확인됨: {username} → {node_name}")
    try:
        _clear_krb5_cleanup_pending(username, node_name)
    except Exception:
        # 배포 자체는 이미 성공했으니 실패로 처리하지 않는다 — 다만 예전 정리 예약이
        # 그대로 남아있을 수 있어서, 재조정 잡이 방금 배포한 keytab을 지울 위험이
        # 있다는 걸 명확히 남긴다. 절대 이 예약을 새로 다시 걸지는 않는다(성공한
        # 배포를 실패 경로로 되돌리는 꼴이 되므로).
        app.logger.exception(f"[KRB5] cleanup_pending 정리 실패(수동 확인 필요, 배포 자체는 성공): {username} ← {node_name}")


def _remove_krb5_from_farm(username: str, node_name: str) -> None:
    node = _get_farm_node_info(node_name)
    _farm_ssh(node["host"], node["port"], f"remove {username}")
    app.logger.info(f"[KRB5] farm 정리 완료: {username} ← {node_name}")


def _record_krb5_cleanup_pending(username: str, node_name: str) -> None:
    """farm 노드에서 keytab/timer 정리가 실패했을 때 재조정 잡이 나중에 재시도할 수 있도록 기록한다."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO krb5_cleanup_pending (username, node_name, failed_at)
                VALUES (%s, %s, NOW())
                ON DUPLICATE KEY UPDATE failed_at = NOW()
                """,
                (username, node_name),
            )
        conn.commit()
    except Exception:
        app.logger.exception(f"[KRB5] cleanup_pending 기록 실패: {username} ← {node_name}")
        conn.rollback()
    finally:
        conn.close()


def _clear_krb5_cleanup_pending(username: str, node_name: str) -> None:
    """_deploy_krb5_to_farm이 (username, node_name)에 대한 keytab 배포를 확인한 직후
    호출한다. 예전에 실패했던 시도가 이 정확한 (username, node_name) 조합에 남긴 '나중에
    삭제' 예약을 지워서, 재조정 잡이 방금 살려놓은 keytab을 뒤늦게 지워버리는 사고를 막는다.

    username만으로 지우면 이번에 안 건드린 다른 노드의 정당한 정리 예약까지 같이
    지워버리므로 반드시 node_name까지 조건에 건다 — krb5_cleanup_pending 자체가
    (username, node_name) 조합으로 예약을 구분하는 테이블이다.

    DB 실패는 조용히 삼키지 않고 그대로 올린다. 호출자가 "정리 예약이 그대로 남아있을
    수 있다"는 걸 알고, 그렇다고 방금 성공한 배포를 실패로 되돌리지는 않게 대응해야
    하기 때문이다."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM krb5_cleanup_pending WHERE username = %s AND node_name = %s",
                (username, node_name),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _remove_krb5_from_all_farms(username: str) -> None:
    for node in app.config["FARM_NODES"]:
        try:
            _remove_krb5_from_farm(username, node["name"])
        except Exception as e:
            app.logger.warning(f"[KRB5] farm 정리 실패, 재조정 잡에 위임: {node['name']} — {e}")
            _record_krb5_cleanup_pending(username, node["name"])


accounts_bp = Blueprint("accounts", __name__)

# ---------- 계정 생성 단계 (v2.0) ----------
# 순서: 계정(passwd·group·shadow·sudoers) → NAS 홈 → Kerberos principal



# ---------- 계정 회수 단계 (v2.0) ----------
# 순서: 계정(passwd·shadow·group) → NAS 홈 → Kerberos principal·keytab
# 제어기의 회수 작업은 보존 대상인 홈을 지우지 않으므로 홈 단계를 빼고 실행한다(논문 REVOKED 정의, 계획서 v2.0).



# 컨테이너 이미지가 이미 가진 그룹 이름. 이 이름으로 팀 그룹을 만들면 이미지 entrypoint 의
# ensure_supplemental_groups() 가 "이름은 있는데 gid 가 다르다"로 판단해 컨테이너를 죽인다
# (admin_infra_server container-images/entrypoint.sh:182-189). 사용자 컨테이너의 /etc/group 은
# 이미지 자신의 것이고 config-server 가 마운트하지 않으므로, group 파일 중복 검사로는 못 막는다.
#
# 출처: 운영 중인 dguailab/decs 이미지(cuda11.8-tf2.13-ubuntu22.04 의 260627·260915, 두 판 동일)의
# /etc/group 과 base_etc/group 시드의 합집합. 이미지를 바꾸면 아래로 다시 뽑아 갱신한다.
#   kubectl exec <사용자 Pod> -- cut -d: -f1 /etc/group
# 사용자 개인 그룹은 entrypoint 가 실행 중에 만드는 것이라 목록에 넣지 않는다.
# crontab·docker·input·kvm·render·ssh 는 지금 이미지에는 없지만 데비안·NVIDIA 계열 이미지에서
# 흔히 생기는 이름이라 미리 막는다(admin_infra-proposed#152).
RESERVED_GROUP_NAMES = frozenset("""
_ssh adm audio backup bin cdrom crontab daemon dialout dip disk docker fax floppy games gnats
input irc kmem kvm list lp mail man messagebus news nogroup nova operator plugdev polkitd proxy
render root sasl shadow src ssh ssl-cert staff sudo svmanager sys systemd-journal systemd-network
systemd-resolve systemd-timesync tape tty users utmp uucp video voice www-data
""".split())

# 이미지 그룹이 아니어도 계정명으로 쓰면 안 되는 이름: base_etc/passwd 시드의 시스템 계정과
# 노드·이미지 운영용 계정. 원장 시드 계정은 새로 만들 때 USER_ALREADY_EXISTS 로도 막히지만,
# admin_be 가 가입 단계에서 한 목록으로 거르도록 여기 함께 둔다.
RESERVED_USER_NAMES = frozenset("""
root daemon bin sys sync games man lp mail news uucp proxy www-data backup list irc _apt nobody
systemd-network systemd-timesync messagebus polkitd admin ubuntu ailab-krb5
""".split())

# 새 계정명으로 받지 않는 이름 전체. admin_be 는 GET /reserved-names 로 이 목록을 받아 가입 단계에서 쓴다.
RESERVED_ACCOUNT_NAMES = RESERVED_GROUP_NAMES | RESERVED_USER_NAMES


@accounts_bp.route("/reserved-names", methods=["GET"])
def get_reserved_names():
    """
    예약 이름 목록 API

    새 계정명으로 받지 않는 이름(이미지 그룹·시스템 계정·운영용 계정)을 돌려준다.
    admin_be 가 가입 단계에서 같은 목록으로 거르도록 목록의 원본을 여기 한 곳에 둔다.

    ---
    tags:
    - Accounts

    summary: 예약 이름 목록

    responses:

      200:
        description: 정렬된 예약 이름 목록
        schema:
          type: object
          example: {"names": ["_apt", "admin", "root"]}
    """
    return jsonify(names=sorted(RESERVED_ACCOUNT_NAMES)), 200


# ----------- Group management -----------
@accounts_bp.route("/groups", methods=["POST"])
@validate_body(AddGroupRequest)
def add_group(body: AddGroupRequest):
    """
    그룹 생성 API

    시스템에 새로운 Linux 그룹을 생성합니다.

    ---
    tags:
    - Accounts

    summary: 그룹 생성

    consumes:
    - application/json

    parameters:

      - in: body
        name: body
        required: true
        schema:
          $ref: '#/definitions/AddGroupRequest'

    responses:

      201:
        description: 그룹 생성 성공
        schema:
          type: object
          properties:
            group:
              type: object
              properties:
                name:
                  type: string
                  example: developers
                gid:
                  type: integer
                  example: 10001
        examples:
          application/json:
            group:
              name: developers
              gid: 10001
      400:
        description: 잘못된 요청
      409:
        description: 그룹 이미 존재
    """
    name, gid, members = body.name, body.gid, body.members

    # Validate that all members exist as users
    passwd_lines = read_passwd_lines()
    existing_users = {parse_passwd_line(l)["name"] for l in passwd_lines if parse_passwd_line(l)}
    # AD 에서 사용자와 그룹은 sAMAccountName 을 공유한다 — 같은 이름이면 나중에 계정 생성이
    # 실패하므로 여기서 막는다(#146).
    if name in existing_users:
        return jsonify(infra_error("ADD_GROUP", "GROUP_NAME_CONFLICTS_USER",
                                   f"group name collides with an existing user: {name}")), 400
    # 이미지가 이미 쥔 이름이면 Pod 가 기동하지 못한다 — 여기서 막지 않으면 원인이 그룹 이름이라는
    # 것을 기동 실패 로그에서 알아내야 한다(#152).
    if name in RESERVED_GROUP_NAMES:
        return jsonify(infra_error("ADD_GROUP", "GROUP_NAME_RESERVED",
                                   f"group name is reserved by the container image: {name}")), 409
    if members:
        invalid_members = [m for m in members if m not in existing_users]
        if invalid_members:
            return jsonify(infra_error("ADD_GROUP", "INVALID_GROUP_MEMBER",
                                       f"invalid members (users not found): {', '.join(invalid_members)}")), 400

    ensure_etc_layout()
    with ledger_lock(), LockedFile(app.config["GROUP_PATH"], "r+") as f:
        g_lines = f.read().splitlines()

        if any((parse_group_line(gl) or {}).get("name") == name for gl in g_lines):
            return jsonify(infra_error("ADD_GROUP", "GROUP_NAME_EXISTS", f"group already exists (name: {name})")), 409

        if gid is None:
            try:
                issued_max = read_issued_id_max("shared_gid")
            except Exception:
                app.logger.exception("[ACCOUNTS] gid 발급 기록을 읽지 못해 그룹 생성 중단: %s", name)
                return jsonify(infra_error("ADD_GROUP", "ISSUED_ID_RECORD_FAILED",
                                           "cannot read issued gid record")), 500
            gid = _allocate_next_gid(g_lines, min_gid=SHARED_GID_MIN, issued_max=issued_max)
            if SHARED_GID_MAX is not None and gid > SHARED_GID_MAX:
                return jsonify(infra_error("ADD_GROUP", "GID_RANGE_EXHAUSTED", f"gid range {SHARED_GID_MIN}~{SHARED_GID_MAX} exhausted")), 500
        elif gid < SHARED_GID_MIN or (SHARED_GID_MAX is not None and gid > SHARED_GID_MAX):
            # 호출자가 gid를 직접 주는 경로 — 개인 그룹 대역(=uid 대역)을 침범하면 여기서 막는다.
            return jsonify(infra_error("ADD_GROUP", "GID_OUT_OF_RANGE", f"gid {gid} outside shared gid range {SHARED_GID_MIN}~{SHARED_GID_MAX}")), 400
        elif any((parse_group_line(gl) or {}).get("gid") == gid for gl in g_lines):
            return jsonify(infra_error("ADD_GROUP", "GROUP_GID_EXISTS", f"group already exists (gid: {gid})")), 409

        # 직접 준 gid(옛 팀을 원래 번호로 되살리는 운영 경로)도 기록해, 자동 배정이 그 번호를 다시 주지 않게 한다.
        try:
            record_issued_id("shared_gid", gid)
        except Exception:
            app.logger.exception("[ACCOUNTS] gid 발급 기록 실패로 그룹 생성 중단: %s(%s)", name, gid)
            return jsonify(infra_error("ADD_GROUP", "ISSUED_ID_RECORD_FAILED",
                                       "cannot record issued gid")), 500
        new_group = {
            "name": name,
            "passwd": "x",
            "gid": gid,
            "members": sorted(members)
        }

        g_lines.append(format_group_entry(new_group))
        f.seek(0)
        f.write("\n".join(g_lines) + "\n")
        f.truncate()

    # AD 에 올려야 NAS 가 이 그룹을 인정한다(#146). 여기서 실패하면 파일에만 있고 실제로는
    # 안 먹는 그룹이 남으므로, 방금 쓴 줄을 되돌리고 실패로 답한다.
    try:
        _create_ad_group(name, gid)
        for m in sorted(members):
            _add_ad_group_member(name, m)
    except Exception as e:
        app.logger.exception("[ACCOUNTS] AD 그룹 생성 실패, group 파일 롤백: %s(%s)", name, gid)
        try:
            _remove_group_line(name)
        except Exception:
            app.logger.exception("[ACCOUNTS] 롤백까지 실패 — 수동 정리 필요: %s(%s)", name, gid)
        return jsonify(infra_error("ADD_GROUP", "AD_GROUP_CREATE_FAILED",
                                   f"failed to create group in AD: {name}")), 500

    # 팀 디렉터리가 없으면 그룹은 있어도 같이 쓸 자리가 없다. AD 그룹과 디렉터리 생성이 모두
    # 멱등이라 줄을 되돌려 두면 같은 요청을 다시 보내 이어서 끝낼 수 있다.
    try:
        _ensure_team_dir(name, gid)
    except Exception:
        app.logger.exception("[ACCOUNTS] 팀 디렉터리 생성 실패, group 파일 롤백: %s(%s)", name, gid)
        try:
            _remove_group_line(name)
        except Exception:
            app.logger.exception("[ACCOUNTS] 롤백까지 실패 — 수동 정리 필요: %s(%s)", name, gid)
        return jsonify(infra_error("ADD_GROUP", "TEAM_DIR_CREATE_FAILED",
                                   f"failed to create team directory: {name}")), 500

    return jsonify({"group": {"name": name, "gid": gid}}), 201

# ----------- Add user to supplementary groups -----------
@accounts_bp.route("/users/<username>/groups", methods=["POST"])
@validate_body(AddUserGroupsRequest)
def add_user_groups(username: str, body: AddUserGroupsRequest):
    """
    사용자 보조 그룹 추가 API

    특정 사용자를 하나 이상의 supplementary group에 추가합니다.

    ---
    tags:
    - Accounts

    summary: 사용자 그룹 추가

    parameters:

      - in: path
        name: username
        required: true
        type: string
        example: user2100
      - in: body
        name: body
        required: true
        schema:
          $ref: '#/definitions/AddUserGroupsRequest'

    responses:

      200:
        description: 그룹 추가 성공
      404:
        description: 사용자 또는 그룹 없음
      400:
        description: groups 필드 누락
    """
    groups = body.groups

    # Verify user exists and capture their name
    user_found = False
    for line in read_passwd_lines():
        rec = parse_passwd_line(line)
        if rec and rec["name"] == username:
            user_found = True
            break
    if not user_found:
        return jsonify(infra_error("ADD_USER_GROUPS", "USER_NOT_FOUND", f"user not found: {username}")), 404

    g_lines = read_group_lines()
    names = set(groups)

    # Ensure all requested groups existed
    existing_group_names = {parse_group_line(gl)["name"] for gl in g_lines if parse_group_line(gl)}
    missing = [g for g in groups if g not in existing_group_names]
    if missing:
        return jsonify(infra_error("ADD_USER_GROUPS", "GROUP_NOT_FOUND", f"groups not found: {', '.join(missing)}")), 404

    # AD 를 먼저 맞춘다 — 실패해도 group 파일이 더럽혀지지 않는다. 그룹·사용자 모두 이미
    # 존재해야 하는 경로라 여기서 만들 것은 없고 멤버십만 더한다(#146).
    try:
        for g in sorted(names):
            _add_ad_group_member(g, username)
    except Exception:
        app.logger.exception("[ACCOUNTS] AD 그룹 멤버 추가 실패: %s -> %s", username, sorted(names))
        return jsonify(infra_error("ADD_USER_GROUPS", "AD_GROUP_MEMBER_FAILED",
                                   f"failed to add {username} to groups in AD")), 500

    # 신청 승인은 신규·재사용 계정 모두 이 경로로 그룹을 더한다. 팀 디렉터리가 생기기 전에
    # 만든 그룹도 여기서 채워야 멤버가 같이 쓸 자리가 생긴다(#154). 멱등이라 매번 불러도 된다.
    gids = {r["name"]: r["gid"] for gl in g_lines if (r := parse_group_line(gl))}
    try:
        for g in sorted(names):
            _ensure_team_dir(g, gids[g])
    except TeamDirGroupMismatch as e:
        app.logger.error("[ACCOUNTS] 팀 디렉터리 gid 불일치: %s", e)
        return jsonify(infra_error("ADD_USER_GROUPS", "TEAM_DIR_GROUP_MISMATCH", str(e))), 409
    except Exception:
        app.logger.exception("[ACCOUNTS] 팀 디렉터리 생성 실패: %s", sorted(names))
        return jsonify(infra_error("ADD_USER_GROUPS", "TEAM_DIR_CREATE_FAILED",
                                   f"failed to create team directories: {', '.join(sorted(names))}")), 500

    _set_group_membership(names, username, member=True)

    # 이미 떠 있는 Pod 는 기동 때 구운 /etc/group 을 그대로 쓴다 — 여기서 채워야 재생성 없이
    # 새 세션부터 그룹이 보인다(admin_infra_server#25). 권한 원천(AD)은 이미 반영됐으므로
    # 실패해도 요청은 성공으로 둔다.
    pods = sync_running_pod_groups(username, {g: gids[g] for g in names})
    return jsonify({"status": "updated", "user": username, "groups": sorted(list(names)), "pods": pods})

# ----------- Remove user from a supplementary group -----------
@accounts_bp.route("/users/<username>/groups/<groupname>", methods=["DELETE"])
def remove_user_group(username: str, groupname: str):
    """
    사용자 보조 그룹 제거 API

    사용자를 공용 그룹에서 뺍니다. 이미 빠져 있어도 성공입니다. 팀 디렉터리와 그 안의 파일은
    건드리지 않습니다.

    ---
    tags:
    - Accounts

    summary: 사용자 그룹 제거

    parameters:

      - in: path
        name: username
        required: true
        type: string
        example: user2100
      - in: path
        name: groupname
        required: true
        type: string
        example: developers

    responses:

      200:
        description: 제거 성공(이미 빠져 있던 경우 포함)
      400:
        description: 이름 형식 오류
      404:
        description: 그룹 없음
      409:
        description: 사용자의 primary 그룹
      500:
        description: AD 반영 실패
    """
    # 두 이름 모두 AD DC 로 가는 SSH 명령 문자열에 그대로 들어간다(#146).
    for value in (username, groupname):
        if not is_valid_unix_name(value):
            return jsonify(infra_error("REMOVE_USER_GROUP", "INVALID_NAME", f"invalid name: {value}")), 400

    g_lines = read_group_lines()
    group = next((r for gl in g_lines if (r := parse_group_line(gl)) and r["name"] == groupname), None)
    if not group:
        return jsonify(infra_error("REMOVE_USER_GROUP", "GROUP_NOT_FOUND", f"group not found: {groupname}")), 404

    # 회수된 계정은 passwd 에 없을 수 있다 — 그때도 남은 멤버십을 치울 수 있게 거부하지 않는다.
    user = next((r for pl in read_passwd_lines() if (r := parse_passwd_line(pl)) and r["name"] == username), None)
    if user and user["gid"] == group["gid"]:
        return jsonify(infra_error("REMOVE_USER_GROUP", "PRIMARY_GROUP",
                                   f"{groupname} is the primary group of {username}")), 409

    # 추가와 같은 순서 — AD 가 실패하면 group 파일은 그대로 두어 재시도가 같은 상태에서 시작한다.
    try:
        _remove_ad_group_member(groupname, username)
    except Exception:
        app.logger.exception("[ACCOUNTS] AD 그룹 멤버 제거 실패: %s -> %s", username, groupname)
        return jsonify(infra_error("REMOVE_USER_GROUP", "AD_GROUP_MEMBER_FAILED",
                                   f"failed to remove {username} from {groupname} in AD")), 500

    _set_group_membership([groupname], username, member=False)

    pods = remove_running_pod_groups(username, [groupname])
    return jsonify({"status": "removed", "user": username, "group": groupname, "pods": pods})

# ----------- Change a user's login password -----------
@accounts_bp.route("/users/<username>/password", methods=["PUT"])
@validate_body(ChangePasswordRequest)
def change_user_password(username: str, body: ChangePasswordRequest):
    """
    사용자 로그인 비밀번호 교체 API

    계정 원장(shadow), 사용자의 모든 계정 Secret, 떠 있는 모든 Pod 의 /etc/shadow 를 새 해시로 바꾼다.
    멱등이라 일부가 실패하면 같은 요청을 다시 보내면 된다. Pod 가 하나도 없어도 성공이다.

    ---
    tags:
    - Accounts

    summary: 로그인 비밀번호 교체

    parameters:

      - in: path
        name: username
        required: true
        type: string
        example: user2100
      - in: body
        name: body
        required: true
        schema:
          $ref: '#/definitions/ChangePasswordRequest'

    responses:

      200:
        description: 원장·Secret·떠 있는 Pod 모두 반영
      400:
        description: 이름 또는 해시 형식 오류
      404:
        description: 계정 없음
      500:
        description: 일부 반영 실패(같은 요청으로 재시도)
    """
    if not is_valid_unix_name(username):
        return jsonify(infra_error("CHANGE_PASSWORD", "INVALID_NAME", f"invalid name: {username}")), 400

    # 원장을 먼저 바꾼다. 계정이 없으면 여기서 끝나 Secret·Pod 를 건드리지 않는다.
    old_hash = set_ledger_password(username, body.passwd_hash)
    if old_hash is None:
        return jsonify(infra_error("CHANGE_PASSWORD", "USER_NOT_FOUND", f"user not found: {username}")), 404

    try:
        secrets = update_account_secrets(username, body.passwd_hash)
    except Exception:
        app.logger.exception("[ACCOUNTS] 계정 Secret 비밀번호 교체 실패: %s", username)
        return jsonify(infra_error("CHANGE_PASSWORD", "SECRET_UPDATE_FAILED",
                                   f"failed to update account secrets: {username}",
                                   rolled_back=_restore_password(username, old_hash))), 500

    pods = sync_running_pod_password(username, body.passwd_hash)
    if pods["failed"] or pods.get("error"):
        return jsonify(infra_error("CHANGE_PASSWORD", "POD_PASSWORD_SYNC_FAILED",
                                   f"failed to apply password to running pods: {username}",
                                   pods=pods, rolled_back=_restore_password(username, old_hash))), 500
    return jsonify({"status": "updated", "user": username, "secrets": secrets, "pods": pods})


def set_ledger_password(username: str, passwd_hash: str):
    """계정 원장(shadow)의 해시와 변경일을 바꾼다. 반환: 바꾸기 전 해시, 계정이 없으면 None."""
    ensure_etc_layout()
    with ledger_lock(), LockedFile(app.config["SHADOW_PATH"], "r+") as f:
        sh_lines = f.read().splitlines()
        for i, line in enumerate(sh_lines):
            rec = parse_shadow_line(line)
            if rec and rec["name"] == username:
                old_hash = rec["passwd"]
                rec["passwd"], rec["lastchg"] = passwd_hash, int(time.time() // 86400)
                sh_lines[i] = format_shadow_entry(rec)
                f.seek(0)
                f.write("\n".join(sh_lines) + "\n")
                f.truncate()
                return old_hash
    return None


def _restore_password(username: str, old_hash: str) -> bool:
    """비밀번호 교체가 중간에 실패했을 때 원장·Secret·떠 있는 Pod 를 옛 해시로 되돌린다. 일부만 바뀐 채로
    남으면 컨테이너마다 비밀번호가 달라지고, admin_be 는 옛 해시를 계속 기준으로 삼는다.

    옛 해시가 SHA-512 crypt 가 아니면(잠긴 계정 "!" 등) Secret 에 넣을 수 없어(이미지가 기동을 거부한다)
    원장만 되돌린다. 반환: 모두 되돌렸는지. 되돌리기까지 실패하면 같은 요청을 다시 보내면 맞춰진다."""
    try:
        set_ledger_password(username, old_hash)
        if not SHA512_CRYPT_RE.match(old_hash or ""):
            app.logger.error("[ACCOUNTS] 옛 해시가 SHA-512 crypt 가 아니라 원장만 되돌림: %s", username)
            return False
        update_account_secrets(username, old_hash)
        pods = sync_running_pod_password(username, old_hash)
        if pods["failed"] or pods.get("error"):
            app.logger.error("[ACCOUNTS] 비밀번호 되돌리기 중 Pod 반영 실패: %s %s", username, pods)
            return False
        return True
    except Exception:
        app.logger.exception("[ACCOUNTS] 비밀번호 되돌리기 실패 — 같은 요청으로 다시 맞출 것: %s", username)
        return False

# Register the blueprint under /accounts
app.register_blueprint(accounts_bp)

# ==========================================
# Swagger 설정
# ==========================================
swagger_config = {
    "headers": [],
    "specs": [
        {
            "endpoint": 'apispec_1',
            "route": '/apispec_1.json',
            "rule_filter": lambda rule: True, # 모든 라우트 강제 문서화
            "model_filter": lambda tag: True,
        }
    ],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/apidocs/"
}

swagger_template = {
    "info": {
        "title": "GPU Server Manager API",
        "description": "Kubernetes Pod 동적 할당 및 시스템 계정 관리 API",
        "version": "1.0.0"
    },
    # 요청 스키마는 request_models의 모델에서 만든다(경로 docstring은 $ref만 적는다).
    "definitions": {
        **swagger_definitions(),

        "ErrorResponse": {
            "type": "object",
            "properties": {
                "step": {"type": "string", "example": "VALIDATE_REQUEST"},
                "error": {"type": "string", "example": "INVALID_REQUEST"},
                "detail": {"type": "string", "example": "request_id: request_id는 admin_be 신청 번호(양의 정수)여야 합니다"},
                "errors": {"type": "array", "items": {"type": "object", "properties": {
                    "field": {"type": "string"}, "message": {"type": "string"}}}}
            }
        }
    }
}

app.config['SWAGGER'] = {
    'title': 'GPU Server Manager API',
    'uiversion': 3
}

# config와 template를 모두 넣어준다.
swagger = Swagger(app, config=swagger_config, template=swagger_template)

# ////////////////////// 작업 등록과 제어기 (v2.0) //////////////////////
# 승인 API는 작업만 등록하고 바로 응답한다. 실행은 별도 제어기(controller.py)가 위의 단계 함수로 한다.
# 작업 목록은 따로 두지 않고 operation_log의 작업 단위 행(PROVISION/REVOKE)으로 판단한다: START만
# 있고 끝(SUCCESS/FAIL/UNKNOWN)이 없는 것이 아직 끝나지 않은 작업이다. 실행에 필요한 입력은
# Redis(영속화 켬)에 두며, 비밀번호는 평문이 아니라 shadow에 그대로 쓸 해시만 저장한다.



# 홈 생성(mkdir -p)과 회수 경로 전 단계는 원래 멱등이라 재실행이 곧 관찰이다.

# 재실행 전에 앞선 시도의 흔적을 걷어내 멱등하게 만드는 사전 정리. 첫 실행에 돌아도 해가 없다.

# 매번 다시 실행하는 순수 조회 단계. 이어하기 때 뒤 단계가 쓰는 user_info를 되살린다.

# spec 객체는 JSON으로 저장할 수 없어, Pod 생성 전이면 재실행해 다시 만든다(사전 정리가 포트 중복을
# 막는다). Pod가 이미 만들어졌으면 spec이 필요 없으므로 건너뛴다.

# 이어하기 때 복원하는 컨텍스트. JSON으로 남길 수 있는 값만.



# 생성·회수 단계는 lifecycle_steps/로 이동했다(얇은 이동). 재수출로 기존 참조를 유지한다.
from lifecycle_steps.provision import (  # noqa: E402
    reconcile_nodeport_allocations, get_cluster_reserved_nodeports, allocate_nodeports,
    release_nodeports, _cleanup_create_failure, step_fetch_user_config, step_prepare_pod,
    step_select_node, step_build_pod_spec, step_create_pod_k8s, step_wait_ready,
    step_create_services, POD_CREATE_STEPS, build_pod_spec, _normalize_gid_list,
    _resolve_primary_group, _build_user_groups_env, _get_sudo_allowed_commands,
    _build_sudoers_policy, _rollback_user, _allocate_next_uid, _allocate_next_gid,
    step_create_account, step_create_home, step_create_krb5_principal, ACCOUNT_CREATE_STEPS,
    step_add_user_groups, step_sync_ad_groups, SUPP_GROUPS_ONLY_STEPS,
    account_secret_name, ensure_account_secret, own_account_secret, delete_account_secret,
    LoginPasswordMissing, decode_login_password, login_password_for_recreate, login_password_hash)
from lifecycle_steps.revoke import (  # noqa: E402
    step_delete_services, step_release_nodeports, step_delete_pod_k8s,
    step_cleanup_pod_node_krb5, _new_delete_rollback, POD_DELETE_STEPS,
    step_check_account_revocable, step_delete_account, step_remove_krb5,
)

from lifecycle_steps.migrate import (  # noqa: E402
    step_migrate_select_target, step_migrate_inherit_password, step_migrate_cleanup_old, MIGRATE_STEPS,
)

# 작업 실행 엔진은 application/jobs.py로 이동했다(얇은 이동). 아래 재수출은 기존 소비자
# (라우트·제어기·테스트의 main.* 참조)를 무수정으로 유지한다.
from application.jobs import (  # noqa: E402
    STEP_MAX_ATTEMPTS, RETRY_DELAY_SEC, STEP_OBSERVERS, RERUN_SAFE, PRE_STEP,
    ALWAYS_RERUN, DEFER_DONE, SAVED_CTX_KEYS, _saved_ctx, _StepDegraded, _execute_step,
    JOB_ACTIONS, _JOB_KIND, _job_steps, _job_ctx, find_unfinished_jobs, job_end_exists, _finish_job,
    _record_job_result, _compensate_provision, run_job, _release_lease, _run_job,
    _observe_account_created, _observe_krb5_principal, _observe_pod_created,
)


def _register_job(kind, request_id, username, job):
    action = JOB_ACTIONS[kind]
    try:
        if not save_job_input(action.value, request_id, job):
            return jsonify(infra_error(
                "REGISTER_JOB", "JOB_ALREADY_REGISTERED",
                f"{kind} job for request {request_id} is already registered and not finished",
            )), 409
    except Exception as e:
        app.logger.exception("[JOB] job input save failed")
        return jsonify(infra_error("REGISTER_JOB", "JOB_STORE_UNAVAILABLE", str(e))), 503

    # 이 START 행이 제어기가 작업을 찾는 근거이자 작업 번호(행 id)라, 기록이 실패하면 등록도 실패로 돌린다.
    # 목표 상태는 이 행에 남긴다(비밀번호 해시 제외).
    target = {k: v for k, v in job.items() if k != "account"}
    if job.get("account"):
        target["account"] = {k: v for k, v in job["account"].items() if k != "passwd_hash"}
    try:
        job_id = log_operation(request_id=request_id, username=username, action=action,
                               phase=Phase.START, target_state=json.dumps(target, ensure_ascii=False),
                               start_job=True, raise_errors=True)
    except Exception as e:
        delete_job_input(action.value, request_id)
        return jsonify(infra_error("REGISTER_JOB", "JOB_LOG_UNAVAILABLE", str(e))), 503

    if kind in ("provision", "migrate"):
        set_pod_creation_status(request_id, "started", "요청 접수")
    app.logger.info(f"[JOB] registered {kind} request_id={request_id} job_id={job_id} username={username}")
    return jsonify({"request_id": request_id, "job_id": job_id, "status": "accepted"}), 202


def _adoptable_account_uid(username: str, expected_uid):
    """원장에 같은 이름의 계정이 있고 UID 가 expected_uid 와 같으면 그 UID. 아니면 None.
    UID 가 다르면 같은 이름을 쓰던 다른 사람의 계정일 수 있어 이어받지 않는다(새로 만들려다 막힌다)."""
    if expected_uid is None:
        return None
    for line in read_passwd_lines():
        rec = parse_passwd_line(line)
        if rec and rec["name"] == username:
            return rec["uid"] if rec["uid"] == expected_uid else None
    return None


def _username_group_conflict(username: str) -> Optional[str]:
    """새 계정명이 예약 이름(RESERVED_ACCOUNT_NAMES)이나 공용 그룹 이름과 겹치면 사유를, 아니면 None 을 돌려준다.
    uid 대역(UID_MIN 이상, 공용 gid 대역 미만)의 같은 이름 그룹은 이 사용자의 개인 그룹이 남은
    것이라 계정 단계가 그대로 이어 쓴다 — 충돌로 보지 않는다."""
    if username in RESERVED_ACCOUNT_NAMES:
        return f"username is reserved by the container image or operations: {username}"
    for line in read_group_lines():
        rec = parse_group_line(line)
        if not rec or rec["name"] != username:
            continue
        if UID_MIN <= rec["gid"] < SHARED_GID_MIN:
            return None
        return f"username collides with an existing group: {username} (gid {rec['gid']})"
    return None


@app.route("/operations/provision", methods=["POST"])
@validate_body(ProvisionRequest)
def register_provision(body: ProvisionRequest):
    """
    생성 작업 등록 (v2.0)

    계정(선택)과 Pod 생성을 작업으로 등록하고 바로 202를 돌려준다. 실행은 제어기가 한다.
    계정이 이미 있으면 account를 빼고 보낸다. supplementary_groups은 account 여부와
    무관하게 pod 생성 후 추가된다(기존 계정 재사용 경로). 진행 상황은 GET /requests/<request_id>/status,
    작업 결과는 GET /operations/provision/<request_id>로 조회한다.
    ---
    tags:
    - Operations
    parameters:
      - in: body
        name: body
        required: true
        schema:
          $ref: '#/definitions/ProvisionRequest'
    responses:
      202: {description: 등록됨}
      400: {description: 입력 오류}
      409: {description: 같은 신청의 생성 작업이 아직 끝나지 않음}
    """
    username = body.username
    job = {"username": username}
    adopted_uid = _adoptable_account_uid(username, body.account.expected_uid) if body.account else None
    if adopted_uid is not None:
        # admin_be 의 계정 기록만 빠진 경우다(실패 작업이 계정을 남겼거나 수동 정리). 새로 만들면
        # USER_ALREADY_EXISTS 로 막히므로, 이 사용자가 쓰던 바로 그 계정이면 재사용 경로로 돌린다.
        set_ledger_password(username, body.account.password_hash())
        groups = {g.name: g.model_dump() for g in [*body.account.supplementary_groups, *body.supplementary_groups]}
        job["adopted_uid"] = adopted_uid
        if groups:
            job["supp_groups_only"] = list(groups.values())
        app.logger.warning(f"[JOB] 원장에 남은 계정을 이어받음: user={username} uid={adopted_uid}")
        return _register_job("provision", body.request_id, username, job)
    if body.account is not None:
        conflict = _username_group_conflict(username)
        if conflict:
            # 개인 그룹을 계정명으로 만들고 AD 는 사용자·그룹 이름 공간을 공유한다 — 작업으로 넘기면
            # 계정 단계에서 primary group conflict 로 실패하고 되돌리므로 등록 전에 거절한다.
            # 409 는 admin_be 가 "같은 신청의 작업이 진행 중"으로 읽으므로 400 으로 돌려준다.
            return jsonify(infra_error("PROVISION", "USERNAME_CONFLICTS_GROUP", conflict)), 400
        account = body.account
        job["account"] = {
            "pg_name": account.primary_group_name or username,
            "supp_groups": [group.model_dump() for group in account.supplementary_groups],
            "gecos": account.gecos,
            "passwd_hash": account.password_hash(),
            # 원장엔 없지만 이 사람이 예전에 쓰던 uid — 계정 단계가 NAS 홈 소유자와 맞춰 보고 되돌려 준다.
            "expected_uid": account.expected_uid,
        }

    # supplementary_groups는 account가 없을 때도 처리 (기존 계정 재사용 경로)
    if body.supplementary_groups:
        job["supp_groups_only"] = [group.model_dump() for group in body.supplementary_groups]
    
    return _register_job("provision", body.request_id, username, job)


@app.route("/operations/revoke", methods=["POST"])
@validate_body(RevokeRequest)
def register_revoke(body: RevokeRequest):
    """
    회수 작업 등록 (v2.0)

    Pod 회수(Service·NodePort·Pod·그 노드 keytab)와, delete_account가 참이면 계정·Kerberos 회수까지
    작업으로 등록하고 바로 202를 돌려준다. 홈 디렉터리는 보존한다.
    ---
    tags:
    - Operations
    parameters:
      - in: body
        name: body
        required: true
        schema:
          $ref: '#/definitions/RevokeRequest'
    responses:
      202: {description: 등록됨}
      400: {description: 입력 오류}
      409: {description: 같은 신청의 회수 작업이 아직 끝나지 않음}
    """
    username = body.username or _pod_username(body.pod_name)
    job = {"username": username, "pod_name": body.pod_name, "node_name": body.node_name,
           "delete_account": body.delete_account}
    return _register_job("revoke", body.request_id, username, job)


@app.route("/operations/migrate", methods=["POST"])
@validate_body(MigrateRequest)
def register_migrate(body: MigrateRequest):
    """
    마이그레이션 작업 등록 (v2.0)

    사용자 Pod를 다른 노드로 옮기는 작업을 등록하고 바로 202를 돌려준다. 제어기가 옮길 노드를 고르고(force면
    개선 비율을 보지 않음), 새 노드에 Pod를 만들어 준비되면 기존 Pod를 정리한다. 옮길 이유가 없으면 작업은
    성공으로 끝나고 결과의 status가 skipped다. 결과는 GET /operations/migrate/<request_id>로 조회한다.
    홈 디렉터리는 유지되고 컨테이너 안의 시스템 변경은 유지되지 않는다.
    ---
    tags:
    - Operations
    parameters:
      - in: body
        name: body
        required: true
        schema:
          $ref: '#/definitions/MigrateRequest'
    responses:
      202: {description: 등록됨}
      400: {description: 입력 오류}
      409: {description: 같은 신청의 마이그레이션 작업이 아직 끝나지 않음}
    """
    job = {"username": body.username, "pod_name": body.pod_name, "nodes": body.nodes, "force": bool(body.force)}
    if body.min_improvement_ratio is not None:
        job["min_improvement_ratio"] = body.min_improvement_ratio
    return _register_job("migrate", body.request_id, body.username, job)


@app.route("/operations/nas-gss-flush", methods=["POST"])
def trigger_nas_gss_flush():
    """
    NAS GSS 캐시 온디맨드 flush 트리거 (#161)

    그룹 변경 승인 직후 admin_be가 부른다. 30분 크론(reconcile_krb5.py)과 같은 조건(NAS
    winbind가 이미 대장을 따라잡았을 때만)으로 flush하되, 승인 직후부터 짧은 간격으로 최대
    10분간 재시도한다. 요청은 즉시 202로 끝나고 실제 작업은 백그라운드에서 돈다 — admin_be의
    승인 트랜잭션을 막지 않기 위함이라, 이 응답은 "재시도를 시작했다/이미 돌고 있다"만 뜻하지
    flush 성공을 보장하지 않는다(실패해도 30분 크론이 안전망으로 남아있다).
    ---
    tags:
    - Operations
    responses:
      202: {description: 재시도 루프를 새로 띄웠거나 이미 돌고 있음}
    """
    # main.py를 import하는 reconcile_krb5.py를 순환 임포트 없이 쓰려고 함수 안에서 늦게 불러온다.
    from reconcile_krb5 import trigger_nas_gss_flush_ondemand
    started = trigger_nas_gss_flush_ondemand()
    return jsonify({"status": "accepted", "started_new_loop": started}), 202


@app.route("/operations/<kind>/<request_id>", methods=["GET"])
def get_job_result(kind, request_id):
    """
    작업 결과 조회 (v2.0)

    phase: none(등록 이력 없음) / START(대기·실행 중) / SUCCESS / FAIL / UNKNOWN

    성공한 작업은 만든 자원을 result로 함께 돌려준다(생성: 계정 uid·gid, Pod 이름·노드, 외부 포트
    목록 — 포트는 internal_port·external_port·usage_purpose). 하루가 지나면 result는 사라지고 phase만 남는다.
    ---
    tags:
    - Operations
    parameters:
      - {in: path, name: kind, required: true, type: string, enum: [provision, revoke, migrate]}
      - {in: path, name: request_id, required: true, type: string}
    responses:
      200: {description: 조회 성공}
      404: {description: 알 수 없는 작업 종류}
    """
    action = JOB_ACTIONS.get(kind)
    if action is None:
        return jsonify(infra_error("GET_JOB", "UNKNOWN_JOB_KIND", f"unknown job kind {kind!r}")), 404
    conn = get_log_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT phase, error_code, created_at, job_id FROM operation_log "
                "WHERE request_id=%s AND action=%s ORDER BY id DESC LIMIT 1",
                (str(request_id), action.value),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify({"request_id": request_id, "kind": kind, "phase": "none"}), 200
    phase, error_code, created_at, job_id = row
    return jsonify({"request_id": request_id, "kind": kind, "job_id": job_id, "phase": phase,
                    "error_code": error_code, "updated_at": str(created_at),
                    "result": load_job_result(action.value, request_id)}), 200


# 한 신청에서 돌려줄 최근 작업 수. 재승인·재회수가 반복돼도 응답이 커지지 않게 한다.
JOB_STEPS_MAX_JOBS = 5
# 단계 기록 요약에 내보내도 되는 근거 항목. 접근 시험 근거에는 노드 내부 주소, NAS 마운트 경로,
# 명령 출력이 섞여 있어 그대로 내보내지 않는다(이 기록은 관리자 화면에 보인다).
_STEP_SUMMARY_KEYS = ("expected_uid", "requested", "visible", "node", "placed_in_candidates", "roundtrip",
                      "owner_uid", "connected", "reason", "likely_cause", "step", "compensation",
                      "interrupted_after", "unknown", "degraded", "rc", "stat_rc",
                      # 컨테이너 준비 대기: 이미지 새로 받음/노드에 있던 이미지, 받은 시간·크기, 재시도 횟수
                      "image_source", "image_pull_seconds", "image_size_mb", "mount_retries", "restarts")
_INTERNAL_ADDRESS = re.compile(r"\d{1,3}(\.\d{1,3}){3}|:/")
_TERMINAL_PHASES = (Phase.SUCCESS.value, Phase.FAIL.value, Phase.UNKNOWN.value)


def _step_summary(detail):
    try:
        data = json.loads(detail) if detail else None
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    summary = {}
    for key in _STEP_SUMMARY_KEYS:
        value = data.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        if isinstance(value, str):
            if _INTERNAL_ADDRESS.search(value):
                continue
            value = value[:120]
        summary[key] = value
    return summary or None


def _utc_iso(ts):
    # operation_log.created_at은 UTC로 저장된다.
    return ts.isoformat() + "Z" if isinstance(ts, datetime) else (str(ts) if ts is not None else None)


@app.route("/operations/<kind>/<request_id>/steps", methods=["GET"])
def get_job_steps(kind, request_id):
    """
    작업 단계 기록 조회 (v2.0)

    한 신청의 생성(provision) 또는 회수(revoke) 작업을 최근 순으로 최대 5개, 작업마다 단계별 결과를
    시각순으로 돌려준다. 단계는 끝난 행(SUCCESS/FAIL/RETRY/UNKNOWN)만 담는다. 접근 시험 근거는 화면에
    보여도 되는 항목만 summary로 요약한다(내부 주소·마운트 경로·명령 출력 제외). 관리자 인증은 이 API를
    부르는 admin_be가 맡는다.
    ---
    tags:
    - Operations
    parameters:
      - {in: path, name: kind, required: true, type: string, enum: [provision, revoke, migrate]}
      - {in: path, name: request_id, required: true, type: string}
    responses:
      200: {description: 조회 성공 — 작업이 없으면 jobs가 빈 목록}
      404: {description: 알 수 없는 작업 종류}
    """
    action = JOB_ACTIONS.get(kind)
    if action is None:
        return jsonify(infra_error("GET_JOB", "UNKNOWN_JOB_KIND", f"unknown job kind {kind!r}")), 404
    conn = get_log_db_connection()
    try:
        with conn.cursor() as cur:
            # 작업 시작 행은 자기 id가 작업 번호다.
            cur.execute(
                "SELECT id FROM operation_log WHERE request_id=%s AND action=%s AND phase=%s AND job_id=id "
                "ORDER BY id DESC LIMIT %s",
                (str(request_id), action.value, Phase.START.value, JOB_STEPS_MAX_JOBS),
            )
            job_ids = [r[0] for r in cur.fetchall()]
            rows = []
            if job_ids:
                marks = ",".join(["%s"] * len(job_ids))
                cur.execute(
                    "SELECT job_id, action, phase, attempt, resource_type, error_code, error_detail, created_at "
                    f"FROM operation_log WHERE job_id IN ({marks}) ORDER BY id",
                    tuple(job_ids),
                )
                rows = cur.fetchall()
    finally:
        conn.close()

    jobs = {job_id: {"job_id": job_id, "started_at": None, "finished_at": None, "phase": Phase.START.value,
                     "error_code": None, "steps": []} for job_id in job_ids}
    probe_actions = (Action.VERIFY_ACCESS.value, Action.VERIFY_REVOKED.value)
    for job_id, act, phase, attempt, resource_type, error_code, detail, created_at in rows:
        job = jobs.get(job_id)
        if job is None:
            continue
        at = _utc_iso(created_at)
        if act == action.value and phase == Phase.START.value:
            job["started_at"] = at
            continue
        if act != action.value and phase == Phase.START.value:
            continue  # 단계 시작 행은 끝 행과 짝이라 끝 행만 담는다
        if act == action.value and phase in _TERMINAL_PHASES:
            job.update(finished_at=at, phase=phase, error_code=error_code)
        job["steps"].append({
            "at": at, "action": act, "phase": phase, "attempt": attempt,
            "probe": resource_type if act in probe_actions else None,
            # 재시도 행의 resource_type은 다시 돌린 단계 이름이다
            "step": resource_type if phase == Phase.RETRY.value else None,
            "error_code": error_code, "summary": _step_summary(detail),
        })
    return jsonify({"request_id": str(request_id), "kind": kind,
                    "jobs": [jobs[job_id] for job_id in job_ids]}), 200



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
