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
import crypt
import json
import subprocess
from datetime import datetime

from error import infra_error, k8s_error_fields
from adapters.pod_status import (
    set_pod_creation_status, get_pod_creation_status,
    save_job_input, load_job_input, mark_job_running, mark_job_done, delete_job_input,
    save_job_result, load_job_result,
)
from adapters.operation_log import Action, Phase, log_operation, current_job_id, current_attempt

from adapters import job_control
from adapters.job_control import LeaseLost
from lifecycle_steps import verify

from utils import (
    get_db_connection, get_log_db_connection, is_pod_ready, get_pod_failure_reason, get_pod_progress_stage,
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

# 로그 설정
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
# 제안 시스템 조건(noprobe | full). 실제 접근 시험(probe)을 수행할지를 이 값 하나로 가른다.
VERIFY_MODE = os.getenv("VERIFY_MODE", "noprobe")
# 실험 스택은 AD·Kerberos, NAS 홈, farm keytab을 운영과 같이 쓰고 이것들은 이름으로 식별된다.
# 접두어를 주면 그 접두어로 시작하지 않는 이름은 받지 않아, 운영 계정을 덮어쓰거나 지우지 못하게 한다.
# 비워 두면(운영) 제한 없음.
ACCOUNT_PREFIX = os.getenv("ACCOUNT_PREFIX", "")


@app.before_request
def _enforce_account_prefix():
    if not ACCOUNT_PREFIX or request.method not in ("POST", "PUT", "DELETE"):
        return None
    names = []
    if request.view_args and "username" in request.view_args:
        names.append(request.view_args["username"])
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        if request.path in ("/create-pod", "/migrate", "/operations/provision", "/operations/revoke"):
            names.append(body.get("username"))
        if request.path == "/operations/revoke" and str(body.get("pod_name") or "").startswith("ailab-"):
            names.append(_pod_username(body["pod_name"]))
        elif request.path == "/accounts/users" and request.method == "PUT":
            names.append(body.get("name"))
    bad = [n for n in names if n is not None and not str(n).startswith(ACCOUNT_PREFIX)]
    if bad:
        app.logger.warning(f"[PREFIX] 접두어 없는 계정 거절: {bad} ({request.method} {request.path})")
        return jsonify({"status": "error",
                        "message": f"이 환경은 '{ACCOUNT_PREFIX}'로 시작하는 계정만 다룹니다"}), 403
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
        description: 서버 정상
        schema:
          type: string
          example: OK
    """
    return "OK", 200

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
# 생성·회수 흐름을 단계 함수로 나눈다. 동기 엔드포인트(/create-pod, /delete-pod, PUT·DELETE
# /accounts/users)와 제어기(controller.py)가 같은 단계 함수를 차례로 부르므로, 두 경로의 차이는
# 실행 구조(요청 안에서 끝까지 실행하는지, 작업만 등록하고 제어기가 실행하는지)뿐이다.

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



















@app.route("/create-pod", methods=["POST"])
def create_pod():
    """
    사용자 컨테이너 Pod 생성 API

    이 API는 Kubernetes에 사용자 Pod를 생성합니다.

    동작 과정

    1. WAS 서버에서 사용자 설정 조회
    2. GPU 노드 중 가장 적합한 노드 선택
    3. NodePort 자동 할당
    4. Pod 생성
    5. Service 생성

    ---
    tags:
    - Pod

    summary: 사용자 Pod 생성

    description: |
        특정 사용자 환경을 Kubernetes Pod로 생성합니다.

    consumes:
    - application/json

    produces:
    - application/json

    parameters:

      - in: body
        name: body
        required: true
        schema:
          $ref: '#/definitions/CreatePodRequest'

    responses:

      201:
        description: Pod 생성 성공
        schema:
          $ref: '#/definitions/CreatePodResponse'
      400:
        description: username 누락
        schema:
          $ref: '#/definitions/ErrorResponse'
      409:
        description: 동일 Pod 이미 존재
        schema:
          $ref: '#/definitions/ErrorResponse'
      500:
        description: 서버 내부 오류
        schema:
          $ref: '#/definitions/ErrorResponse'
    """
    data = request.get_json(force=True)
    username = data.get("username")
    # 진행 상황 조회 키. username만으로는 한 사용자가 Pod를 여러 개 동시에 생성할 때
    # 서로 다른 시도의 진행 상황이 같은 키에서 덮어써져 구분이 안 된다 — request_id는
    # 신청 하나당 하나로 고정이라 이걸로 키를 잡는다.
    request_id = data.get("request_id")

    app.logger.info(f"[CREATE POD] request received - username={username} request_id={request_id}")
    if username:
        set_pod_creation_status(request_id, "started", "요청 접수")

    if not username:
        app.logger.warning("[CREATE POD] username missing in request")
        return jsonify(infra_error(
            "VALIDATE_REQUEST",
            "INVALID_CREATE_POD_REQUEST",
            "username required",
        )), 400

    if not request_id:
        app.logger.warning("[CREATE POD] request_id missing in request")
        return jsonify(infra_error(
            "VALIDATE_REQUEST",
            "INVALID_CREATE_POD_REQUEST",
            "request_id required",
        )), 400

    ctx = {"request_id": request_id, "username": username}
    try:
        for step in POD_CREATE_STEPS:
            step(ctx)
    except StepFailed as e:
        return jsonify(e.body), e.status
    except Exception as e:
        app.logger.exception("[CREATE POD] unexpected error")
        set_pod_creation_status(request_id, "failed", "예기치 않은 오류")
        return jsonify(infra_error(
            "CREATE_POD",
            "CREATE_POD_FAILED",
            str(e),
        )), 500

    return jsonify({
        "status": "created",
        "node": ctx["node"],
        "pod_name": ctx["pod_name"],
        "ports": ctx["allocated_ports"]
    }), 201


@app.route("/requests/<request_id>/status", methods=["GET"])
def get_pod_status(request_id):
    """
    신청(request) 단위 Pod 생성 진행 상황 조회

    /create-pod는 이미지 pull 등으로 오래(최대 POD_READY_MAX_WAIT_SEC초) 걸릴 수 있는
    동기 API라서, 그 요청이 끝나기 전에 별도로 진행 상황만 가볍게 조회하기 위한 엔드포인트.
    한 사용자가 Pod를 여러 개 동시에 생성할 수 있어 username이 아니라 request_id로 조회한다
    (create-pod 호출 시 넘긴 request_id와 동일한 값).

    stage는 다음 순서로 진행되며, 최종 상태는 ready 또는 failed다:
      - unknown            : 생성 이력 없음 (한 번도 /create-pod를 호출한 적 없음)
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













@app.route("/delete-pod", methods=["POST"])
def delete_pod():
    """
    사용자 Pod 삭제 API

    특정 Pod를 삭제하고 다음 리소스를 정리합니다.

    - Kubernetes Pod
    - NodePort Service
    - NodePort DB allocation

    ---
    tags:
    - Pod

    summary: 사용자 Pod 삭제

    consumes:
    - application/json

    parameters:

      - in: body
        name: body
        required: true
        schema:
          $ref: '#/definitions/DeletePodRequest'

    responses:

      200:
        description: Pod 삭제 성공
        schema:
          type: object
          properties:
            status:
              type: string
              example: deleted
      400:
        description: 잘못된 요청
        schema:
          $ref: '#/definitions/ErrorResponse'
      500:
        description: 삭제 실패
        schema:
          $ref: '#/definitions/ErrorResponse'
    """
    data = request.get_json(force=True)
    pod_name = data.get("pod_name")

    app.logger.info(f"[DELETE POD] request received - pod_name={pod_name}")

    if not pod_name:
        app.logger.warning("[DELETE POD] pod_name missing")
        return jsonify(infra_error(
            "VALIDATE_REQUEST",
            "INVALID_DELETE_POD_REQUEST",
            "pod_name required",
        )), 400

    rollback = _new_delete_rollback()

    try:
        if not pod_name.startswith("ailab-"):
            app.logger.warning(f"[DELETE POD] invalid pod_name format: {pod_name}")
            return jsonify(infra_error(
                "VALIDATE_REQUEST",
                "INVALID_POD_NAME",
                "invalid pod_name",
                rollback=rollback,
                pod_name=pod_name,
            )), 400

        username = _pod_username(pod_name)
        # admin_be는 이 Pod를 만든 신청 PK를 request_id로 보낸다. 대응하는 신청이 없는
        # 고아 Pod 정리처럼 값이 없는 호출은 이 삭제 호출 하나만을 묶는 임시 키로 기록한다.
        request_id = data.get("request_id") or f"{username}-DELETE-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:-3]}"

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


def _migrate_internal(data):

    load_k8s()
    v1 = client.CoreV1Api()

    username = data.get("username")
    nodes = data.get("nodes")  # resource group에 속한 node_id 목록
    min_ratio = data.get("min_improvement_ratio", 0.2)

    ns = app.config["NAMESPACE"]

    if nodes:
        canon = []
        for n in nodes:
            c = resolve_k8s_node_name(n)
            if not c:
                return jsonify({"error": f"unknown kubernetes node: {n!r}"}), 400
            canon.append(c)
        nodes = canon

    # 1. 현재 Pod 확인
    old_pod_name = get_existing_pod(ns, username)
    if not old_pod_name:
        return jsonify({"error": "no running pod"}), 404

    pod = v1.read_namespaced_pod(old_pod_name, ns)
    current_node = pod.spec.node_name

    if current_node not in nodes:
        return jsonify({
            "error": "current node is not in given nodes list"
        }), 400

    candidate_nodes = [n for n in nodes if n != current_node]
    if not candidate_nodes:
        return jsonify({
            "status": "skipped",
            "reason": "no_candidate_node"
        }), 200

    # 2. GPU score 계산
    prom_url = app.config["PROM_URL"]
    timeout = app.config["HTTP_TIMEOUT_SEC"]

    current_score = get_node_gpu_score(current_node, prom_url, timeout)
    scores = {
        node: get_node_gpu_score(node, prom_url, timeout)
        for node in candidate_nodes
    }

    best_node, best_score = min(scores.items(), key=lambda x: x[1])

    # 3. 이전(migrate) 기준 판단
    if best_score > current_score * (1 - min_ratio):
        return jsonify({
            "status": "skipped",
            "reason": "no_significant_improvement",
            "current_node": current_node,
            "current_score": current_score,
            "best_candidate": best_node,
            "best_score": best_score
        }), 200

    # 4. WAS에서 사용자 정보 조회
    was_url = app.config["WAS_URL_TEMPLATE"].format(username=username)
    resp = requests.get(
        was_url,
        timeout=app.config["HTTP_TIMEOUT_SEC"]
    )

    app.logger.info(f"[MIGRATE] WAS status={resp.status_code}")
    app.logger.debug(f"[MIGRATE] WAS body={resp.text}")

    user_info = resp.json()

    # 5. 기존 Pod 이미지 저장
    ok = commit_and_save_user_image(username, old_pod_name, ns)
    if not ok:
        return jsonify({
            "error": "image_commit_failed"
        }), 500

    # 6. 새 Pod 이름 생성
    new_pod_name = generate_pod_name(username)

    # 7. 새 노드에서 Pod 재생성
    try:
        spec_wrapper, allocated_ports = build_pod_spec(
            username,
            user_info,
            best_node,
            new_pod_name
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    pod_spec = spec_wrapper["config"]["kubernetes"]["pod"]

    set_pod_creation_status(username, "creating_pod", f"마이그레이션: k8s pod 생성 중 (node={best_node})")
    try:
        v1.create_namespaced_pod(namespace=ns, body=pod_spec)
    except Exception:
        set_pod_creation_status(username, "failed", "마이그레이션 실패: pod 생성 실패")
        release_nodeports(new_pod_name)
        raise

    # 8. Ready 대기 (create-pod와 동일하게 POD_READY_MAX_WAIT_SEC를 쓴다 —
    # 예전엔 60초로 하드코딩돼 있어서 이미지 pull이 오래 걸리는 이미지는
    # create-pod보다 훨씬 먼저 실패 처리되는 불일치가 있었다.)
    migrate_failure_reason = None
    max_wait = app.config["POD_READY_MAX_WAIT_SEC"]
    last_progress_stage = None
    for i in range(max_wait):
        pod = v1.read_namespaced_pod(new_pod_name, ns)
        if is_pod_ready(pod):
            set_pod_creation_status(username, "ready", f"마이그레이션 완료 (node={best_node})")
            break
        migrate_failure_reason = get_pod_failure_reason(pod)
        if migrate_failure_reason:
            break
        if i % 5 == 0:
            progress = get_pod_progress_stage(v1, ns, new_pod_name)
            if progress and progress[0] != last_progress_stage:
                last_progress_stage, progress_message = progress
                set_pod_creation_status(username, last_progress_stage, progress_message)
                app.logger.info(f"[MIGRATE] username={username} pod={new_pod_name} stage={last_progress_stage} {progress_message}")
        time.sleep(1)
    else:
        migrate_failure_reason = migrate_failure_reason or f"pod not ready within {max_wait}s"

    if migrate_failure_reason:
        # 새 Pod 실패 -> 정리 후 종료
        app.logger.error(f"[MIGRATE] new pod failed to start: {migrate_failure_reason}")
        set_pod_creation_status(username, "failed", "마이그레이션 실패")
        v1.delete_namespaced_pod(new_pod_name, ns)
        release_nodeports(new_pod_name)
        return jsonify({"error": "new pod failed to start", "detail": migrate_failure_reason}), 500

    # 9. 새 Pod 성공 후 Service 생성
    set_pod_creation_status(username, "creating_services", "마이그레이션: NodePort 서비스 생성 중")
    try:
        create_nodeport_services(
            username,
            ns,
            new_pod_name,
            allocated_ports
        )
    except Exception:
        set_pod_creation_status(username, "failed", "마이그레이션 실패: 서비스 생성 실패")
        v1.delete_namespaced_pod(new_pod_name, ns)
        release_nodeports(new_pod_name)
        return jsonify({"error": "service creation failed"}), 500

    # 10. 기존 Pod 정리 — 새 Pod는 이미 정상 기동되어 서비스 중이므로, 여기서 실패해도
    # 마이그레이션 자체는 성공으로 응답한다 (호출자가 실패로 오인해 재시도하면 중복 Pod가 생길 수 있음).
    # 다만 실패 사실은 응답에 남겨서 수동 정리가 필요함을 알 수 있게 한다.
    old_pod_cleanup_failed = False
    try:
        delete_nodeport_services(old_pod_name, ns)
        release_nodeports(old_pod_name)
        delete_pod_util(old_pod_name, ns)
    except Exception:
        app.logger.exception(f"[MIGRATE] 기존 Pod({old_pod_name}) 정리 실패 — 새 Pod는 정상 기동됨, 수동 정리 필요")
        old_pod_cleanup_failed = True

    set_pod_creation_status(username, "ready", f"마이그레이션 완료 (node={best_node})")

    response = {
        "status": "migrated",
        "from": current_node,
        "to": best_node,
        "new_pod": new_pod_name,
        "ports": allocated_ports
    }
    if old_pod_cleanup_failed:
        response["old_pod_cleanup"] = "failed"
    return jsonify(response), 200


@app.route("/migrate", methods=["POST"])
def migrate():
    """
    Pod GPU 노드 마이그레이션

    현재 실행 중인 사용자 Pod를 더 성능이 좋은 GPU 노드로 이동합니다.

    동작 순서

    1. 현재 Pod 조회
    2. GPU score 계산
    3. 더 좋은 노드 존재 시 마이그레이션
    4. 기존 Pod commit
    5. 새로운 Pod 생성
    6. 기존 Pod 삭제

    ---
    tags:
    - Migration

    summary: GPU 노드 마이그레이션

    consumes:
    - application/json

    parameters:

      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - username
            - nodes
          properties:
            username:
              type: string
              description: 사용자 이름
              example: alice
            nodes:
              type: array
              items:
                type: string
              example:
                - gpu-node-1
                - gpu-node-2
            min_improvement_ratio:
              type: number
              example: 0.2

    responses:

      200:
        description: 마이그레이션 성공 또는 skip
      400:
        description: 잘못된 요청
      404:
        description: 실행 중 Pod 없음
      500:
        description: 서버 오류
    """
    data = request.get_json(force=True)
    username = data.get("username")
    nodes = data.get("nodes")

    if not username or not nodes or not isinstance(nodes, list):
        return jsonify({
            "error": "username and nodes(list) are required"
        }), 400

    lock_path = f"/tmp/migrate-{username}.lock"

    with LockedFile(lock_path, "w"):
        try:
            return _migrate_internal(data)
        except Exception as e:
            app.logger.exception("[MIGRATE] unexpected error")
            set_pod_creation_status(username, "failed", "마이그레이션 실패: 예기치 않은 오류")
            return jsonify(infra_error(
                "MIGRATE",
                "MIGRATE_FAILED",
                str(e),
            )), 500




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
               "-o", "StrictHostKeyChecking=no",
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
           "-o", "StrictHostKeyChecking=no",
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
            # 원격 ailab-krb5-admin 스크립트는 자체적으로 kinit을 최대 30초(kinit_timeout)까지
            # 기다린 뒤 응답한다. 클라이언트 타임아웃이 그것과 같은 30초면, 원격이 막 자기
            # 한도를 다 채우고 정상적으로 응답하려는 순간 클라이언트가 먼저 끊어버리는 경합이
            # 생긴다. 원격이 스스로 정리하고 응답할 시간을 확실히 벌어주기 위해 60초로 둔다.
            result = subprocess.run(
                cmd, input=stdin_data, capture_output=True, text=True, timeout=60,
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

# ---------- /etc/passwd CRUD ----------
@accounts_bp.route("/users", methods=["GET"])
def list_users():
    """
    사용자 목록 조회

    ---
    tags:
    - Accounts

    summary: 시스템 사용자 목록

    responses:

      200:
        description: 사용자 목록 반환
      500:
        description: 서버 오류
    """
    try:
        lines = read_passwd_lines()
        users = []
        for line in lines:
            rec = parse_passwd_line(line)
            if rec:
                users.append({
                    "name": rec["name"],
                    "uid": rec["uid"],
                    "gid": rec["gid"],
                    "gecos": rec.get("gecos", ""),
                    "home": rec["home"],
                    "shell": rec["shell"]
                })
        return jsonify({"users": users}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@accounts_bp.route("/users/<username>", methods=["GET"])
def get_user(username: str):
    """
    특정 사용자 상세 정보 조회

    사용자의 기본 계정 정보와 소속 그룹 정보를 반환합니다.

    조회 정보

    - UID
    - GID
    - 홈 디렉토리
    - 쉘
    - primary group
    - supplementary groups

    ---
    tags:
    - Accounts

    summary: 사용자 상세 정보 조회

    parameters:

      - in: path
        name: username
        required: true
        type: string
        description: 조회할 사용자 이름
        example: user2100

    responses:

      200:
        description: 사용자 정보 반환
        schema:
          type: object
          properties:
            user:
              type: object
              properties:
                name:
                  type: string
                  example: user2100
                uid:
                  type: integer
                  example: 2100
                gid:
                  type: integer
                  example: 2100
                home:
                  type: string
                  example: /home/user2100
                shell:
                  type: string
                  example: /bin/bash
            groups:
              type: array
              items:
                type: object
                properties:
                  name:
                    type: string
                    example: developers
                  gid:
                    type: integer
                    example: 3001
                  type:
                    type: string
                    example: supplementary
      404:
        description: 사용자 없음
        schema:
          $ref: '#/definitions/ErrorResponse'
      500:
        description: 서버 오류
        schema:
          $ref: '#/definitions/ErrorResponse'
    """
    try:
        # Find user in passwd
        lines = read_passwd_lines()
        user_rec = None
        for line in lines:
            rec = parse_passwd_line(line)
            if rec and rec["name"] == username:
                user_rec = rec
                break

        if not user_rec:
            return jsonify({"error": "user not found"}), 404

        # Get group memberships
        g_lines = read_group_lines()
        groups = []

        for gl in g_lines:
            grec = parse_group_line(gl)
            if not grec:
                continue

            # Primary group
            if grec["gid"] == user_rec["gid"]:
                groups.append({
                    "name": grec["name"],
                    "gid": grec["gid"],
                    "type": "primary"
                })
            # Supplementary groups
            elif username in grec.get("members", []):
                groups.append({
                    "name": grec["name"],
                    "gid": grec["gid"],
                    "type": "supplementary"
                })

        return jsonify({
            "user": {
                "name": user_rec["name"],
                "uid": user_rec["uid"],
                "gid": user_rec["gid"],
                "gecos": user_rec.get("gecos", ""),
                "home": user_rec["home"],
                "shell": user_rec["shell"]
            },
            "groups": groups
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500





# ---------- 계정 생성 단계 (v2.0) ----------
# 순서: 계정(passwd·group·shadow·sudoers) → NAS 홈 → Kerberos principal









@accounts_bp.route("/users", methods=["PUT"])
def create_user():
    """
    사용자 생성 API

    시스템에 새로운 Linux 사용자를 생성합니다.

    생성 대상 파일

    - /etc/passwd
    - /etc/shadow
    - /etc/group

    ---
    tags:
    - Accounts

    summary: 사용자 생성

    consumes:
    - application/json

    parameters:

      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - name
            - passwd_base64
          properties:
            name:
              type: string
              description: 사용자 이름
              example: user2100
            passwd_base64:
              type: string
              description: Base64 인코딩된 평문 패스워드
              example: "cGFzc3dvcmQ="
            gecos:
              type: string
              example: "GPU User"
            primary_group_name:
              type: string
              example: user2100
            supplementary_groups:
              type: array
              description: 추가 소속 그룹 목록 (없으면 생략 가능)
              items:
                type: object
                properties:
                  name:
                    type: string
                    example: ailab
                  gid:
                    type: integer
                    example: 2001

    responses:

      201:
        description: 사용자 생성 성공
      400:
        description: 필수 필드 누락
      409:
        description: 사용자 이미 존재
      500:
        description: 서버 오류
    """
    data = request.get_json(force=True)
    required = ["name", "passwd_base64"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400

    name = data["name"]
    pg_name = data.get("primary_group_name", name)
    supp_groups = data.get("supplementary_groups", [])

    for sg in supp_groups:
        if not isinstance(sg, dict) or "name" not in sg or "gid" not in sg:
            return jsonify({"error": "supplementary_groups must be list of {name, gid}"}), 400

    try:
        plaintext_pw = base64.b64decode(data["passwd_base64"], validate=True).decode("utf-8")
    except Exception:
        return jsonify({"error": "invalid passwd_base64"}), 400

    # admin_be는 /create-pod와 같은 신청 PK를 request_id로 보낸다. 그래야 한 승인의 계정
    # 생성 이력과 Pod 생성 이력이 하나의 request_id로 묶인다. 값이 없는 호출(직접 호출 등)은
    # 이 계정생성 호출 하나만을 묶는 임시 키로 기록한다.
    request_id = data.get("request_id") or f"{name}-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:-3]}"

    ctx = {
        "request_id": request_id,
        "name": name,
        "pg_name": pg_name,
        "supp_groups": supp_groups,
        "gecos": data.get("gecos", ""),
        "plaintext_pw": plaintext_pw,
    }
    try:
        for step in ACCOUNT_CREATE_STEPS:
            step(ctx)
    except StepFailed as e:
        return jsonify(e.body), e.status

    return jsonify({
        "status": "created",
        "user": ctx["entry"],
        "group": {"name": pg_name, "gid": ctx["gid"]},
        "supplementary_groups": ctx["added_supp"],
        "sudoers": ctx["s_path"],
    }), 201


# ---------- 계정 회수 단계 (v2.0) ----------
# 순서: 계정(passwd·shadow·group) → NAS 홈 → Kerberos principal·keytab
# 동기 계정 삭제(DELETE /accounts/users)는 세 단계를 모두 거친다. 제어기의 회수 작업은 보존 대상인
# 홈을 지우지 않으므로 홈 단계를 빼고 실행한다(논문 REVOKED 정의, 계획서 v2.0).











@accounts_bp.route("/users/<username>", methods=["DELETE"])
def delete_user(username: str):
    """
    사용자 삭제 API

    다음 정보를 제거합니다.

    - /etc/passwd
    - /etc/shadow
    - /etc/group

    ---
    tags:
    - Accounts

    summary: 사용자 삭제

    parameters:

      - in: path
        name: username
        required: true
        type: string
        example: user2100
      - in: query
        name: node_name
        required: false
        type: string
        description: >
          이번 삭제가 실제로 정리해야 하는 farm 노드. 주면 그 노드만 KRB5 정리를 시도한다.
          안 주면(하위 호환) 예전처럼 설정된 모든 farm 노드를 훑는데, 이러면 이번 계정과
          무관한 farm에 살아있는 동일 이름 레거시 계정까지 잘못 건드릴 수 있으니, 어느
          노드에 배포했는지 아는 호출자는 반드시 넘겨야 한다.
        example: farm2
      - in: query
        name: request_id
        required: false
        type: string
        description: >
          이 회수를 유발한 승인 번호. 작업 이력을 승인 1건 단위로 묶는 키이므로,
          아는 호출자는 반드시 넘겨야 한다. 안 주면 생성 쪽 이력과 조인할 수 없는
          임시 키로 기록된다. 계정은 신청이 아니라 웹 계정에 귀속되어 있어
          호출자가 승인 번호를 특정하지 못하는 경우가 있다.
        example: "4821"

    responses:

      200:
        description: 삭제 성공
        schema:
          type: object
          properties:
            status:
              type: string
              example: deleted
      404:
        description: 사용자 없음
      500:
        description: 서버 오류
    """
    node_name = request.args.get("node_name")
    # 회수 이력도 생성 쪽(/create-pod, PUT /accounts/users)과 같은 키로 묶는다. 그래야 한
    # 승인의 생성부터 회수까지가 하나의 request_id로 조회되고 회수 소요시간이 나온다.
    # 계정은 신청이 아니라 웹 계정에 귀속되어 있어 호출자가 승인 번호를 특정하지 못하는
    # 경우가 있는데, 그때는 생성 이력과 조인되지 않는 임시 키로 떨어진다.
    request_id = request.args.get("request_id") or f"{username}-DELETE-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:-3]}"

    ctx = {"request_id": request_id, "username": username, "node_name": node_name}
    try:
        for step in ACCOUNT_DELETE_STEPS:
            step(ctx)
    except StepFailed as e:
        return jsonify(e.body), e.status

    return jsonify({"status": "deleted", "user": username})


@accounts_bp.route("/groups/<groupname>", methods=["DELETE"])
def delete_group(groupname: str):
    """
    그룹 삭제 API

    특정 Linux 그룹을 삭제합니다.

    주의

    - 해당 그룹이 사용자 primary group이면 삭제 불가

    ---
    tags:
    - Accounts

    summary: 그룹 삭제

    parameters:

      - in: path
        name: groupname
        required: true
        type: string
        example: developers

    responses:

      200:
        description: 삭제 성공
      400:
        description: primary group 사용 중
      404:
        description: 그룹 없음
    """
    # Check if group exists
    g_lines = read_group_lines()
    group_found = None
    new_lines = []
    
    for line in g_lines:
        rec = parse_group_line(line)
        if rec and rec["name"] == groupname:
            group_found = rec
            continue
        new_lines.append(line)
    
    if not group_found:
        return jsonify({"error": "group not found"}), 404
    
    # Check if this group is used as primary group by any user
    passwd_lines = read_passwd_lines()
    users_with_primary_gid = []
    for line in passwd_lines:
        user_rec = parse_passwd_line(line)
        if user_rec and user_rec["gid"] == group_found["gid"]:
            users_with_primary_gid.append(user_rec["name"])
    
    if users_with_primary_gid:
        return jsonify({
            "error": f"Cannot delete group {groupname}: it is the primary group for users: {', '.join(users_with_primary_gid)}"
        }), 400
    
    # Remove group
    write_group_lines(new_lines)
    
    return jsonify({
        "status": "deleted", 
        "group": groupname,
        "gid": group_found["gid"]
    })

# ----------- Group management -----------
@accounts_bp.route("/groups", methods=["PUT"])
def add_group():
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
          type: object
          required:
            - name
          example:
            name: developers
            members:
              - user2100
              - user2101
          properties:
            name:
              type: string
              example: developers
            gid:
              type: integer
              description: 생략 시 /kube_share/group 기준으로 자동 할당
            members:
              type: array
              items:
                type: string
              example:
                - user2100
                - user2101

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
    data = request.get_json(force=True)
    required = ["name"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400

    name = data["name"]
    members = data.get("members", [])

    gid = None
    gid_raw = data.get("gid")
    if gid_raw not in (None, ""):
        if isinstance(gid_raw, bool):
            return jsonify({"error": "gid must be an integer"}), 400
        if isinstance(gid_raw, int):
            gid = gid_raw
        elif isinstance(gid_raw, str):
            try:
                gid = int(gid_raw)
            except ValueError:
                return jsonify({"error": "gid must be an integer"}), 400
        else:
            return jsonify({"error": "gid must be an integer"}), 400

    if not isinstance(members, list):
        return jsonify({"error": "members must be a list"}), 400

    # Validate that all members exist as users
    if members:
        passwd_lines = read_passwd_lines()
        existing_users = {parse_passwd_line(l)["name"] for l in passwd_lines if parse_passwd_line(l)}
        invalid_members = [m for m in members if m not in existing_users]
        if invalid_members:
            return jsonify({"error": f"invalid members (users not found): {', '.join(invalid_members)}"}), 400

    ensure_etc_layout()
    with LockedFile(app.config["GROUP_PATH"], "r+") as f:
        g_lines = f.read().splitlines()

        if any((parse_group_line(gl) or {}).get("name") == name for gl in g_lines):
            return jsonify({"error": f"group already exists (name: {name})"}), 409

        if gid is None:
            gid = _allocate_next_gid(g_lines, min_gid=UID_MIN)
            if UID_MAX is not None and gid > UID_MAX:
                return jsonify({"error": f"gid range {UID_MIN}~{UID_MAX} exhausted"}), 500
        elif any((parse_group_line(gl) or {}).get("gid") == gid for gl in g_lines):
            return jsonify({"error": f"group already exists (gid: {gid})"}), 409

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
@accounts_bp.route("/users/<username>/groups", methods=["PUT"])
def add_user_groups(username: str):
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
          type: object
          properties:
            groups:
              type: array
              items:
                type: string
              example:
                - developers
                - ai-lab

    responses:

      200:
        description: 그룹 추가 성공
      404:
        description: 사용자 또는 그룹 없음
      400:
        description: groups 필드 누락
    """
    data = request.get_json(force=True)
    groups = data.get("groups") or []
    if not groups:
        return jsonify({"error": "'groups' list is required"}), 400

    # Verify user exists and capture their name
    user_found = False
    for line in read_passwd_lines():
        rec = parse_passwd_line(line)
        if rec and rec["name"] == username:
            user_found = True
            break
    if not user_found:
        return jsonify({"error": "user not found"}), 404

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
        return jsonify({"error": f"groups not found: {', '.join(missing)}"}), 404

    write_group_lines(new_lines)
    return jsonify({"status": "updated", "user": username, "groups": sorted(list(names))})

# Register the blueprint under /accounts
app.register_blueprint(accounts_bp, url_prefix="/accounts")

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
    # "definitions": {}  # 정의가 없어도 에러 안 나도록 빈 객체 추가
    "definitions": {

        "CreatePodRequest": {
            "type": "object",
            "required": ["username"],
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Pod를 생성할 사용자 이름",
                    "example": "user2100"
                }
            }
        },

        "CreatePodResponse": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "example": "created"
                },
                "node": {
                    "type": "string",
                    "example": "...RTX 3080..."
                },
                "pod_name": {
                    "type": "string",
                    "example": "ailab-user2100-1"
                },
                "ports": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "internal_port": {
                                "type": "integer",
                                "example": 22
                            },
                            "external_port": {
                                "type": "integer",
                                "example": 30001
                            },
                            "usage_purpose": {
                                "type": "string",
                                "example": "ssh"
                            }
                        }
                    }
                }
            }
        },

        "DeletePodRequest": {
            "type": "object",
            "required": ["pod_name"],
            "properties": {
                "pod_name": {
                    "type": "string",
                    "example": "ailab-user2100-1"
                }
            }
        },

        "ErrorResponse": {
            "type": "object",
            "properties": {
                "error": {
                    "type": "string",
                    "example": "username required"
                }
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
)
from lifecycle_steps.revoke import (  # noqa: E402
    step_delete_services, step_release_nodeports, step_delete_pod_k8s,
    step_cleanup_pod_node_krb5, _new_delete_rollback, POD_DELETE_STEPS,
    step_check_account_revocable, step_delete_account, step_delete_home,
    step_remove_krb5, ACCOUNT_DELETE_STEPS,
)

# 작업 실행 엔진은 application/jobs.py로 이동했다(얇은 이동). 아래 재수출은 기존 소비자
# (라우트·제어기·테스트의 main.* 참조)를 무수정으로 유지한다.
from application.jobs import (  # noqa: E402
    STEP_MAX_ATTEMPTS, RETRY_DELAY_SEC, STEP_OBSERVERS, RERUN_SAFE, PRE_STEP,
    ALWAYS_RERUN, DEFER_DONE, SAVED_CTX_KEYS, _saved_ctx, _StepDegraded, _execute_step,
    JOB_ACTIONS, _JOB_KIND, _job_steps, _job_ctx, find_unfinished_jobs, _finish_job,
    _record_job_result, _compensate_provision, run_job, _release_lease, _run_job,
    _observe_account_created, _observe_krb5_principal, _observe_pod_created,
)


def _job_request_id(value):
    """작업 이력을 admin_be 신청 기록과 조인하는 키이자 사용자 설정 조회 키라 신청 PK(양의 정수)만 받는다."""
    text = str(value).strip() if value is not None and not isinstance(value, bool) else ""
    return str(int(text)) if text.isdigit() and int(text) > 0 else None


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

    if kind == "provision":
        set_pod_creation_status(request_id, "started", "요청 접수")
    app.logger.info(f"[JOB] registered {kind} request_id={request_id} job_id={job_id} username={username}")
    return jsonify({"request_id": request_id, "job_id": job_id, "status": "accepted"}), 202


@app.route("/operations/provision", methods=["POST"])
def register_provision():
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
          type: object
          required: [request_id, username]
          properties:
            request_id: {type: integer, example: 4821, description: admin_be 신청 번호}
            username: {type: string, example: exp-np-001}
            account:
              type: object
              description: PUT /accounts/users와 같은 필드(name 제외)
              properties:
                passwd_base64: {type: string}
                gecos: {type: string}
                primary_group_name: {type: string}
                supplementary_groups: {type: array, items: {type: object}}
    responses:
      202: {description: 등록됨}
      400: {description: 입력 오류}
      409: {description: 같은 신청의 생성 작업이 아직 끝나지 않음}
    """
    data = request.get_json(force=True) or {}
    request_id, username = _job_request_id(data.get("request_id")), data.get("username")
    if not request_id or not username:
        return jsonify(infra_error("VALIDATE_REQUEST", "INVALID_PROVISION_REQUEST",
                                   "request_id(admin_be 신청 번호, 양의 정수) and username required")), 400

    job = {"username": username}
    account = data.get("account")
    if account is not None:
        if not isinstance(account, dict) or "passwd_base64" not in account:
            return jsonify({"error": "missing fields: account.passwd_base64"}), 400
        supp_groups = account.get("supplementary_groups", [])
        for sg in supp_groups:
            if not isinstance(sg, dict) or "name" not in sg or "gid" not in sg:
                return jsonify({"error": "supplementary_groups must be list of {name, gid}"}), 400
        try:
            plaintext_pw = base64.b64decode(account["passwd_base64"], validate=True).decode("utf-8")
        except Exception:
            return jsonify({"error": "invalid passwd_base64"}), 400
        job["account"] = {
            "pg_name": account.get("primary_group_name", username),
            "supp_groups": supp_groups,
            "gecos": account.get("gecos", ""),
            "passwd_hash": crypt.crypt(plaintext_pw, crypt.mksalt(crypt.METHOD_SHA512)),
        }
    return _register_job("provision", str(request_id), username, job)


@app.route("/operations/revoke", methods=["POST"])
def register_revoke():
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
          type: object
          required: [request_id]
          properties:
            request_id: {type: integer, example: 4821, description: admin_be 신청 번호}
            pod_name: {type: string, example: ailab-exp-np-001-7f3a9c21}
            username: {type: string, description: pod_name이 없을 때 필요}
            node_name: {type: string, description: keytab을 지울 노드. 없으면 지운 Pod의 노드}
            delete_account: {type: boolean, default: false}
    responses:
      202: {description: 등록됨}
      400: {description: 입력 오류}
      409: {description: 같은 신청의 회수 작업이 아직 끝나지 않음}
    """
    data = request.get_json(force=True) or {}
    request_id = _job_request_id(data.get("request_id"))
    pod_name = data.get("pod_name")
    delete_account = bool(data.get("delete_account"))
    if pod_name and not str(pod_name).startswith("ailab-"):
        return jsonify(infra_error("VALIDATE_REQUEST", "INVALID_POD_NAME", "invalid pod_name")), 400
    username = data.get("username") or (_pod_username(pod_name) if pod_name else None)
    if not request_id or not username or not (pod_name or delete_account):
        return jsonify(infra_error("VALIDATE_REQUEST", "INVALID_REVOKE_REQUEST",
                                   "request_id(admin_be 신청 번호, 양의 정수) and pod_name, "
                                   "or username with delete_account, required")), 400

    job = {"username": username, "pod_name": pod_name, "node_name": data.get("node_name"),
           "delete_account": delete_account}
    return _register_job("revoke", str(request_id), username, job)


@app.route("/operations/<kind>/<request_id>", methods=["GET"])
def get_job_result(kind, request_id):
    """
    작업 결과 조회 (v2.0)

    phase: none(등록 이력 없음) / START(대기·실행 중) / SUCCESS / FAIL / UNKNOWN

    성공한 작업은 만든 자원을 result로 함께 돌려준다(생성: 계정 uid·gid, Pod 이름·노드, 외부 포트
    목록 — 포트는 /create-pod 응답과 같은 형식). 하루가 지나면 result는 사라지고 phase만 남는다.
    ---
    tags:
    - Operations
    parameters:
      - {in: path, name: kind, required: true, type: string, enum: [provision, revoke]}
      - {in: path, name: request_id, required: true, type: string}
    responses:
      200: {description: 조회 성공}
      404: {description: 알 수 없는 작업 종류}
    """
    action = JOB_ACTIONS.get(kind)
    if action is None:
        return jsonify({"error": f"unknown job kind {kind!r}"}), 404
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
















if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
