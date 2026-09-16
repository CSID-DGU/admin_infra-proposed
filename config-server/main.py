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
import crypt
import json
import subprocess
from datetime import datetime

from error import infra_error, k8s_error_fields
from request_models import (validate_body, check_values, swagger_definitions, ProvisionRequest, RevokeRequest,
                            DeletePodRequest, MigrateRequest, AddGroupRequest, AddUserGroupsRequest)
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
    LockedFile, get_node_gpu_score,
    ensure_etc_layout, ensure_sudoers_file,
    read_passwd_lines, write_passwd_lines,
    read_group_lines, write_group_lines,
    read_shadow_lines, write_shadow_lines,
    parse_passwd_line, format_passwd_entry,
    parse_group_line, format_group_entry,
    parse_shadow_line, format_shadow_entry,
    create_user_home_directory,
    delete_user_home_directory,
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


@app.before_request
def _require_api_token():
    if not API_TOKEN:
        return None
    if request.method == "GET" and _TOKEN_FREE_GET.match(request.path):
        return None
    if hmac.compare_digest(request.headers.get("X-Internal-Token", ""), API_TOKEN):
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
        description: 서버 정상. run_mode 는 실행 중인 조건, oplog_write_failures 는
          작업 이력 기록 실패 누적 건수(조회 불능이면 null — 0 과 구분).
        schema:
          type: object
          example: {"status": "OK", "run_mode": "noprobe", "oplog_write_failures": 0}
    """
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

    def __init__(self, body, status, cause=None):
        super().__init__(body.get("error") if isinstance(body, dict) else str(body))
        self.body = body
        self.status = status
        # 요청은 나갔지만 응답을 못 받아 실제로 실행됐는지 알 수 없는 실패인지(UNKNOWN)
        self.unknown = _is_unknown_result(cause)


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

def _farm_ad_ssh(remote_command: str, stdin_data: str = "") -> str:
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
            result = subprocess.run(cmd, input=stdin_data, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as e:
            last_error = e
            app.logger.warning(f"[FARM AD SSH] {node['name']} 타임아웃")
            continue
        if result.returncode != 0:
            last_error = RuntimeError(f"AD DC SSH 실패 ({node['name']}): {result.stderr.strip()}")
            app.logger.warning(f"[FARM AD SSH] {node['name']} 실패: {result.stderr.strip()}")
            continue
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
    if members:
        passwd_lines = read_passwd_lines()
        existing_users = {parse_passwd_line(l)["name"] for l in passwd_lines if parse_passwd_line(l)}
        invalid_members = [m for m in members if m not in existing_users]
        if invalid_members:
            return jsonify(infra_error("ADD_GROUP", "INVALID_GROUP_MEMBER",
                                       f"invalid members (users not found): {', '.join(invalid_members)}")), 400

    ensure_etc_layout()
    with LockedFile(app.config["GROUP_PATH"], "r+") as f:
        g_lines = f.read().splitlines()

        if any((parse_group_line(gl) or {}).get("name") == name for gl in g_lines):
            return jsonify(infra_error("ADD_GROUP", "GROUP_NAME_EXISTS", f"group already exists (name: {name})")), 409

        if gid is None:
            gid = _allocate_next_gid(g_lines, min_gid=UID_MIN)
            if UID_MAX is not None and gid > UID_MAX:
                return jsonify(infra_error("ADD_GROUP", "GID_RANGE_EXHAUSTED", f"gid range {UID_MIN}~{UID_MAX} exhausted")), 500
        elif any((parse_group_line(gl) or {}).get("gid") == gid for gl in g_lines):
            return jsonify(infra_error("ADD_GROUP", "GROUP_GID_EXISTS", f"group already exists (gid: {gid})")), 409

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

    # Update group file
    g_lines = read_group_lines()
    names = set(groups)
    updated = False
    new_lines = []
    for gl in g_lines:
        rec = parse_group_line(gl)
        if rec and rec["name"] in names:
            members = set(rec.get("members", []))
            if username not in members:
                members.add(username)
                rec["members"] = sorted(members)
                updated = True
            new_lines.append(format_group_entry(rec))
        else:
            new_lines.append(gl)

    # Ensure all requested groups existed
    existing_group_names = {parse_group_line(gl)["name"] for gl in g_lines if parse_group_line(gl)}
    missing = [g for g in groups if g not in existing_group_names]
    if missing:
        return jsonify(infra_error("ADD_USER_GROUPS", "GROUP_NOT_FOUND", f"groups not found: {', '.join(missing)}")), 404

    write_group_lines(new_lines)
    return jsonify({"status": "updated", "user": username, "groups": sorted(list(names))})

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
    account_secret_name, ensure_account_secret, own_account_secret, delete_account_secret,
    LoginPasswordMissing, decode_login_password, login_password_for_recreate)
from lifecycle_steps.revoke import (  # noqa: E402
    step_delete_services, step_release_nodeports, step_delete_pod_k8s,
    step_cleanup_pod_node_krb5, _new_delete_rollback, POD_DELETE_STEPS,
    step_check_account_revocable, step_delete_account, step_delete_home,
    step_remove_krb5, ACCOUNT_DELETE_STEPS,
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


@app.route("/operations/provision", methods=["POST"])
@validate_body(ProvisionRequest)
def register_provision(body: ProvisionRequest):
    """
    생성 작업 등록 (v2.0)

    계정(선택)과 Pod 생성을 작업으로 등록하고 바로 202를 돌려준다. 실행은 제어기가 한다.
    계정이 이미 있으면 account를 빼고 보낸다. 진행 상황은 GET /requests/<request_id>/status,
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
    if body.account is not None:
        account = body.account
        job["account"] = {
            "pg_name": account.primary_group_name or username,
            "supp_groups": [group.model_dump() for group in account.supplementary_groups],
            "gecos": account.gecos,
            "passwd_hash": crypt.crypt(account.plaintext_password(), crypt.mksalt(crypt.METHOD_SHA512)),
        }
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
                      "interrupted_after", "unknown", "degraded", "rc",
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
