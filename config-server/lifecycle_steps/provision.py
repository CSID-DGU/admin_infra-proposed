"""생성 단계 — 계정·홈·principal·노드포트·Pod·Service

main.py에서 얇게 이동한 코드다. main 소속 헬퍼·설정은 테스트가 main.*를 대역으로 바꾸는
계약을 지키기 위해 지연 프록시(_main)로 호출 시점에 바인딩한다.
"""
import base64
import crypt
import json
import os
import re
import subprocess
import time

import requests
import urllib3
from kubernetes import client

from typing import List, Optional

from adapters.job_control import LeaseLost
from adapters.operation_log import Action, Phase
from request_models import SHA512_CRYPT_RE


class _MainProxy:
    def __getattr__(self, name):
        import main
        return getattr(main, name)

    def __setattr__(self, name, value):
        # 쓰기도 main 모듈로 위임한다. 이게 없으면 _main.X = ... 가 프록시 인스턴스에만
        # 저장돼 main 의 상태(_last_reconcile_ts 등)와 이원화된다(#57).
        import main
        setattr(main, name, value)


_main = _MainProxy()


def reconcile_nodeport_allocations(namespace: str) -> int:
    """
    MySQL의 nodeport_allocations 테이블과 실제 k8s NodePort Service 상태를 동기화.

    문제 상황:
        - NodePort 할당 정보는 MySQL에 저장되고, 실제 Service는 k8s(etcd)에 존재.
        - config-server를 거치지 않고 Service가 삭제되거나 (kubectl delete svc 등),
          config-server 비정상 종료로 release_nodeports()가 호출되지 않으면
          MySQL에 포트가 점유된 채로 남아 포트 고갈 발생 가능.

    동기화 방향: k8s -> MySQL  (k8s가 단일 진실 소스)
        - k8s에 실제 존재하는 NodePort Service의 pod_name 목록 조회.
        - MySQL에는 있지만 k8s에 없는 pod_name 행을 stale로 판단해 삭제.

    Args:
        namespace: NodePort Service가 존재하는 k8s 네임스페이스

    Returns:
        int: 삭제된 stale 행 수 (0이면 동기화 불필요 또는 쓰로틀로 스킵)
    """
    # 쓰로틀 상태(_last_reconcile_ts)는 main 모듈이 소유한다. 이 함수가 main.py에서 이 모듈로
    # 옮겨질 때(#49) _RECONCILE_INTERVAL_SEC 는 _main. 참조로 고쳐졌지만 _last_reconcile_ts 는
    # 누락돼, 자기 모듈에 없는 이름을 global로 읽어 NameError 로 NodePort 예약이 전면 실패했다(#57).
    # ── 쓰로틀 체크: 마지막 실행으로부터 _RECONCILE_INTERVAL_SEC 이내면 스킵 ──
    now = time.time()
    elapsed = now - _main._last_reconcile_ts
    if elapsed < _main._RECONCILE_INTERVAL_SEC:
        _main.app.logger.debug(
            f"[RECONCILE] skipped (throttle: {int(_main._RECONCILE_INTERVAL_SEC - elapsed)}s remaining)"
        )
        return 0

    _main.app.logger.info(f"[RECONCILE] start namespace={namespace}")
    # 쓰로틀 기준 시각: 성공/실패와 무관하게 "시도" 단위로 갱신한다.
    _main._last_reconcile_ts = time.time()

    # ── 1. k8s에서 실제 살아있는 NodePort Service의 pod_name 집합 조회 ──
    #    label_selector로 config-server가 관리하는 Service만 필터링.
    #    (app=ailab-nodeport 라벨은 create_nodeport_services()에서 부여)
    _main.load_k8s()  # utils.load_k8s — main.py 상단 import에서 가져옴
    v1 = client.CoreV1Api()

    try:
        services = v1.list_namespaced_service(
            namespace=namespace,
            label_selector="app=ailab-nodeport"
        )
    except Exception as e:
        # k8s API 실패 시 reconcile 스킵. 포트 할당은 계속하고 다음 주기에 재시도.
        _main.app.logger.warning("[RECONCILE] k8s API call failed, skipping reconcile: %s", e, exc_info=True)
        return 0

    # Service 메타데이터의 pod_name 라벨에서 살아있는 pod 이름 수집
    live_pod_names = {
        svc.metadata.labels["pod_name"]
        for svc in services.items
        if svc.metadata.labels and "pod_name" in svc.metadata.labels
    }
    _main.app.logger.debug(f"[RECONCILE] live pods in k8s: {live_pod_names}")

    # ── 2. MySQL에서 현재 점유 중인 pod_name 목록 조회 ──
    conn = _main.get_db_connection()
    deleted_count = 0

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT pod_name FROM nodeport_allocations")
            db_pod_names = {row[0] for row in cur.fetchall()}
            _main.app.logger.debug(f"[RECONCILE] pods in MySQL: {db_pod_names}")

            # k8s에는 없지만 MySQL에는 남아있는 stale pod_name 계산
            stale_pod_names = db_pod_names - live_pod_names

            if not stale_pod_names:
                _main.app.logger.info("[RECONCILE] no stale entries, DB is in sync")
                return 0

            _main.app.logger.info(f"[RECONCILE] stale pods to remove: {stale_pod_names}")

            # stale pod의 모든 NodePort 할당 행을 삭제
            for pod in stale_pod_names:
                cur.execute(
                    "DELETE FROM nodeport_allocations WHERE pod_name=%s",
                    (pod,)
                )
                deleted_count += cur.rowcount
                _main.app.logger.info(f"[RECONCILE] removed {cur.rowcount} rows for stale pod={pod}")

        conn.commit()
        _main.app.logger.info(f"[RECONCILE] done, total deleted={deleted_count}")
        return deleted_count

    except Exception:
        _main.app.logger.exception("[RECONCILE] failed, rolling back")
        conn.rollback()
        # reconcile 실패는 non-fatal. allocate_nodeports()는 stale 제거 없이 계속 진행.
        return 0
    finally:
        conn.close()

def get_cluster_reserved_nodeports() -> set:
    """
    클러스터 전체(모든 네임스페이스)에서 이미 점유 중인 NodePort 집합 조회.

    nodeport_allocations 테이블에는 이 서비스가 직접 할당한 포트만 기록되므로,
    고정 NodePort로 배포된 자기 자신이나 수동으로 생성된 Service가 점유한
    포트는 DB만 봐서는 알 수 없다. 그런 포트가 available로 잘못 계산되면
    이후 Service 생성 단계에서 "already allocated"로 실패한다.
    """
    _main.load_k8s()
    v1 = client.CoreV1Api()
    reserved = set()
    for svc in v1.list_service_for_all_namespaces().items:
        for port in svc.spec.ports or []:
            if port.node_port:
                reserved.add(port.node_port)
    return reserved

def allocate_nodeports(username, pod_name, node_name, ports):
    """
    ports:
    [
        {"internal_port": 22, "usage_purpose": "ssh"},
        {"internal_port": 8888, "usage_purpose": "jupyter"},
        ...
    ]
    """
    _main.app.logger.info(f"[NODEPORT] allocate start username={username} pod={pod_name} node={node_name}")
    _main.app.logger.debug(f"[NODEPORT] requested ports={ports}")

    # 포트 할당 전에 MySQL과 k8s 실제 상태를 동기화한다.
    # stale 행이 정리되어야 available 포트 계산이 정확해짐/
    # 5분 쓰로틀 적용 — in-flight pod 오탐 방지 및 k8s/DB 부하 줄어듬
    _main.reconcile_nodeport_allocations(namespace=_main.app.config["NAMESPACE"])

    conn = _main.get_db_connection() #DB 연결

    try:
        with conn.cursor() as cur: #DB 커서 생성 (python pymysql 라이브러리)

            cur.execute("SELECT node_port FROM nodeport_allocations FOR UPDATE")
            used = {row[0] for row in cur.fetchall()}

            try:
                used |= _main.get_cluster_reserved_nodeports()
            except Exception:
                _main.app.logger.warning(
                    "[NODEPORT] failed to query live k8s nodeport usage, "
                    "falling back to DB-only availability check",
                    exc_info=True,
                )

            _main.app.logger.debug(f"[NODEPORT] used ports count={len(used)}")
            available = [
                p for p in range(_main.NODEPORT_MIN, _main.NODEPORT_MAX + 1)
                if p not in used
            ]

            _main.app.logger.debug(f"[NODEPORT] available ports count={len(available)}")

            if len(available) < len(ports):
                raise ValueError("Not enough NodePorts")

            result_ports = []

            for idx, port in enumerate(ports):
                _main.app.logger.debug(f"[NODEPORT] assigning internal_port={port['internal_port']}")

                node_port = available[idx]
                _main.app.logger.info(f"[NODEPORT] allocated {port['internal_port']} -> {node_port}")

                cur.execute("""
                    INSERT INTO nodeport_allocations
                    (username, pod_name, node_name, internal_port, node_port, purpose)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, (
                    username,
                    pod_name,
                    node_name,
                    port["internal_port"],
                    node_port,
                    port.get("usage_purpose", "custom")
                ))

                result_ports.append({
                    "internal_port": port["internal_port"],
                    "external_port": node_port,
                    "usage_purpose": port.get("usage_purpose", "custom")
                })
            _main.app.logger.info(f"[NODEPORT] allocation success total={len(result_ports)}")
            conn.commit() #Commit changes to stable storage.
            return result_ports #Return the allocated ports.

    except ValueError:
        conn.rollback()
        raise
    except Exception:
        _main.app.logger.exception(f"[NODEPORT] allocation failed pod={pod_name}")
        conn.rollback()
        raise
    finally:
        conn.close()

def release_nodeports(pod_name):
    _main.app.logger.info(f"[NODEPORT] release start pod={pod_name}")
    conn = _main.get_db_connection()
    try:
        with conn.cursor() as cur:
            _main.app.logger.debug(f"[NODEPORT] deleting DB rows for pod={pod_name}")

            cur.execute(
                "DELETE FROM nodeport_allocations WHERE pod_name=%s",
                (pod_name,)
            )

        conn.commit()
        _main.app.logger.info(f"[NODEPORT] release complete pod={pod_name}")
    except Exception:
        _main.app.logger.exception(f"[NODEPORT] release failed pod={pod_name}")
        raise
    finally:
        conn.close()

def account_secret_name(pod_name):
    return f"{pod_name}-account"


class LoginPasswordMissing(ValueError):
    """컨테이너에 줄 로그인 비밀번호가 없다. 이대로 만들면 시작 스크립트가 공개된 기본 비밀번호를 쓴다."""


def decode_login_password(passwd_base64):
    try:
        password = base64.b64decode(passwd_base64 or "", validate=True).decode("utf-8")
    except Exception as e:
        raise LoginPasswordMissing("로그인 비밀번호 형식이 올바르지 않음") from e
    if not password:
        raise LoginPasswordMissing("로그인 비밀번호가 비어 있음(승인 완료 뒤 지워졌을 수 있음)")
    return password


def hash_login_password(password):
    return crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))


def checked_login_password_hash(passwd_hash):
    if not passwd_hash:
        raise LoginPasswordMissing("로그인 비밀번호 해시가 비어 있음(승인 완료 뒤 지워졌을 수 있음)")
    if not SHA512_CRYPT_RE.match(passwd_hash):
        raise LoginPasswordMissing("로그인 비밀번호 해시 형식이 올바르지 않음")
    return passwd_hash


def login_password_hash(info):
    """사용자 설정(admin_be)의 로그인 비밀번호 해시. admin_be는 해시(passwd_hash)만 보내고,
    그 이전 버전은 평문(passwd_base64)을 보내므로 그때는 여기서 해시한다."""
    info = info or {}
    if info.get("passwd_hash"):
        return checked_login_password_hash(info["passwd_hash"])
    return hash_login_password(decode_login_password(info.get("passwd_base64")))


def login_password_for_recreate(v1, ns, old_pod_name, user_info):
    """다시 만드는 Pod(마이그레이션)의 비밀번호 해시. 옛 Pod의 Secret을 먼저 쓰고, 없으면 신청 설정 값을 쓴다.
    해시 도입 전에 만든 Secret은 평문(USER_PW)이라 이어받으면서 해시로 바꾼다."""
    try:
        secret = v1.read_namespaced_secret(account_secret_name(old_pod_name), ns)
        data = secret.data or {}
        if data.get("USER_PW_HASH"):
            return checked_login_password_hash(base64.b64decode(data["USER_PW_HASH"]).decode("utf-8"))
        if data.get("USER_PW"):
            return hash_login_password(decode_login_password(data["USER_PW"]))
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
    return login_password_hash(user_info)


def ensure_account_secret(v1, ns, pod_name, username, passwd_hash):
    """Pod가 읽을 로그인 비밀번호 해시 Secret을 만든다(있으면 새 값으로 바꾼다). 해시가 비거나 형식이 다르면 만들지 않는다."""
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=account_secret_name(pod_name), namespace=ns,
                                     labels={"app": "ailab-account", "ailab.dgu/pod": pod_name, "username": username}),
        type="Opaque",
        string_data={"USER_PW_HASH": checked_login_password_hash(passwd_hash)},
    )
    try:
        v1.create_namespaced_secret(namespace=ns, body=body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
        v1.replace_namespaced_secret(account_secret_name(pod_name), ns, body)


def own_account_secret(v1, ns, pod_name, created_pod):
    """Secret의 소유자를 Pod로 지정해, Pod가 어떤 경로로 지워져도 쿠버네티스가 Secret을 함께 지우게 한다."""
    uid = getattr(getattr(created_pod, "metadata", None), "uid", None)
    if not uid:
        return
    try:
        v1.patch_namespaced_secret(account_secret_name(pod_name), ns, {"metadata": {"ownerReferences": [
            {"apiVersion": "v1", "kind": "Pod", "name": pod_name, "uid": uid, "blockOwnerDeletion": False}]}})
    except Exception:
        _main.app.logger.warning("[ACCOUNT SECRET] 소유자 지정 실패 — 회수 단계가 직접 지운다: %s", pod_name, exc_info=True)


def delete_account_secret(v1, ns, pod_name):
    try:
        v1.delete_namespaced_secret(account_secret_name(pod_name), ns)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            _main.app.logger.warning("[ACCOUNT SECRET] 삭제 실패: %s", pod_name, exc_info=True)
    except Exception:
        _main.app.logger.warning("[ACCOUNT SECRET] 삭제 실패: %s", pod_name, exc_info=True)


# 기동에 실패한 컨테이너에서 남겨 둘 로그 분량. 실패 원인은 마지막 몇 줄에 나오므로 짧게 잡는다.
POD_FAILURE_LOG_LINES = 30
POD_FAILURE_LOG_CHARS = 2000


# 단계가 실패하면서 앞 단계의 자원까지 정리했을 때 재시도를 다시 시작할 단계.
# Pod 쪽 정리는 포트 배정을 반환하므로 배정하는 단계부터, 계정 쪽 정리는 원장 계정을 지우므로
# 계정 생성부터 다시 한다.
POD_RESTART_STEP = "step_build_pod_spec"
ACCOUNT_RESTART_STEP = "step_create_account"


def _cleanup_create_failure(pod_name, v1=None, delete_services=False):
    ns = _main.app.config["NAMESPACE"]
    rollback = {
        "nodeportsReleased": False,
        "podDeleted": False,
        "servicesDeleted": False,
    }

    if delete_services:
        try:
            _main.delete_nodeport_services(pod_name, ns)
            rollback["servicesDeleted"] = True
        except Exception:
            _main.app.logger.warning("[CREATE POD] cleanup service deletion failed", exc_info=True)

    try:
        _main.release_nodeports(pod_name)
        rollback["nodeportsReleased"] = True
    except Exception:
        _main.app.logger.warning("[CREATE POD] cleanup nodeport release failed", exc_info=True)

    if v1 is not None:
        # Pod를 지우면 컨테이너 로그도 함께 사라진다. 기동에 실패한 이유는 그 로그에만 남으므로
        # 지우기 전에 읽어 둔다. 읽지 못해도 정리는 그대로 진행한다.
        try:
            log_tail = v1.read_namespaced_pod_log(pod_name, ns, tail_lines=POD_FAILURE_LOG_LINES)
            if log_tail and log_tail.strip():
                rollback["podLogTail"] = log_tail.strip()[-POD_FAILURE_LOG_CHARS:]
        except Exception:
            _main.app.logger.warning("[CREATE POD] cleanup pod log read failed", exc_info=True)

        try:
            v1.delete_namespaced_pod(pod_name, ns)
            rollback["podDeleted"] = True
        except client.exceptions.ApiException as e:
            if e.status == 404:
                rollback["podDeleted"] = True
            else:
                _main.app.logger.warning("[CREATE POD] cleanup pod deletion failed", exc_info=True)
        except Exception:
            _main.app.logger.warning("[CREATE POD] cleanup pod deletion failed", exc_info=True)
        delete_account_secret(v1, ns, pod_name)

    return rollback

def step_fetch_user_config(ctx):
    request_id, username = ctx["request_id"], ctx["username"]
    if ctx.get("config_by_request"):
        # 제어기 경로: 처리 중인 그 신청 하나의 설정을 신청 번호로 조회한다. 사용자명 조회는 열린 신청이
        # 여럿이면 가장 최근 것을 골라 다른 신청의 설정을 가져올 수 있다. 동기 경로(baseline)는 그대로 둔다.
        was_url = f"{_main.ADMIN_BE_INTERNAL_URL}/api/requests/config/by-request/{request_id}"
    else:
        was_url = _main.app.config["WAS_URL_TEMPLATE"].format(username=username)
    _main.app.logger.info(f"[CREATE POD] requesting user config from WAS: {was_url}")
    _main.log_operation(request_id=request_id, username=username,
                  action=Action.FETCH_USER_CONFIG, phase=Phase.START)

    resp = None
    try:
        resp = requests.get(was_url, headers=_main.admin_be_headers(),
                            timeout=_main.app.config["HTTP_TIMEOUT_SEC"])
        user_info = resp.json()
        if not isinstance(user_info, dict):
            # 아래 status 조회가 사전을 전제한다. 사전이 아니면 예상 못 한 예외로 빠지는 대신
            # 이미 있는 "응답 형식 이상" 경로를 타게 한다.
            raise ValueError(f"expected a JSON object, got {type(user_info).__name__}")
    except requests.RequestException as e:
        _main.app.logger.exception("[CREATE POD] WAS request failed")
        _main.log_operation(request_id=request_id, username=username,
                      action=Action.FETCH_USER_CONFIG, phase=_main._fail_phase(e),
                      error_code="USER_CONFIG_FETCH_FAILED", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "FETCH_USER_CONFIG",
            "USER_CONFIG_FETCH_FAILED",
            str(e),
        ), 502, cause=e)
    except ValueError as e:
        _main.app.logger.exception("[CREATE POD] invalid WAS response")
        _main.log_operation(request_id=request_id, username=username,
                      action=Action.FETCH_USER_CONFIG, phase=Phase.FAIL,
                      error_code="USER_CONFIG_INVALID_RESPONSE", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "FETCH_USER_CONFIG",
            "USER_CONFIG_INVALID_RESPONSE",
            str(e),
            was_status=resp.status_code if resp is not None else None,
        ), 502)

    # WAS가 HTTP 200 + body {"status": 404} 형태로 유저 없음을 알리는 경우 처리
    if user_info.get("status") == 404 or resp.status_code == 404:
        _main.app.logger.warning(f"[CREATE POD] user {username!r} not found in WAS")
        _main.log_operation(request_id=request_id, username=username,
                      action=Action.FETCH_USER_CONFIG, phase=Phase.FAIL,
                      error_code="USER_CONFIG_NOT_FOUND", error_detail=f"user {username!r} not found in WAS")
        raise _main.StepFailed(_main.infra_error(
            "FETCH_USER_CONFIG",
            "USER_CONFIG_NOT_FOUND",
            f"user {username!r} not found in WAS",
            was_status=resp.status_code,
        ), 404)
    if resp.status_code >= 400:
        _main.app.logger.error(f"[CREATE POD] WAS returned {resp.status_code}")
        _main.log_operation(request_id=request_id, username=username,
                      action=Action.FETCH_USER_CONFIG, phase=Phase.FAIL,
                      error_code="USER_CONFIG_FETCH_FAILED",
                      error_detail=f"WAS returned {resp.status_code} for user {username!r}")
        raise _main.StepFailed(_main.infra_error(
            "FETCH_USER_CONFIG",
            "USER_CONFIG_FETCH_FAILED",
            f"WAS returned {resp.status_code} for user {username!r}",
            was_status=resp.status_code,
        ), 502)

    _main.log_operation(request_id=request_id, username=username,
                  action=Action.FETCH_USER_CONFIG, phase=Phase.SUCCESS)
    _main.app.logger.debug(f"[CREATE POD] user_info received: {user_info}")
    ctx["user_info"] = user_info

def step_prepare_pod(ctx):
    """Pod 이름을 정하고 같은 이름의 Pod가 없는지 확인한 뒤 후보 노드 목록을 만든다."""
    username, user_info = ctx["username"], ctx["user_info"]
    ns = _main.app.config["NAMESPACE"]

    pod_name = _main.generate_pod_name(username)
    _main.app.logger.info(f"[CREATE POD] generated pod_name={pod_name}")
    ctx["pod_name"] = pod_name

    # pod_name 중복 확인
    try:
        _main.load_k8s()
        v1 = client.CoreV1Api()
    except Exception as e:
        _main.app.logger.exception("[CREATE POD] k8s client setup failed")
        raise _main.StepFailed(_main.infra_error(
            "CHECK_EXISTING_POD",
            "K8S_CLIENT_SETUP_FAILED",
            str(e),
            pod_name=pod_name,
        ), 500)

    try:
        v1.read_namespaced_pod(pod_name, ns)
        _main.app.logger.warning(f"[CREATE POD] pod already exists: {pod_name}")
        raise _main.StepFailed(_main.infra_error(
            "CHECK_EXISTING_POD",
            "POD_ALREADY_EXISTS",
            "pod already exists",
            pod_name=pod_name,
        ), 409)
    except _main.StepFailed:
        raise
    except client.exceptions.ApiException as e:
        if e.status != 404:
            _main.app.logger.exception("[CREATE POD] pod existence check failed")
            raise _main.StepFailed(_main.infra_error(
                "CHECK_EXISTING_POD",
                "POD_CHECK_FAILED",
                e.body,
                pod_name=pod_name,
                **_main.k8s_error_fields(e),
            ), 500)
        _main.app.logger.debug("[CREATE POD] pod does not exist yet")
    except Exception as e:
        _main.app.logger.exception("[CREATE POD] pod existence check failed")
        raise _main.StepFailed(_main.infra_error(
            "CHECK_EXISTING_POD",
            "POD_CHECK_FAILED",
            str(e),
            pod_name=pod_name,
        ), 500, cause=e)

    # Prometheus 기반 노드 선택
    gpu_nodes = user_info.get("gpu_nodes", [])
    node_list = [
        str(n["node_name"]).strip().lower()
        for n in gpu_nodes
        if n.get("node_name")
    ]

    # WAS가 gpu_nodes를 반환하지 않으면 k8s Ready 워커 노드 전체로 폴백
    if not node_list:
        _main.app.logger.warning("[CREATE POD] gpu_nodes missing from WAS — falling back to all ready worker nodes")
        try:
            _main.load_k8s()
            _all_nodes = client.CoreV1Api().list_node().items
        except client.exceptions.ApiException as e:
            _main.app.logger.exception("[CREATE POD] fallback node list failed")
            raise _main.StepFailed(_main.infra_error(
                "LIST_NODES",
                "NODE_LIST_FAILED",
                e.body,
                **_main.k8s_error_fields(e),
            ), 500)
        except Exception as e:
            _main.app.logger.exception("[CREATE POD] fallback node list failed")
            raise _main.StepFailed(_main.infra_error(
                "LIST_NODES",
                "NODE_LIST_FAILED",
                str(e),
            ), 500, cause=e)
        node_list = [
            n.metadata.name
            for n in _all_nodes
            if all(c.status == "True" for c in n.status.conditions if c.type == "Ready")
            and not any(
                "control-plane" in (t.key or "") and t.effect == "NoSchedule"
                for t in (n.spec.taints or [])
            )
        ]

    _main.app.logger.info(f"[CREATE POD] candidate nodes: {node_list}")
    ctx["node_list"] = node_list

def step_select_node(ctx):
    request_id, username, pod_name = ctx["request_id"], ctx["username"], ctx["pod_name"]
    _main.set_pod_creation_status(request_id, "selecting_node", "GPU 노드 선택 중")
    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  action=Action.SELECT_NODE, phase=Phase.START)

    try:
        best_node = _main.select_best_node_from_prometheus(
            ctx["node_list"],
            _main.app.config["PROM_URL"],
            _main.app.config["HTTP_TIMEOUT_SEC"]
        )
    except Exception as e:
        _main.app.logger.exception("[CREATE POD] node selection failed")
        _main.set_pod_creation_status(request_id, "failed", "노드 선택 실패")
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      action=Action.SELECT_NODE, phase=_main._fail_phase(e),
                      error_code="NODE_SELECTION_FAILED", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "SELECT_NODE",
            "NODE_SELECTION_FAILED",
            str(e),
            pod_name=pod_name,
        ), 500, cause=e)
    _main.app.logger.info(f"[CREATE POD] selected best node: {best_node}")
    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  node_name=best_node, action=Action.SELECT_NODE, phase=Phase.SUCCESS)
    ctx["node"] = best_node

def step_build_pod_spec(ctx):
    """NodePort 할당과 farm 노드 keytab 배포가 build_pod_spec 안에서 함께 일어난다."""
    request_id, username, pod_name = ctx["request_id"], ctx["username"], ctx["pod_name"]
    best_node = ctx["node"]
    _main.app.logger.info("[CREATE POD] building pod spec")
    _main.set_pod_creation_status(request_id, "building_pod_spec", f"pod spec 생성 중 (node={best_node})")

    try:
        if not best_node:
            raise ValueError(
                "no suitable node selected (check gpu_nodes and Prometheus metrics)"
            )
        spec_wrapper, allocated_ports = _main.build_pod_spec(
            username,
            ctx["user_info"],
            best_node,
            pod_name,
            request_id=request_id,
        )
    except _main.PodSpecBuildError as e:
        _main.set_pod_creation_status(request_id, "failed", "pod spec 생성 실패")
        raise _main.StepFailed(_main.infra_error(
            "BUILD_POD_SPEC",
            "POD_SPEC_BUILD_FAILED",
            str(e),
            progress=e.progress,
            pod_name=pod_name,
            # 호출자(admin_be)가 계정 삭제 보상 트랜잭션을 실행할 때 이 노드만 정리하도록
            # 넘겨주기 위함 — 없으면 대상 노드를 몰라서 전체 farm을 무차별로 훑게 된다.
            node=best_node,
        ), 500, cause=e)
    except ValueError as e:
        _main.set_pod_creation_status(request_id, "failed", "pod spec 생성 실패")
        raise _main.StepFailed(_main.infra_error(
            "BUILD_POD_SPEC",
            "POD_SPEC_BUILD_FAILED",
            str(e),
            rollback={"nodeportsReleased": False},
            pod_name=pod_name,
        ), 400)
    except Exception as e:
        _main.app.logger.exception("[CREATE POD] pod spec build failed")
        _main.set_pod_creation_status(request_id, "failed", "pod spec 생성 실패")
        raise _main.StepFailed(_main.infra_error(
            "BUILD_POD_SPEC",
            "POD_SPEC_BUILD_FAILED",
            str(e),
            rollback={"nodeportsReleased": False},
            pod_name=pod_name,
            node=best_node,
        ), 500, cause=e)
    _main.app.logger.debug(f"[CREATE POD] allocated ports: {allocated_ports}")

    ctx["pod_spec"] = spec_wrapper["config"]["kubernetes"]["pod"]
    ctx["allocated_ports"] = allocated_ports
    _main.app.logger.info("[CREATE POD] pod spec built")

def step_create_pod_k8s(ctx):
    request_id, username, pod_name = ctx["request_id"], ctx["username"], ctx["pod_name"]
    best_node = ctx["node"]
    ns = _main.app.config["NAMESPACE"]

    try:
        _main.load_k8s()
        v1 = client.CoreV1Api()
    except Exception as e:
        _main.app.logger.exception("[CREATE POD] k8s client setup failed")
        rollback = _main._cleanup_create_failure(pod_name)
        raise _main.StepFailed(_main.infra_error(
            "CREATE_POD",
            "K8S_CLIENT_SETUP_FAILED",
            str(e),
            rollback=rollback,
            pod_name=pod_name,
        ), 500, restart_from=POD_RESTART_STEP)
    ctx["v1"] = v1

    try:
        passwd_hash = login_password_hash(ctx["user_info"])
    except LoginPasswordMissing as e:
        _main.set_pod_creation_status(request_id, "failed", "로그인 비밀번호 없음")
        rollback = _main._cleanup_create_failure(pod_name)
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=best_node, resource_type="pod",
                      action=Action.CREATE_POD_K8S, phase=Phase.FAIL,
                      error_code="LOGIN_PASSWORD_MISSING", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "CREATE_POD", "LOGIN_PASSWORD_MISSING", str(e), rollback=rollback, pod_name=pod_name,
        ), 422)

    _main.app.logger.info(f"[CREATE POD] creating pod in namespace={ns}")
    _main.set_pod_creation_status(request_id, "creating_pod", f"k8s pod 생성 중 (node={best_node})")
    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  node_name=best_node, resource_type="pod",
                  action=Action.CREATE_POD_K8S, phase=Phase.START)
    try:
        ensure_account_secret(v1, ns, pod_name, username, passwd_hash)
        created = v1.create_namespaced_pod(
            namespace=ns,
            body=ctx["pod_spec"]
        )
        own_account_secret(v1, ns, pod_name, created)
    except client.exceptions.ApiException as e:
        _main.app.logger.exception("[CREATE POD] pod creation failed")
        _main.set_pod_creation_status(request_id, "failed", "pod 생성 실패")
        rollback = _main._cleanup_create_failure(pod_name, v1)
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=best_node, resource_type="pod",
                      action=Action.CREATE_POD_K8S, phase=Phase.FAIL,
                      error_code="POD_CREATE_FAILED", error_detail=str(e.body))
        raise _main.StepFailed(_main.infra_error(
            "CREATE_POD",
            "POD_CREATE_FAILED",
            e.body,
            rollback=rollback,
            pod_name=pod_name,
            **_main.k8s_error_fields(e),
        ), 500, restart_from=POD_RESTART_STEP)
    except Exception as e:
        _main.app.logger.exception("[CREATE POD] pod creation failed")
        _main.set_pod_creation_status(request_id, "failed", "pod 생성 실패")
        rollback = _main._cleanup_create_failure(pod_name, v1)
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=best_node, resource_type="pod",
                      action=Action.CREATE_POD_K8S, phase=_main._fail_phase(e),
                      error_code="POD_CREATE_FAILED", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "CREATE_POD",
            "POD_CREATE_FAILED",
            str(e),
            rollback=rollback,
            pod_name=pod_name,
        ), 500, cause=e, restart_from=POD_RESTART_STEP)

    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  node_name=best_node, resource_type="pod",
                  action=Action.CREATE_POD_K8S, phase=Phase.SUCCESS)
    _main.app.logger.info("[CREATE POD] pod creation request sent")

# 준비 대기 안의 세부 단계. 진행 상황(Pod 이벤트) 단계 이름 → 작업 이력의 단계.
WAIT_READY_SUBSTAGES = {
    "pulling_image": Action.PULL_IMAGE,
    "starting_container": Action.START_CONTAINER,
    "mount_retrying": Action.MOUNT_VOLUME,
}


def step_wait_ready(ctx):
    request_id, username, pod_name = ctx["request_id"], ctx["username"], ctx["pod_name"]
    best_node, v1 = ctx["node"], ctx["v1"]
    ns = _main.app.config["NAMESPACE"]

    _main.app.logger.info("[CREATE POD] waiting for pod to become Ready")
    # "이미지 pull / 컨테이너 기동 대기 중"처럼 두 단계를 합친 문구를 초기값으로도
    # 남기지 않는다 — 이벤트가 아직 안 잡힌 순간에도 이미 분리된 stage로 시작해서,
    # pulling_image/starting_container 둘 중 하나로만 노출되게 한다.
    _main.set_pod_creation_status(request_id, "pulling_image", "이미지 다운로드 중")
    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  node_name=best_node, resource_type="pod",
                  action=Action.WAIT_READY, phase=Phase.START)
    _main.app.logger.info(f"[CREATE POD] username={username} pod={pod_name} stage=pulling_image 이미지 다운로드 중")
    # 지금 열려 있는 세부 단계. 다음 세부 단계로 넘어가거나 준비가 끝나면 SUCCESS, 실패하면 FAIL로 닫는다.
    sub_log = dict(request_id=request_id, username=username, pod_name=pod_name, node_name=best_node, resource_type="pod")
    open_sub = [None]

    def close_sub(phase, error_code=None):
        if open_sub[0] is not None:
            _main.log_operation(action=open_sub[0], phase=phase, error_code=error_code, **sub_log)
            open_sub[0] = None

    try:
        failure_reason = None
        max_wait = _main.app.config["POD_READY_MAX_WAIT_SEC"]
        last_progress_stage = "pulling_image"
        for i in range(max_wait):
            pod = v1.read_namespaced_pod(pod_name, ns)
            if _main.is_pod_ready(pod):
                _main.app.logger.info(f"[CREATE POD] pod ready after {i+1} seconds")
                close_sub(Phase.SUCCESS)
                break
            failure_reason = _main.get_pod_failure_reason(pod)
            if failure_reason:
                _main.app.logger.error(f"[CREATE POD] pod failed to start: {failure_reason}")
                break

            # 5초에 한 번만 이벤트를 조회해 API 부담을 줄이고, 단계가 실제로 바뀔 때만
            # Redis에 다시 쓴다. stage 필드 자체를 이미지 pull 중/컨테이너 기동 중으로
            # 구분해서 저장한다 (메시지 텍스트만 바꾸면 프론트에서 두 단계를 구분할 수 없다).
            if i % 5 == 0:
                progress = _main.get_pod_progress_stage(v1, ns, pod_name)
                sub_action = WAIT_READY_SUBSTAGES.get(progress[0]) if progress else None
                if sub_action is not None and sub_action != open_sub[0]:
                    close_sub(Phase.SUCCESS)
                    open_sub[0] = sub_action
                    _main.log_operation(action=sub_action, phase=Phase.START, **sub_log)
                if progress and progress[0] != last_progress_stage:
                    last_progress_stage, progress_message = progress
                    _main.set_pod_creation_status(request_id, last_progress_stage, progress_message)
                    _main.app.logger.info(f"[CREATE POD] username={username} pod={pod_name} stage={last_progress_stage} {progress_message}")

            time.sleep(1)
        else:
            failure_reason = failure_reason or f"pod not ready within {max_wait}s"

        if failure_reason:
            close_sub(Phase.FAIL, "POD_READY_TIMEOUT")
            _main.app.logger.info(f"[CREATE POD] deleting failed pod: {pod_name}")
            _main.set_pod_creation_status(request_id, "failed", failure_reason.split(":", 1)[0])
            rollback = _main._cleanup_create_failure(pod_name, v1)
            # 컨테이너가 남긴 마지막 출력을 이 단계 행에 함께 남긴다. 작업 단위 행에도 실리지만,
            # 단계별로 볼 때 먼저 보게 되는 것은 이 행이다.
            log_tail = rollback.get("podLogTail")
            _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                          node_name=best_node, resource_type="pod",
                          action=Action.WAIT_READY, phase=Phase.FAIL,
                          error_code="POD_READY_TIMEOUT",
                          error_detail=f"{failure_reason}\n{log_tail}" if log_tail else failure_reason)
            # 위에서 Pod를 지웠다. 다시 기다려 봐야 "없는 Pod"만 보게 되고, 그 오류가 여기 담긴
            # 진짜 원인(컨테이너가 왜 죽었는지)을 덮어쓴다. 그래서 재시도하지 않는다.
            raise _main.StepFailed(_main.infra_error(
                "WAIT_POD_READY",
                "POD_READY_TIMEOUT",
                failure_reason,
                rollback=rollback,
                pod_name=pod_name,
            ), 500, retry=False)
    except _main.StepFailed:
        raise
    except client.exceptions.ApiException as e:
        close_sub(Phase.FAIL, "POD_READY_CHECK_FAILED")
        _main.app.logger.exception("[CREATE POD] pod ready check failed")
        _main.set_pod_creation_status(request_id, "failed", "pod ready 확인 실패")
        rollback = _main._cleanup_create_failure(pod_name, v1)
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=best_node, resource_type="pod",
                      action=Action.WAIT_READY, phase=Phase.FAIL,
                      error_code="POD_READY_CHECK_FAILED", error_detail=str(e.body))
        raise _main.StepFailed(_main.infra_error(
            "WAIT_POD_READY",
            "POD_READY_CHECK_FAILED",
            e.body,
            rollback=rollback,
            pod_name=pod_name,
            **_main.k8s_error_fields(e),
        ), 500, restart_from=POD_RESTART_STEP)
    except Exception as e:
        close_sub(Phase.FAIL, "POD_READY_CHECK_FAILED")
        _main.app.logger.exception("[CREATE POD] pod ready check failed")
        _main.set_pod_creation_status(request_id, "failed", "pod ready 확인 실패")
        rollback = _main._cleanup_create_failure(pod_name, v1)
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=best_node, resource_type="pod",
                      action=Action.WAIT_READY, phase=_main._fail_phase(e),
                      error_code="POD_READY_CHECK_FAILED", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "WAIT_POD_READY",
            "POD_READY_CHECK_FAILED",
            str(e),
            rollback=rollback,
            pod_name=pod_name,
        ), 500, cause=e, restart_from=POD_RESTART_STEP)

    # 이미지 다운로드처럼 오래 걸린 이유가 작업 기록에 남도록 이벤트 요약을 함께 적는다.
    start_summary = _main.summarize_pod_start_events(v1, ns, pod_name)
    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  node_name=best_node, resource_type="pod",
                  action=Action.WAIT_READY, phase=Phase.SUCCESS,
                  error_detail=json.dumps(start_summary) if start_summary else None)

def step_create_services(ctx):
    request_id, username, pod_name = ctx["request_id"], ctx["username"], ctx["pod_name"]
    best_node, v1 = ctx["node"], ctx["v1"]
    ns = _main.app.config["NAMESPACE"]

    _main.app.logger.info("[CREATE POD] creating NodePort services")
    _main.set_pod_creation_status(request_id, "creating_services", "NodePort 서비스 생성 중")
    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  node_name=best_node, resource_type="service",
                  action=Action.CREATE_SERVICE, phase=Phase.START)
    try:
        _main.create_nodeport_services(username, ns, pod_name, ctx["allocated_ports"])
    except client.exceptions.ApiException as e:
        _main.app.logger.exception("[CREATE POD] service creation failed")
        _main.set_pod_creation_status(request_id, "failed", "서비스 생성 실패")
        rollback = _main._cleanup_create_failure(pod_name, v1, delete_services=True)
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=best_node, resource_type="service",
                      action=Action.CREATE_SERVICE, phase=Phase.FAIL,
                      error_code="NODEPORT_SERVICE_CREATE_FAILED", error_detail=str(e.body))
        raise _main.StepFailed(_main.infra_error(
            "CREATE_NODEPORT_SERVICE",
            "NODEPORT_SERVICE_CREATE_FAILED",
            e.body,
            rollback=rollback,
            pod_name=pod_name,
            **_main.k8s_error_fields(e),
        ), 500, restart_from=POD_RESTART_STEP)
    except Exception as e:
        _main.app.logger.exception("[CREATE POD] service creation failed")
        _main.set_pod_creation_status(request_id, "failed", "서비스 생성 실패")
        rollback = _main._cleanup_create_failure(pod_name, v1, delete_services=True)
        _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                      node_name=best_node, resource_type="service",
                      action=Action.CREATE_SERVICE, phase=_main._fail_phase(e),
                      error_code="NODEPORT_SERVICE_CREATE_FAILED", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "CREATE_NODEPORT_SERVICE",
            "NODEPORT_SERVICE_CREATE_FAILED",
            str(e),
            rollback=rollback,
            pod_name=pod_name,
        ), 500, cause=e, restart_from=POD_RESTART_STEP)

    _main.log_operation(request_id=request_id, username=username, pod_name=pod_name,
                  node_name=best_node, resource_type="service",
                  action=Action.CREATE_SERVICE, phase=Phase.SUCCESS)
    _main.app.logger.info("[CREATE POD] services created successfully")

    _main.app.logger.info(f"[CREATE POD] success - pod={pod_name}, node={best_node}")
    _main.set_pod_creation_status(request_id, "ready", f"컨테이너 생성 완료 (node={best_node})")

POD_CREATE_STEPS = [
    step_fetch_user_config,
    step_prepare_pod,
    step_select_node,
    step_build_pod_spec,
    step_create_pod_k8s,
    step_wait_ready,
    step_create_services,
]

def build_pod_spec(
    username: str,
    user_info: dict,
    target_node: str,
    pod_name: str,
    request_id=None
):
    # 생성 작업은 request_id로 진행 상황을 추적한다(한 사용자가 Pod를 여러 개
    # 동시에 만들 수 있어 username만으로는 서로 다른 시도가 섞인다). migrate 경로는
    # 아직 request_id를 안 넘기므로 그때는 기존처럼 username을 키로 쓴다.
    status_key = request_id or username
    _main.app.logger.info(f"[POD SPEC] start user={username} node={target_node}")
    _main.app.logger.debug(f"[POD SPEC] user_info={user_info}")
    ns = _main.app.config["NAMESPACE"]

    # subPath mounts require the source files to already exist on the NFS share.
    _main.ensure_etc_layout()

    canonical = _main.resolve_k8s_node_name(target_node)
    if not canonical:
        raise ValueError(f"unknown kubernetes node: {target_node!r}")
    if canonical != target_node:
        _main.app.logger.info(
            f"[POD SPEC] nodeName will use canonical {canonical!r} (was {target_node!r})"
        )
    target_node = canonical

    image = _main.load_user_image(username, user_info["image"])

    # passwd가 uid/gid의 단일 진실 소스 — WAS 값은 무시
    passwd_rec = None
    for _line in _main.read_passwd_lines():
        _rec = _main.parse_passwd_line(_line)
        if _rec and _rec["name"] == username:
            passwd_rec = _rec
            break
    if passwd_rec is None:
        raise ValueError(
            f"user {username!r} not found in /etc/passwd — "
            "account를 담아 생성 작업(POST /operations/provision)을 등록하세요"
        )
    uid = passwd_rec["uid"]
    primary_gid = passwd_rec["gid"]
    primary_group_name = username
    for _line in _main.read_group_lines():
        _rec = _main.parse_group_line(_line)
        if _rec and _rec["gid"] == primary_gid:
            primary_group_name = _rec["name"]
            break

    # group 멤버 홈 마운트용 gid 목록: groups 배열(신규 포맷) 우선, 없으면 gid 필드
    groups_from_was = user_info.get("groups", [])
    if groups_from_was and isinstance(groups_from_was, list) and isinstance(groups_from_was[0], dict):
        gid_list = [g["gid"] for g in groups_from_was if isinstance(g, dict) and "gid" in g]
    else:
        gid_list = _main._normalize_gid_list(user_info.get("gid"))

    gpu_nodes = user_info.get("gpu_nodes", [])
    
    # 기본 포트
    ports = [
        {"internal_port": 22, "usage_purpose": "ssh"},
        {"internal_port": 8888, "usage_purpose": "jupyter"},
    ]
    _main.app.logger.debug(f"[POD SPEC] base ports={ports}")

    # WAS 추가 포트
    additional_ports = user_info.get("additional_ports", [])
    ports.extend(additional_ports)
    _main.app.logger.info(f"[POD SPEC] final ports={ports}")

    # additional_ports에 novnc 포트가 포함돼 있으면 entrypoint.sh가 noVNC를 띄우도록 ENABLE_VNC 주입
    enable_vnc = any(
        p.get("usage_purpose") in ("novnc", "vnc") or p.get("internal_port") == 6080
        for p in additional_ports
    )
    _main.app.logger.info(f"[POD SPEC] enable_vnc={enable_vnc}")
    # 포트 할당
    _main.set_pod_creation_status(status_key, "allocating_nodeport", "NodePort 할당 중")
    _main.log_operation(request_id=status_key, username=username, pod_name=pod_name,
                  node_name=target_node, resource_type="nodeport",
                  action=Action.ALLOCATE_NODEPORT, phase=Phase.START)
    try:
        allocated_ports = _main.allocate_nodeports(
            username=username,
            pod_name=pod_name,
            node_name=target_node,
            ports=ports
        )
    except Exception as e:
        _main.log_operation(request_id=status_key, username=username, pod_name=pod_name,
                      node_name=target_node, resource_type="nodeport",
                      action=Action.ALLOCATE_NODEPORT, phase=_main._fail_phase(e),
                      error_detail=str(e))
        raise
    _main.log_operation(request_id=status_key, username=username, pod_name=pod_name,
                  node_name=target_node, resource_type="nodeport",
                  action=Action.ALLOCATE_NODEPORT, phase=Phase.SUCCESS)
    try:
        _main.app.logger.info(f"[POD SPEC] allocated_ports={allocated_ports}")
        cpu_limit = _main.app.config["DEFAULT_CPU_LIMIT"]
        memory_limit = _main.app.config["DEFAULT_MEM_LIMIT"]
        num_gpu = 0
    
        tn_key = target_node.lower()
        for node in gpu_nodes:
            if (node.get("node_name") or "").lower() == tn_key:
                cpu_limit = node.get("cpu_limit", cpu_limit)
                memory_limit = node.get("memory_limit", memory_limit)
                num_gpu = node.get("num_gpu", 0)
                break
    
        _main.app.logger.info(f"[POD SPEC] resources cpu={cpu_limit} mem={memory_limit} gpu={num_gpu}")

        # GPU 디바이스는 개별 hostPath로 수동 마운트하지 않는다. 이미지에 baked-in된
        # NVIDIA_VISIBLE_DEVICES=all과 노드의 기본 컨테이너 런타임(nvidia-container-runtime)이
        # 컨테이너 생성 시점마다 현재 호스트 디바이스 상태를 다시 조회해서 알아서 주입해준다.
        # 예전에는 /dev/nvidia{i}를 수동으로 bind mount했는데, 이 마운트는 마운트 시점의
        # inode에 고정되기 때문에 이후 호스트에서 드라이버 리로드 등으로 디바이스 파일이
        # 재생성되면 이미 떠 있던 컨테이너의 GPU 접근이 복구 불가능하게 끊기는 문제가 있었다
        # (nvidia-container-runtime 훅과 중복/충돌하는 구조였음). 레거시 시스템(uid-gid,
        # docker run --gpus device=all --runtime=nvidia)도 개별 디바이스를 수동 마운트하지
        # 않는 방식이라 이 문제가 없었다.

        # NFS user-share 전체를 /home에 마운트 — 유저 격리는 chmod 700으로 처리
        # image-store PVC(pvc-image-store)는 제거 — 해당 PV의 NFS subdir가
        # 미치환 템플릿(user-share/${pvc.annotations.nfs.io/username})이라 모든 유저 파드가
        # mount access denied로 Ready 실패. MVP는 image-store 불필요.
        volume_mounts = [
            {"name": "nfs-home",    "mountPath": "/home",        "readOnly": False},
        ]
        volumes = [
            {
                "name": "nfs-home",
                # 노드마다 로컬 NFS 마운트 경로(/home/tako<N>/share/user)가 다르므로
                # 항상 이 Pod가 뜰 target_node 기준으로 계산한다 (전 노드 공통 고정값이었던
                # 예전 FARM_HOME_MOUNT_ROOT는 farm2 외 노드에서 FailedMount를 유발했다).
                "hostPath": {"path": _main.resolve_farm_home_mount_root(target_node), "type": "Directory"},
            },
        ]

        if _main.app.config["KRB5_REALM"]:
            # keytab은 컨테이너에 마운트하지 않는다 — farm 노드에만 배포하고 호스트가 갱신한 TGT만 공유한다.
            # 이 배포가 실패하면 예외가 아래 except로 전달되어 nodeport 롤백 + Pod 미생성으로 처리된다.
            _main.set_pod_creation_status(status_key, "deploying_krb5", f"krb5 배포 중 (node={target_node})")
            _main.log_operation(request_id=status_key, username=username, pod_name=pod_name,
                          node_name=target_node, resource_type="kerberos",
                          action=Action.DEPLOY_KRB5, phase=Phase.START)
            try:
                _main._deploy_krb5_to_farm(username, uid, target_node)
            except Exception as e:
                _main.log_operation(request_id=status_key, username=username, pod_name=pod_name,
                              node_name=target_node, resource_type="kerberos",
                              action=Action.DEPLOY_KRB5, phase=_main._fail_phase(e),
                              error_detail=str(e))
                raise
            _main.log_operation(request_id=status_key, username=username, pod_name=pod_name,
                          node_name=target_node, resource_type="kerberos",
                          action=Action.DEPLOY_KRB5, phase=Phase.SUCCESS)

            # rpc-gssd가 호스트에서 ccache를 읽을 수 있도록 Pod와 호스트가 /run/user/<uid> 공유
            volume_mounts.append({
                "name": "krb5-ccache",
                "mountPath": f"/run/user/{uid}",
            })
            volumes.append({
                "name": "krb5-ccache",
                "hostPath": {
                    "path": f"/run/user/{uid}",
                    "type": "DirectoryOrCreate",
                },
            })

        _main.app.logger.debug(f"[POD SPEC] volume_mounts={len(volume_mounts)} volumes={len(volumes)}")
    
        spec = {
                    "config": {
                        "backend": "kubernetes",
                        "kubernetes": {
                            "connection": {
                                    "host": "https://kubernetes.default.svc",
                                    "cacertFile": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
                                    "bearerTokenFile": "/var/run/secrets/kubernetes.io/serviceaccount/token"
                                },
                            "pod": {
                                "metadata": {
                                    "name": pod_name,
                                    "namespace": ns,
                                    "labels": {
                                        "app": "ailab-guest",
                                        "managed-by": "ailab-infra",
                                        "username": username,
                                        "pod_name": pod_name,
                                        # GPU 유실 점검 CronJob(check_gpu_pods.py)이 이 라벨로 대상을 고른다.
                                        "has-gpu": "true" if num_gpu > 0 else "false"
                                    }
                                },
                                "spec": {
                                        "nodeName": target_node,
                                        # 노드가 쪼들릴 때 kubelet이 Pod를 쫓아내는 순서는 우선순위로 갈린다.
                                        # 비워 두면(0) 사용자 컨테이너가 가장 먼저 밀려난다 — 실제로 그렇게
                                        # 축출돼 마이그레이션이 실패했다. 등급이 없는 환경에서는 설정을 비우면
                                        # 이 항목을 넣지 않는다(없는 등급을 지정하면 Pod 생성이 거부된다).
                                        **({"priorityClassName": _main.app.config["POD_PRIORITY_CLASS"]}
                                           if _main.app.config["POD_PRIORITY_CLASS"] else {}),
                                        "containers": [
                                            {
                                                "name": "shell",
                                                "image": image,
                                                "imagePullPolicy": "IfNotPresent",
                                                "stdin": True,
                                                "tty": True,
                                                "ports": [
                                                    {
                                                        "containerPort": m["internal_port"],
                                                        "protocol": "TCP"
                                                    }
                                                    for m in allocated_ports
                                                ],
                                                "env": [
                                                    {"name": "USER", "value": username},
                                                    {"name": "USER_ID", "value": username},
                                                    {"name": "USER_GROUP", "value": primary_group_name},
                                                    {"name": "TARGET_UID", "value": str(uid)},
                                                    {"name": "TARGET_GID", "value": str(primary_gid)},
                                                    {"name": "UID", "value": str(uid)},
                                                    {"name": "GID", "value": str(primary_gid)},
                                                    {"name": "SHELL", "value": "/bin/bash"},
                                                    # entrypoint.sh의 ensure_group_and_user()가 컨테이너 계정을 처음 만들 때
                                                    # `echo "$USER_ID:$USER_PW_HASH" | chpasswd -e`로 로그인 비밀번호를 설정한다.
                                                    # 평문은 받지 않는다 — admin_be가 신청 때 해시만 남긴다.
                                                    # 값은 Pod 설정에 평문으로 두지 않고 Pod별 Secret에서 읽는다
                                                    # (Pod 설정은 조회 권한만 있으면 누구나 볼 수 있다). Secret은 Pod를
                                                    # 만들기 직전에 ensure_account_secret()이 만든다.
                                                    {"name": "USER_PW_HASH", "valueFrom": {"secretKeyRef": {"name": account_secret_name(pod_name), "key": "USER_PW_HASH"}}},
                                                    # 이미지 entrypoint.sh 의 ensure_supplemental_groups()가 읽는 이름은
                                                    # DECS_SUPPLEMENTAL_GROUPS 다. USER_GROUPS 로 보내면 아무도 읽지 않아
                                                    # 보조 그룹이 컨테이너 안에 만들어지지 않는다(#145). 값 형식(콤마 구분
                                                    # 이름:gid)은 양쪽이 이미 같아 그대로 둔다 — 함수명까지 바꾸면
                                                    # 재수출·호출부로 변경이 번진다.
                                                    {"name": "DECS_SUPPLEMENTAL_GROUPS", "value": _main._build_user_groups_env(username, primary_group_name, primary_gid, gid_list)},
                                                    *([{"name": "ENABLE_VNC", "value": "true"}] if enable_vnc else []),
                                                    *([
                                                        {"name": "KRB5_REALM",          "value": _main.app.config["KRB5_REALM"]},
                                                        {"name": "DECS_KRB5_PRINCIPAL", "value": f"{username}@{_main.app.config['KRB5_REALM']}"},
                                                        # 호스트 타이머가 갱신하는 티켓 캐시 경로. 위 krb5-ccache 마운트로 Pod에 들어온다.
                                                        # entrypoint.sh는 홈에 쓸 수 없을 때 이 값이 없으면 곧바로 종료한다. 홈은 인증이
                                                        # 필요한 NFS라 노드 쪽 준비가 끝나기 전 잠깐 못 쓸 수 있으므로(특히 처음 쓰는 uid)
                                                        # 그 순간을 컨테이너 기동 실패로 만들지 않는다.
                                                        {"name": "KRB5CCNAME",          "value": f"FILE:/run/user/{uid}/krb5cc_ailab"},
                                                    ] if _main.app.config["KRB5_REALM"] else []),
                                                ],
                                                # readinessProbe가 없으면 k8s는 컨테이너 프로세스가 시작되기만 해도
                                                # Ready로 본다. 실제로는 entrypoint.sh가 그 뒤에 계정 생성/비밀번호
                                                # 설정/sshd 기동을 이어서 진행하므로, is_pod_ready()가 보는 Ready
                                                # 조건이 실제 SSH 접속 가능 시점보다 먼저 참이 되는 문제가 있었다.
                                                # sshd가 실제로 포트 22를 열 때까지 Ready를 미룬다.
                                                "readinessProbe": {
                                                    "tcpSocket": {"port": 22},
                                                    "initialDelaySeconds": 2,
                                                    "periodSeconds": 2,
                                                    "failureThreshold": 30,
                                                },
                                                "resources": {
                                                    "requests": {
                                                        "cpu": _main.app.config["DEFAULT_CPU_REQUEST"],
                                                        "memory": _main.app.config["DEFAULT_MEM_REQUEST"],
                                                        "ephemeral-storage": _main.app.config["DEFAULT_EPHEMERAL_STORAGE_REQUEST"]
                                                    },
                                                    "limits": {
                                                        "cpu": cpu_limit,
                                                        "memory": memory_limit,
                                                        "ephemeral-storage": _main.app.config["DEFAULT_EPHEMERAL_STORAGE_LIMIT"]
                                                    }
                                                },
                                                "volumeMounts": volume_mounts
                                            }
                                        ],
                                        "volumes": volumes,
                                        "restartPolicy": "Never"
                                    }
                                }
                            }
                        },
                        "environment": {
                            "USER": {"value": username, "sensitive": False}
                        },
                        "metadata": {},
                        "files": {}
                    }
        _main.app.logger.info(f"[POD SPEC] complete pod_name={pod_name}")
        return spec, allocated_ports
    except Exception as e:
        _main.app.logger.warning(
            "[POD SPEC] failed after nodeport allocation; releasing rows pod=%s — %s",
            pod_name, e,
            exc_info=True,
        )
        rollback = {"nodeportsReleased": False}
        try:
            _main.release_nodeports(pod_name)
            rollback["nodeportsReleased"] = True
        except Exception:
            _main.app.logger.warning(
                "[POD SPEC] nodeport release failed during rollback pod=%s",
                pod_name,
                exc_info=True,
            )
        raise _main.PodSpecBuildError(str(e), progress=rollback) from e

def _normalize_gid_list(raw_gid) -> List[int]:
    if raw_gid is None:
        return []
    if isinstance(raw_gid, list):
        values = raw_gid
    else:
        values = [raw_gid]
    out = []
    for value in values:
        if isinstance(value, int):
            out.append(value)
        elif str(value).isdigit():
            out.append(int(value))
    return out

def _resolve_primary_group(username: str, gid_list: List[int]) -> tuple[int, str]:
    primary_gid = None
    for line in _main.read_passwd_lines():
        rec = _main.parse_passwd_line(line)
        if rec and rec["name"] == username:
            primary_gid = rec["gid"]
            break

    if primary_gid is None and gid_list:
        primary_gid = gid_list[0]

    if primary_gid is None:
        raise ValueError(f"primary gid not found for user {username!r}")

    primary_group_name = username
    for line in _main.read_group_lines():
        rec = _main.parse_group_line(line)
        if rec and rec["gid"] == primary_gid:
            primary_group_name = rec["name"]
            break

    return primary_gid, primary_group_name

def _build_user_groups_env(
    username: str, primary_group_name: str, primary_gid: int, gid_list: List[int]
) -> str:
    """DECS_SUPPLEMENTAL_GROUPS env var 값 생성: 'primary:gid,supp1:gid1,...' 형태."""
    entries = [f"{primary_group_name}:{primary_gid}"]
    seen = {primary_gid}
    g_lines = _main.read_group_lines()
    for gid in gid_list:
        if gid in seen:
            continue
        seen.add(gid)
        for line in g_lines:
            rec = _main.parse_group_line(line)
            if rec and rec["gid"] == gid:
                entries.append(f"{rec['name']}:{gid}")
                break
    return ",".join(entries)

def _get_sudo_allowed_commands() -> List[str]:
    return [cmd for cmd in _main.app.config.get("SUDO_ALLOWED_COMMANDS", []) if cmd]

def _build_sudoers_policy(username: str) -> Optional[str]:
    allowed_commands = _main._get_sudo_allowed_commands()
    if not allowed_commands:
        return None
    return f"{username} ALL=(ALL) PASSWD: {', '.join(allowed_commands)}\n"

def _rollback_user(name: str, primary_gid: Optional[int] = None) -> None:
    """계정 파일에서 사용자를 지운다. 개인 그룹은 이름이 사용자명과 같거나, primary_gid가 주어지면 그 gid인
    줄을 멤버가 없을 때 지운다 — 개인 그룹 이름을 따로 받은 계정(primary_group_name)도 되감아 다시 만들 때
    "primary group conflict"로 막히지 않게 한다(#210)."""
    with _main.ledger_lock():
        pw_lines = _main.read_passwd_lines()
        _main.write_passwd_lines([l for l in pw_lines if (_main.parse_passwd_line(l) or {}).get("name") != name])

        sh_lines = _main.read_shadow_lines()
        _main.write_shadow_lines([l for l in sh_lines if (_main.parse_shadow_line(l) or {}).get("name") != name])

        g_lines = _main.read_group_lines()
        cleaned = []
        for gl in g_lines:
            rec = _main.parse_group_line(gl)
            if not rec:
                cleaned.append(gl)
                continue
            if name in rec["members"]:
                rec["members"] = [m for m in rec["members"] if m != name]
            personal = rec["name"] == name or (primary_gid is not None and rec["gid"] == primary_gid)
            if personal and not rec["members"]:
                continue
            cleaned.append(_main.format_group_entry(rec))
        _main.write_group_lines(cleaned)

def _allocate_next_uid(lines, min_uid: int = 20000, issued_max: int = 0) -> int:
    """관리 유저(uid >= min_uid, home=/home/) 최댓값과 지금까지 발급한 최댓값(issued_max) 중 큰 쪽 + 1부터
    시작해 passwd 전체에서 사용 중이지 않은 uid를 반환한다. 원장에서 지워진 번호는 다시 주지 않는다.
    시스템 계정이 중간 번호를 점유해도 건너뛰므로 충돌이 없다."""
    used_uids = {rec["uid"] for line in lines if (rec := _main.parse_passwd_line(line))}
    managed_uids = {
        rec["uid"] for line in lines
        if (rec := _main.parse_passwd_line(line))
        and rec["uid"] >= min_uid
        and rec.get("home", "").startswith("/home/")
    }
    candidate = max(max(managed_uids, default=min_uid - 1), issued_max) + 1
    while candidate in used_uids:
        candidate += 1
    return candidate

def _allocate_next_gid(lines, min_gid: int = 20000, issued_max: int = 0) -> int:
    """group 파일의 관리 그룹 최댓값과 지금까지 발급한 최댓값(issued_max) 중 큰 쪽 다음의 빈 GID를 반환한다."""
    reserved_gids = {65534}
    used_gids = {
        rec["gid"]
        for line in lines
        if (rec := _main.parse_group_line(line)) and isinstance(rec.get("gid"), int)
    }
    managed_gids = {
        gid for gid in used_gids
        if gid >= min_gid and gid not in reserved_gids
    }
    candidate = max(max(managed_gids, default=min_gid - 1), issued_max) + 1
    while candidate in used_gids or candidate in reserved_gids:
        candidate += 1
    return candidate

def _returning_owner_uid(ctx):
    """이 사람이 예전에 쓰던 uid 이고 NAS 홈이 지금도 그 uid 소유면 (그 번호, 같은 번호를 가진 다른 홈 이름들).
    아니면 (None, []).

    한 번호는 한 사람에게만 준다(#201). 같은 사람이 돌아왔을 때 그 번호를 되돌려 주는 것은 그 원칙
    안이고, 새 번호를 주면 보존해 둔 홈의 소유자와 어긋나 HOME_OWNER_MISMATCH 로 영영 막힌다.
    후보는 같은 작업의 앞선 시도가 받은 uid(되감기 재시도)와 admin_be 가 보낸 expected_uid 다.
    그 값만 믿지 않고 홈 소유자로 한 번 더 확인해, 기록이 틀려도 남의 홈을 넘겨받지 않게 한다.
    홈이 없으면 지킬 데이터가 없으므로 새 번호를 준다."""
    candidates = [int(c) for c in (ctx.get("uid"), ctx.get("expected_uid")) if c is not None]
    if not candidates:
        return None, []
    request_id, name = ctx["request_id"], ctx["name"]
    # 원장에 이미 있으면 계정 단계가 USER_ALREADY_EXISTS 로 바로 멈춘다 — NAS 장애가 그 원인을 가리지 않게 한다.
    if any((_main.parse_passwd_line(l) or {}).get("name") == name for l in _main.read_passwd_lines()):
        return None, []
    try:
        owner = _main.user_home_owner_uid(name)
        uid = owner if owner in candidates else None
        sharers = _main.other_homes_owned_by(uid, name) if uid is not None else []
    except Exception as e:
        _main.app.logger.exception("[ACCOUNTS] home owner lookup failed for user=%s", name)
        _main.log_operation(request_id=request_id, username=name, resource_type="account",
                      action=Action.CREATE_ACCOUNT, phase=_main._fail_phase(e),
                      error_code="NAS_SSH_FAILED", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "CREATE_ACCOUNT", "NAS_SSH_FAILED", f"failed to look up home owner for {name}"), 500, cause=e)
    return uid, sharers


def _reclaim_uid(ctx, uid, sharers, passwd_lines) -> int:
    """돌아온 사용자에게 예전 uid 를 되돌려 준다. 원장 잠금 안에서 부른다.
    그 번호를 지금 다른 계정·그룹·홈이 쓰고 있거나 대역 밖이면 되돌려 줄 수 없다 — #201 전에 번호가
    재발급된 흔적이라, 돌려주면 다른 사람의 홈까지 넘겨받는다. 새 번호로 넘어가면 홈 소유자 불일치로
    가려지므로, 재시도 없이 그대로 드러낸다."""
    request_id, name = ctx["request_id"], ctx["name"]
    own_groups = {name, ctx.get("pg_name") or name}
    conflict = None
    if uid < _main.UID_MIN or (_main.UID_MAX is not None and uid > _main.UID_MAX):
        conflict = f"uid {uid} is outside {_main.UID_MIN}~{_main.UID_MAX}"
    else:
        holder = next((r["name"] for l in passwd_lines
                       if (r := _main.parse_passwd_line(l)) and r["uid"] == uid), None)
        if holder is None:
            holder = next((r["name"] for l in _main.read_group_lines()
                           if (r := _main.parse_group_line(l)) and r["gid"] == uid and r["name"] not in own_groups),
                          None)
        if holder is not None:
            conflict = f"uid {uid} of {name}'s home is now used by {holder}"
        elif sharers:
            conflict = f"uid {uid} of {name}'s home also owns the homes of {', '.join(sharers)}"
    if conflict:
        _main.log_operation(request_id=request_id, username=name, resource_type="account",
                      action=Action.CREATE_ACCOUNT, phase=Phase.FAIL,
                      error_code="EXPECTED_UID_CONFLICT", error_detail=conflict)
        raise _main.StepFailed(_main.infra_error("CREATE_ACCOUNT", "EXPECTED_UID_CONFLICT", conflict),
                               409, retry=False)
    _main.app.logger.warning(f"[ACCOUNTS] returning user: reclaimed uid={uid} for user={name}")
    return uid


def _delete_home_if_created(ctx, name):
    """이 작업이 새로 만든 빈 홈만 지운다. 이미 있던 홈(돌아온 사용자·재사용 계정)은 사용자 데이터라
    절대 지우지 않는다. 홈을 지우는 되돌림은 모두 이 함수를 거친다."""
    if not ctx.get("home_created"):
        return
    try:
        _main.delete_user_home_directory(name)
        ctx["home_created"] = False
    except Exception:
        _main.app.logger.warning(f"[ACCOUNTS] 롤백 중 새 홈 삭제 실패(무시): {name}")


def step_create_account(ctx):
    """passwd/group/shadow/sudoers까지가 계정 단계다. 비밀번호는 동기 호출이면 평문(plaintext_pw)을
    받아 여기서 해시하고, 제어기가 실행하는 작업이면 등록 때 만든 해시(passwd_hash)를 그대로 쓴다."""
    request_id, name = ctx["request_id"], ctx["name"]
    pg_name, supp_groups = ctx["pg_name"], ctx["supp_groups"]

    _main.ensure_etc_layout()

    # 1) passwd — LOCK_EX를 read부터 write까지 유지해 uid 중복 배정 방지
    uid = gid = None
    entry = None
    _main.log_operation(request_id=request_id, username=name, resource_type="account",
                  action=Action.CREATE_ACCOUNT, phase=Phase.START)
    # NAS 조회는 원장 잠금 밖에서 한다 — 잠금을 쥔 채 SSH 를 기다리면 다른 계정 작업이 모두 멈춘다.
    returning_uid, uid_sharers = _returning_owner_uid(ctx)
    try:
        with _main.ledger_lock(), _main.LockedFile(_main.app.config["PASSWD_PATH"], "r+") as f:
            content = f.read()
            lines = content.splitlines()

            if any((_main.parse_passwd_line(l) or {}).get("name") == name for l in lines):
                _main.log_operation(request_id=request_id, username=name, resource_type="account",
                              action=Action.CREATE_ACCOUNT, phase=Phase.FAIL,
                              error_code="USER_ALREADY_EXISTS", error_detail="user already exists")
                raise _main.StepFailed({"error": "user already exists"}, 409)

            if returning_uid is not None:
                uid = _reclaim_uid(ctx, returning_uid, uid_sharers, lines)
            else:
                uid = _main._allocate_next_uid(lines, min_uid=_main.UID_MIN,
                                               issued_max=_main.read_issued_id_max("uid"))
                if _main.UID_MAX is not None and uid > _main.UID_MAX:
                    _main.log_operation(request_id=request_id, username=name, resource_type="account",
                                  action=Action.CREATE_ACCOUNT, phase=Phase.FAIL,
                                  error_code="UID_RANGE_EXHAUSTED",
                                  error_detail=f"next uid {uid} exceeds UID_MAX {_main.UID_MAX}")
                    raise _main.StepFailed(_main.infra_error(
                        "CREATE_ACCOUNT", "UID_RANGE_EXHAUSTED",
                        f"uid range {_main.UID_MIN}~{_main.UID_MAX} exhausted",
                    ), 500)
                _main.app.logger.info(f"[ACCOUNTS] auto-assigned uid={uid} for user={name}")
            gid = uid
            _main.record_issued_id("uid", uid)

            # 쓰기 시작 표시(#210). 이 이름의 계정이 없음을 잠금 안에서 확인한 직후, 계정 파일에 쓰기 전에
            # 이 작업의 진행 기록에 남긴다. 이 단계 도중 제어기가 죽으면 이어받은 쪽이 이 표시로 "이 작업이
            # 쓴 계정"과 "예전 신청이 만든 계정"을 가린다. 제어기 밖(체크포인트 없음)에서는 아무것도 하지 않는다.
            checkpoint = ctx.get("_checkpoint")
            if checkpoint is not None:
                ctx["account_write_started"] = uid
                try:
                    checkpoint()
                except LeaseLost:
                    raise
                except Exception as e:
                    # 아무것도 쓰기 전이다. 계정 파일 쓰기 실패와 섞이지 않게 따로 표시하고 재시도에 맡긴다.
                    _main.log_operation(request_id=request_id, username=name, resource_type="account",
                                  action=Action.CREATE_ACCOUNT, phase=_main._fail_phase(e),
                                  error_code="CHECKPOINT_FAILED", error_detail=str(e))
                    raise _main.StepFailed(_main.infra_error(
                        "CREATE_ACCOUNT", "CHECKPOINT_FAILED", "failed to record account write marker"),
                        500, cause=e)

            entry = {
                "name": name,
                "passwd": "x",
                "uid": uid,
                "gid": gid,
                "gecos": ctx["gecos"],
                "home": f"/home/{name}",
                "shell": "/bin/bash",
            }
            lines.append(_main.format_passwd_entry(entry))
            new_content = "\n".join(lines) + "\n"
            f.seek(0)
            f.write(new_content)
            f.truncate()
    except (_main.StepFailed, LeaseLost):
        raise
    except Exception as e:
        _main.log_operation(request_id=request_id, username=name, resource_type="account",
                      action=Action.CREATE_ACCOUNT, phase=Phase.FAIL,
                      error_code="PASSWD_WRITE_FAILED", error_detail=str(e))
        raise

    # 2) group — primary 생성 + supplementary 멤버 추가
    added_supp = []
    try:
        with _main.ledger_lock(), _main.LockedFile(_main.app.config["GROUP_PATH"], "r+") as f:
            content = f.read()
            g_lines = content.splitlines()

            # primary group — 공용 gid 대역을 떼어낸 뒤로 uid 대역의 group 줄은 이 사용자의 개인
            # 그룹뿐이다. 이름·gid 중 한쪽만 맞는 줄은 원장이 깨진 것이므로 조용히 재사용하지 않고
            # 아래 except 의 GROUP_WRITE_FAILED + 롤백 경로로 보낸다(#148).
            primary_exists = False
            for gl in g_lines:
                rec = _main.parse_group_line(gl)
                if not rec or (rec["gid"] != gid and rec["name"] != pg_name):
                    continue
                if rec["gid"] != gid or rec["name"] != pg_name:
                    raise RuntimeError(
                        f"primary group conflict: existing {rec['name']}:{rec['gid']} vs {pg_name}:{gid}")
                primary_exists = True
            if not primary_exists:
                g_lines.append(_main.format_group_entry({"name": pg_name, "passwd": "x", "gid": gid, "members": []}))

            # supplementary groups
            for sg in supp_groups:
                sg_gid = int(sg["gid"])
                sg_name = sg["name"]
                found = False
                updated = []
                for gl in g_lines:
                    rec = _main.parse_group_line(gl)
                    if rec and rec["gid"] == sg_gid:
                        if name not in rec["members"]:
                            rec["members"].append(name)
                        updated.append(_main.format_group_entry(rec))
                        found = True
                    else:
                        updated.append(gl)
                g_lines = updated
                if not found:
                    # 원장에서 빠진 팀 그룹 줄을 되살리는 경로 — 그 번호도 발급 기록에 올려 자동 배정과 겹치지 않게 한다.
                    if sg_gid >= _main.SHARED_GID_MIN:
                        _main.record_issued_id("shared_gid", sg_gid)
                    g_lines.append(_main.format_group_entry({"name": sg_name, "passwd": "x", "gid": sg_gid, "members": [name]}))
                added_supp.append({"name": sg_name, "gid": sg_gid})

            new_content = "\n".join(g_lines) + "\n"
            f.seek(0)
            f.write(new_content)
            f.truncate()
    except Exception as e:
        _main.app.logger.exception("[ACCOUNTS] group write failed for user=%s, rolling back", name)
        _main.log_operation(request_id=request_id, username=name, resource_type="account",
                      action=Action.CREATE_ACCOUNT, phase=Phase.FAIL,
                      error_code="GROUP_WRITE_FAILED", error_detail=str(e))
        _main._rollback_user(name)
        raise _main.StepFailed({"error": "failed to write group"}, 500)

    # 3) shadow
    try:
        passwd_sha512 = ctx.get("passwd_hash") or crypt.crypt(ctx["plaintext_pw"], crypt.mksalt(crypt.METHOD_SHA512))

        today_days = int(time.time() // 86400)
        shadow_entry = {
            "name": name,
            "passwd": passwd_sha512,
            "lastchg": today_days,
            "min": 0,
            "max": 99999,
            "warn": 7,
            "inactive": "",
            "expire": "",
            "flag": "",
        }
        with _main.ledger_lock():
            sh_lines = _main.read_shadow_lines()
            sh_lines.append(_main.format_shadow_entry(shadow_entry))
            _main.write_shadow_lines(sh_lines)
    except Exception as e:
        _main.app.logger.exception("[ACCOUNTS] shadow write failed for user=%s, rolling back", name)
        _main.log_operation(request_id=request_id, username=name, resource_type="account",
                      action=Action.CREATE_ACCOUNT, phase=Phase.FAIL,
                      error_code="SHADOW_WRITE_FAILED", error_detail=str(e))
        _main._rollback_user(name)
        raise _main.StepFailed({"error": "failed to write shadow"}, 500)

    # 4) sudoers (로컬 호스트 관리, password-protected whitelist)
    s_path = None
    sudoers_policy = _main._build_sudoers_policy(name)
    if sudoers_policy:
        try:
            s_path = _main.ensure_sudoers_file(_main.app.config["SUDOERS_DIR"], name, sudoers_policy)
        except Exception as e:
            _main.app.logger.exception("[ACCOUNTS] sudoers failed for user=%s, rolling back", name)
            _main.log_operation(request_id=request_id, username=name, resource_type="account",
                          action=Action.CREATE_ACCOUNT, phase=Phase.FAIL,
                          error_code="SUDOERS_CREATE_FAILED", error_detail=str(e))
            _main._rollback_user(name)
            raise _main.StepFailed({"error": "failed to create sudoers file"}, 500)

    # passwd/group/shadow/sudoers까지가 계정 단계다. 홈과 Kerberos는 별도 단계로 기록해야
    # 단계별 소요시간이 나뉘고 어느 단계에서 실패했는지가 action으로 드러난다. 뒤 단계가
    # 실패하면 _rollback_user가 계정을 되돌리므로, 여기서 SUCCESS가 찍힌 계정이 이후
    # 롤백됐는지는 같은 request_id의 뒤 단계 FAIL 행으로 판별한다.
    _main.log_operation(request_id=request_id, username=name, resource_type="account",
                  action=Action.CREATE_ACCOUNT, phase=Phase.SUCCESS)
    ctx.update(uid=uid, gid=gid, entry=entry, added_supp=added_supp, s_path=s_path)

def step_create_home(ctx):
    request_id, name = ctx["request_id"], ctx["name"]

    # 5) NAS SSH로 홈 디렉터리 생성
    _main.log_operation(request_id=request_id, username=name, resource_type="storage",
                  action=Action.CREATE_HOME, phase=Phase.START)
    try:
        # 앞선 시도가 만든 홈이면 이번 시도엔 "이미 있음"으로 보이므로 한 번 만든 사실은 유지한다.
        created = _main.create_user_home_directory(name, ctx["uid"], ctx["gid"])
        ctx["home_created"] = created or bool(ctx.get("home_created"))
    except Exception as e:
        ctx["home_created"] = getattr(e, "home_created", False) or bool(ctx.get("home_created"))
        # 소유자 불일치는 NAS 장애가 아니라 사람이 uid 를 맞춰 줘야 하는 상황이다. 재시도해도
        # 같은 결과이므로 오류 코드를 갈라서, 감수자가 저널만 보고 원인을 알 수 있게 한다.
        mismatch = isinstance(e, _main.HomeOwnerMismatch)
        code = "HOME_OWNER_MISMATCH" if mismatch else "NAS_SSH_FAILED"
        if mismatch:
            _main.app.logger.error("[ACCOUNTS] home owner mismatch for user=%s, rolling back: %s", name, e)
        else:
            _main.app.logger.exception("[ACCOUNTS] home dir creation failed for user=%s, rolling back", name)
        _main.log_operation(request_id=request_id, username=name, resource_type="storage",
                      action=Action.CREATE_HOME, phase=_main._fail_phase(e),
                      error_code=code, error_detail=str(e))
        _main._rollback_user(name)
        detail = str(e) if mismatch else f"failed to create home directory for {name}"
        # 소유자 불일치는 사람이 uid를 맞춰야 풀린다 — 재시도는 같은 결과만 반복하고 DEGRADED로
        # 넘어가면서 이 error_code를 가린다. retry=False로 한 번 만에 그대로 표면화한다.
        raise _main.StepFailed(_main.infra_error("CREATE_HOME_DIRECTORY", code, detail), 500,
                                cause=e, retry=not mismatch, restart_from=ACCOUNT_RESTART_STEP)
    _main.log_operation(request_id=request_id, username=name, resource_type="storage",
                  action=Action.CREATE_HOME, phase=Phase.SUCCESS)

def step_create_krb5_principal(ctx):
    request_id, name = ctx["request_id"], ctx["name"]

    # 6) Kerberos principal 생성 + keytab k8s Secret 저장
    if not _main.app.config.get("KRB5_REALM"):
        return
    _main.log_operation(request_id=request_id, username=name, resource_type="kerberos",
                  action=Action.CREATE_KRB5_PRINCIPAL, phase=Phase.START)
    try:
        _main._create_krb5_principal_and_secret(name, ctx["uid"], ctx["gid"])
    except Exception as e:
        _main.app.logger.exception("[ACCOUNTS] KRB5 principal creation failed for user=%s, rolling back", name)
        _main.log_operation(request_id=request_id, username=name, resource_type="kerberos",
                      action=Action.CREATE_KRB5_PRINCIPAL, phase=_main._fail_phase(e),
                      error_code="KDC_FAILED", error_detail=str(e))
        _delete_home_if_created(ctx, name)
        # _create_krb5_principal_and_secret는 AD principal 생성(①) 다음 k8s Secret
        # 저장(②) 순으로 진행된다. ①만 성공하고 ②에서 실패해도 이 except는 그냥
        # "실패"로 뭉뚱그려서 여기까지 오는데, 그러면 AD엔 이미 만들어진 principal이
        # 그대로 남는다. 존재 여부와 무관하게 항상 삭제를 시도해 정리한다.
        try:
            _main._farm_ad_ssh(f"delete {name}")
        except Exception:
            _main.app.logger.warning(f"[ACCOUNTS] 롤백 중 AD principal 삭제 실패(무시): {name}")
        _main._rollback_user(name)
        raise _main.StepFailed(_main.infra_error("CREATE_KRB5_PRINCIPAL", "KDC_FAILED", f"failed to create Kerberos principal for {name}"), 500, cause=e, restart_from=ACCOUNT_RESTART_STEP)

    # 여기서는 아직 어느 farm 노드에도 keytab을 배포하지 않았다(그건 pod 생성 시
    # build_pod_spec → _deploy_krb5_to_farm에서 함) — 그래서 지울 대상 node_name을
    # 특정할 수 없다. krb5_cleanup_pending은 (username, node_name) 단위 예약이라
    # node_name 없이 이 시점에 username만으로 지우면, 이번에 전혀 안 건드린 다른
    # 노드의 정당한 정리 예약까지 같이 지워버릴 수 있다. 그래서 여기서는 정리하지
    # 않고, 실제로 특정 노드에 배포가 확인되는 _deploy_krb5_to_farm에서만 그 노드
    # 몫만 정리한다.
    _main.log_operation(request_id=request_id, username=name, resource_type="kerberos",
                  action=Action.CREATE_KRB5_PRINCIPAL, phase=Phase.SUCCESS)

def step_await_ad_replication(ctx):
    """새 AD 계정이 도메인의 모든 DC에 도착할 때까지 기다린다.

    계정은 DC 하나에 만들어지고, 노드는 아무 DC에나 물을 수 있다. 복제 전에 노드가 사용자나 홈을
    조회하면 "없는 사용자"가 노드에 캐시되어, 첫 컨테이너가 자기 홈에 쓰지 못한다(E2E C04·C08).
    읽기만 하므로 실패해도 되돌릴 것이 없고, 같은 단계만 다시 돌리면 된다."""
    request_id, name = ctx["request_id"], ctx["name"]
    if not _main._ad_enabled():
        return
    _main.log_operation(request_id=request_id, username=name, resource_type="replication",
                  action=Action.CREATE_KRB5_PRINCIPAL, phase=Phase.START)
    try:
        _main._await_ad_replicated(name, ctx["uid"])
    except Exception as e:
        _main.app.logger.warning("[ACCOUNTS] AD 복제 대기 실패: user=%s: %s", name, e)
        _main.log_operation(request_id=request_id, username=name, resource_type="replication",
                      action=Action.CREATE_KRB5_PRINCIPAL, phase=_main._fail_phase(e),
                      error_code="AD_REPLICATION_TIMEOUT", error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "AWAIT_AD_REPLICATION", "AD_REPLICATION_TIMEOUT",
            f"AD account not replicated to all domain controllers: {name}"), 500, cause=e)
    _main.log_operation(request_id=request_id, username=name, resource_type="replication",
                  action=Action.CREATE_KRB5_PRINCIPAL, phase=Phase.SUCCESS)

def step_add_user_groups(ctx):
    """기존 사용자에게 보충 그룹을 추가하는 단계 (Pod 재사용 시에만 호출).
    
    계정 생성 단계의 그룹 처리 부분(lines 1393~1427)과 유사하지만,
    사용자가 이미 존재한다고 가정하고 그룹만 추가한다."""
    request_id, username = ctx["request_id"], ctx["username"]
    supp_groups = ctx.get("supp_groups", [])
    
    if not supp_groups:
        return  # 추가할 그룹이 없으면 실행 안함
    
    _main.log_operation(request_id=request_id, username=username, resource_type="groups",
                  action=Action.CREATE_ACCOUNT, phase=Phase.START)
    
    try:
        added_supp = []
        with _main.ledger_lock(), _main.LockedFile(_main.app.config["GROUP_PATH"], "r+") as f:
            content = f.read()
            g_lines = content.splitlines()
            
            # supplementary groups — 기존 그룹에 사용자 추가 또는 새 그룹 생성
            for sg in supp_groups:
                sg_gid = int(sg["gid"])
                sg_name = sg["name"]
                found = False
                updated = []
                for gl in g_lines:
                    rec = _main.parse_group_line(gl)
                    if rec and rec["gid"] == sg_gid:
                        if username not in rec["members"]:
                            rec["members"].append(username)
                        updated.append(_main.format_group_entry(rec))
                        found = True
                    else:
                        updated.append(gl)
                g_lines = updated
                if not found:
                    # 원장에서 빠진 팀 그룹 줄을 되살리는 경로 — 그 번호도 발급 기록에 올려 자동 배정과 겹치지 않게 한다.
                    if sg_gid >= _main.SHARED_GID_MIN:
                        _main.record_issued_id("shared_gid", sg_gid)
                    g_lines.append(_main.format_group_entry({"name": sg_name, "passwd": "x", "gid": sg_gid, "members": [username]}))
                added_supp.append({"name": sg_name, "gid": sg_gid})
            
            new_content = "\n".join(g_lines) + "\n"
            f.seek(0)
            f.write(new_content)
            f.truncate()
        
        _main.log_operation(request_id=request_id, username=username, resource_type="groups",
                      action=Action.CREATE_ACCOUNT, phase=Phase.SUCCESS)
        ctx.setdefault("added_supp", []).extend(added_supp)
    except Exception as e:
        _main.app.logger.exception("[ACCOUNTS] supplementary group write failed for user=%s", username)
        _main.log_operation(request_id=request_id, username=username, resource_type="groups",
                      action=Action.CREATE_ACCOUNT, phase=Phase.FAIL,
                      error_code="GROUP_WRITE_FAILED", error_detail=str(e))
        raise _main.StepFailed({"error": "failed to add supplementary groups"}, 500)

def step_sync_ad_groups(ctx):
    """보조 그룹을 AD 에 반영한다 — 없으면 그룹을 만들고, 사용자를 멤버로 넣는다.

    홈은 sec=krb5 로 마운트되어 그룹 권한을 NAS 가 AD 기준으로 판정한다. 그룹 파일에만
    써 두면 컨테이너 안에서만 보이고 NFS 에는 아무 효력이 없다(#146).

    계정 생성 단계에 넣지 않고 따로 뺀 이유: AD 사용자는 step_create_krb5_principal 이
    만든다. 그 전에는 멤버로 넣을 대상 자체가 없다. 그래서 이 단계는 반드시 그 뒤에 온다.

    주의: 이미 떠 있는 Pod 에는 이것만으로 반영되지 않는다. 그룹은 티켓 PAC 에 실려 오고
    갱신 타이머가 kinit -R 을 선호해 옛 PAC 이 최대 약 6일 유지된다(#153).
    """
    request_id = ctx["request_id"]
    name = ctx.get("name") or ctx["username"]
    supp_groups = ctx.get("supp_groups") or []
    if not supp_groups or not _main._ad_enabled():
        return

    _main.log_operation(request_id=request_id, username=name, resource_type="groups",
                  action=Action.CREATE_ACCOUNT, phase=Phase.START)
    try:
        for sg in supp_groups:
            _main._create_ad_group(sg["name"], int(sg["gid"]))
            _main._add_ad_group_member(sg["name"], name)
            # 이 변경 전에 만든 그룹은 디렉터리가 없다. 멤버가 들어올 때 채워 둔다(#154).
            _main._ensure_team_dir(sg["name"], int(sg["gid"]))
    except Exception as e:
        # 팀 디렉터리 gid 불일치는 사람이 NAS 를 확인해야 풀린다 — 재시도는 같은 결과만 반복한다.
        mismatch = isinstance(e, _main.TeamDirGroupMismatch)
        code = "TEAM_DIR_GROUP_MISMATCH" if mismatch else "AD_GROUP_SYNC_FAILED"
        _main.app.logger.exception("[ACCOUNTS] AD 그룹 반영 실패: user=%s", name)
        _main.log_operation(request_id=request_id, username=name, resource_type="groups",
                      action=Action.CREATE_ACCOUNT, phase=_main._fail_phase(e),
                      error_code=code, error_detail=str(e))
        raise _main.StepFailed(_main.infra_error(
            "SYNC_AD_GROUPS", code,
            f"failed to sync supplementary groups to AD for {name}"), 500, cause=e, retry=not mismatch)
    _main.log_operation(request_id=request_id, username=name, resource_type="groups",
                  action=Action.CREATE_ACCOUNT, phase=Phase.SUCCESS)

ACCOUNT_CREATE_STEPS = [
    step_create_account,
    step_create_home,
    step_create_krb5_principal,
    step_await_ad_replication,
    step_sync_ad_groups,
]

def step_trigger_nas_gss_flush(ctx):
    """재사용 계정에 그룹을 더했으면 NAS GSS 캐시 온디맨드 flush 를 띄운다(#181).

    기존 계정은 이미 NAS 와 GSS 컨텍스트를 맺고 있어 옛 그룹 목록이 굳어 있다 — 비우지 않으면
    30분 크론이 돌 때까지 새 그룹 디렉터리가 막힌다. 변경 요청 승인은 admin_be 가 직접 부르지만
    (#161) 이 경로는 AD 가 작업 안에서 바뀌므로 바꾼 쪽이 부른다.

    flush 는 부가 효과다 — 실패해도 30분 크론이 잡으므로 작업을 실패시키지 않는다."""
    if not ctx.get("supp_groups") or not _main._ad_enabled():
        return
    try:
        # reconcile_krb5 는 main 을 import 한다 — 순환을 피하려고 늦게 불러온다.
        from reconcile_krb5 import trigger_nas_gss_flush_ondemand
        trigger_nas_gss_flush_ondemand()
    except Exception:
        _main.app.logger.exception("[NAS GSS 온디맨드] 재사용 계정 그룹 추가 후 트리거 실패 — 30분 크론에 맡김")

SUPP_GROUPS_ONLY_STEPS = [
    step_add_user_groups,
    step_sync_ad_groups,
    step_trigger_nas_gss_flush,
]
