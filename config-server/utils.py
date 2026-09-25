import os
import subprocess
import shlex
import re
import fcntl
import time
import pymysql
import threading
import logging
from contextlib import contextmanager
from typing import List, Optional
import uuid

from datetime import datetime
from kubernetes import client, config as k8s_config
from kubernetes.stream import stream
from flask import current_app as app
from adapters.bg_img_redis import save_image_metadata, get_image_metadata

DEFAULT_BASE_ETC_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "base_etc")

def get_db_connection():
    try:
        app.logger.debug("Creating DB connection")

        conn = pymysql.connect(
            host=os.environ["DB_HOST"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            database=os.environ["DB_NAME"],
            autocommit=False
        )

        app.logger.debug("DB connection established")
        return conn

    except Exception:
        app.logger.exception("Failed to create DB connection")
        raise

# operation_log 전용 MySQL(log-mysql, operation_state_db) 접속을 위한 함수
def get_log_db_connection():
    try:
        app.logger.debug("Creating log DB connection")

        conn = pymysql.connect(
            host=os.environ["LOG_DB_HOST"],
            user=os.environ["LOG_DB_USER"],
            password=os.environ["LOG_DB_PASSWORD"],
            database=os.environ["LOG_DB_NAME"],
            autocommit=False
        )

        app.logger.debug("Log DB connection established")
        return conn

    except Exception:
        app.logger.exception("Failed to create log DB connection")
        raise

def load_k8s():
    # k8s client 초기
    try:
        app.logger.debug("Loading in-cluster Kubernetes config")
        k8s_config.load_incluster_config()
    except Exception:
        app.logger.debug("In-cluster config failed, loading kubeconfig")
        k8s_config.load_kube_config()


def resolve_k8s_node_name(candidate: Optional[str]) -> Optional[str]:
    """
    WAS/Prometheus 등에서 온 node 이름을 클러스터 Node와 대소문자 무시로 매칭하고,
    반환은 항상 소문자로 정규화한다(운영 노드명이 소문자인 환경 기준).
    """
    if candidate is None:
        return None
    s = str(candidate).strip().lower()
    if not s:
        return None
    load_k8s()
    v1 = client.CoreV1Api()
    try:
        resp = v1.list_node()
    except Exception:
        app.logger.exception("[NODE] list_node failed while resolving %r", s)
        return None
    for n in resp.items or []:
        if n.metadata.name.lower() == s:
            out = n.metadata.name.lower()
            if n.metadata.name != out:
                app.logger.info("[NODE] normalized node name %r -> %r", n.metadata.name, out)
            return out
    app.logger.warning("[NODE] no cluster node matches %r (case-insensitive)", s)
    return None


FARM_NODE_NAME_PATTERN = re.compile(r"^farm(\d+)$", re.IGNORECASE)


def resolve_farm_home_mount_root(target_node: str) -> str:
    """farm<N> 노드명에서 그 노드 로컬의 NFS 마운트 경로(/home/tako<N>/share/user)를 도출한다.

    각 farm 노드는 처음부터 자기 hostname 번호에 맞는 /home/tako<N>/share에만
    NFS를 마운트해왔다(admin_infra_server의 remount-farm-user-share-krb.sh 참고).
    그런데 코드는 FARM_HOME_MOUNT_ROOT 하나(기본값 /home/tako2/share/user, 즉 farm2의
    경로)를 모든 노드에 그대로 썼던 버그가 있어서, farm2가 아닌 다른 노드(farm1, farm8 등)에
    뜬 Pod는 전부 hostPath가 로컬에 없는 디렉터리를 가리켜 FailedMount로 영원히 Ready가
    안 됐다. 노드 번호를 그대로 따라가도록 고친다.
    """
    match = FARM_NODE_NAME_PATTERN.match(target_node or "")
    if not match:
        raise ValueError(f"cannot derive farm home mount path for node: {target_node!r}")
    return f"/home/tako{match.group(1)}/share/user"


def is_pod_ready(pod):
    if pod.status.phase != "Running":
        return False
    for cond in (pod.status.conditions or []):
        if cond.type == "Ready" and cond.status == "True":
            return True
    return False


# 이미지 pull 진행 중(ContainerCreating 등)과 실제 실패(ImagePullBackOff 등)를 구분하기 위한 상태 목록
POD_FAILURE_WAITING_REASONS = {
    "ImagePullBackOff", "ErrImagePull", "ErrImageNeverPull", "InvalidImageName",
    "CrashLoopBackOff", "CreateContainerConfigError", "CreateContainerError", "RunContainerError",
}

# waiting_ready 단계 안에서 "이미지 pull 중"과 "컨테이너 기동 중"을 구분해서 보여주기 위한
# k8s 이벤트 reason → (하위 단계, 한국어 메시지) 매핑. FailedMount처럼 재시도는 되지만
# 아직 최종 실패로 확정되진 않은 상태도 여기서 바로 보여줘서, kubectl 없이도 원인을 알 수 있게 한다.
POD_EVENT_STAGE_MAP = {
    "Pulling":      ("pulling_image", "이미지 다운로드 중"),
    "Pulled":       ("starting_container", "이미지 다운로드 완료, 컨테이너 시작 중"),
    "Created":      ("starting_container", "컨테이너 생성됨, 시작 중"),
    "Started":      ("starting_container", "컨테이너 시작됨, 준비 확인 중"),
    "BackOff":      ("starting_container", "컨테이너 재시도 중"),
    "FailedMount":  ("mount_retrying", "볼륨 마운트 재시도 중"),
}


def get_pod_progress_stage(v1, namespace: str, pod_name: str):
    """Pod 이벤트에서 가장 최근의 의미 있는 단계를 (stage, message)로 반환한다.
    이벤트 조회는 진행 상황 표시라는 부가 기능일 뿐이라, 실패하거나 매핑에 없는
    reason이면 조용히 None을 반환하고 호출부는 기존 문구를 그대로 유지한다."""
    try:
        events = v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}",
        ).items
    except Exception as e:
        app.logger.warning(f"[POD PROGRESS] failed to list events for {pod_name}, falling back to generic message: {e}")
        return None

    if not events:
        return None

    def event_time(e):
        return e.last_timestamp or e.event_time or e.metadata.creation_timestamp

    # 매핑에 없는 reason(Scheduled, SuccessfulMountVolume 등)이 시간상 더 최근이면
    # 이미 지난 실제 진행 단계를 가려버린다 — 이미지가 노드에 캐시돼 있어 Pulling/Pulled
    # 이벤트 없이 바로 Created/Started로 넘어가는 경우 특히 두드러진다. 매핑된 이벤트
    # 중에서만 가장 최근 것을 고른다.
    mapped_events = [e for e in events if e.reason in POD_EVENT_STAGE_MAP]
    if not mapped_events:
        return None

    latest = max(mapped_events, key=event_time)
    stage, message = POD_EVENT_STAGE_MAP[latest.reason]
    if latest.reason == "FailedMount" and latest.message:
        message = f"{message}: {latest.message.split(':', 1)[0]}"
    return stage, message


_PULL_DURATION = re.compile(r"in ((?:\d+h)?(?:\d+m)?[\d.]+m?s)")
_IMAGE_SIZE = re.compile(r"Image size: (\d+) bytes")


def _go_duration_seconds(text):
    """쿠버네티스 이벤트의 Go duration 문자열(1m2.345s, 4.2s, 850ms)을 초로 바꾼다."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)(ms|s))?", text or "")
    if not m or not text:
        return None
    h, mnt, num, unit = m.groups()
    seconds = int(h or 0) * 3600 + int(mnt or 0) * 60
    if num:
        seconds += float(num) / (1000 if unit == "ms" else 1)
    return round(seconds, 1)


def summarize_pod_start_events(v1, namespace: str, pod_name: str):
    """컨테이너가 준비되기까지의 이벤트를 화면에 보일 요약으로 만든다.

    이미지를 새로 받았는지(걸린 시간·크기) 노드에 있던 것을 썼는지, 볼륨 마운트·컨테이너 재시작을
    몇 번 했는지만 담는다. 이벤트 메시지 원문(주소·경로가 섞일 수 있음)은 넣지 않는다.
    이벤트 조회 실패는 부가 정보가 없는 것으로 보고 빈 요약을 돌려준다.
    """
    try:
        events = v1.list_namespaced_event(
            namespace=namespace, field_selector=f"involvedObject.name={pod_name}").items or []
    except Exception as e:
        app.logger.warning(f"[POD START SUMMARY] failed to list events for {pod_name}: {e}")
        return {}
    summary = {}
    for e in events:
        message = e.message or ""
        count = e.count or 1
        if e.reason == "Pulled":
            if "already present on machine" in message:
                summary.setdefault("image_source", "cached")
            else:
                summary["image_source"] = "pulled"
                m = _PULL_DURATION.search(message)
                if m:
                    summary["image_pull_seconds"] = _go_duration_seconds(m.group(1))
                size = _IMAGE_SIZE.search(message)
                if size:
                    summary["image_size_mb"] = round(int(size.group(1)) / 1024 / 1024)
        elif e.reason == "FailedMount":
            summary["mount_retries"] = summary.get("mount_retries", 0) + count
        elif e.reason == "BackOff":
            summary["restarts"] = summary.get("restarts", 0) + count
    return summary


def get_pod_failure_reason(pod):
    if pod.status.phase == "Failed":
        # pod.status.reason은 Evicted/NodeAffinity 같은 스케줄러 레벨 사유에만 채워지고,
        # 컨테이너가 그냥 비정상 종료된 경우(가장 흔한 케이스)엔 비어있어 "PodFailed"로만
        # 뭉뚱그려졌다. container_statuses[].state.terminated에 exit code/원인이 있으니
        # 있으면 그걸 우선 사용한다.
        for cs in (pod.status.container_statuses or []):
            terminated = cs.state.terminated
            if terminated:
                detail = terminated.reason or "Error"
                if terminated.exit_code is not None:
                    detail += f" (exit={terminated.exit_code})"
                if terminated.message:
                    detail += f": {terminated.message}"
                return f"PodFailed - {detail}"
        return pod.status.reason or "PodFailed"
    for cs in (pod.status.container_statuses or []):
        waiting = cs.state.waiting
        if waiting and waiting.reason in POD_FAILURE_WAITING_REASONS:
            return f"{waiting.reason}: {waiting.message}"
    return None


# 기존 Pod가 있는지 확인 -> username당 Pod 1개만 유지
def get_existing_pod(namespace, username):
    app.logger.info(f"[POD CHECK] searching existing pod for user={username}")

    load_k8s()

    core_v1 = client.CoreV1Api()
    pods = core_v1.list_namespaced_pod(
        namespace=namespace,
        label_selector=f"username={username}"
    )
    for pod in pods.items:
        app.logger.debug(
            f"[POD CHECK] found pod={pod.metadata.name} phase={pod.status.phase}"
        )

        if pod.status.phase == "Running":
            app.logger.info(f"[POD CHECK] running pod detected: {pod.metadata.name}")
            return pod.metadata.name
    app.logger.info(f"[POD CHECK] no running pod for user={username}")
    return None


# 이미지 entrypoint.sh 의 ensure_supplemental_groups() 와 같은 판정을 한다. 이름과 gid 는 셸 문자열에
# 끼워 넣지 않고 위치 인자로 넘긴다. 이름이 이미 다른 gid 로 있으면 덮어쓰지 않고 실패로 남긴다.
_POD_GROUP_SYNC_SCRIPT = r'''
u="$1"; shift
rc=0
for spec in "$@"; do
    n="${spec%:*}"; g="${spec##*:}"
    cur="$(getent group "$n" | cut -d: -f3)"
    if [ -z "$cur" ]; then
        groupadd -g "$g" "$n" || { rc=1; continue; }
    elif [ "$cur" != "$g" ]; then
        echo "group $n has gid $cur, expected $g" >&2; rc=1; continue
    fi
    usermod -aG "$n" "$u" || rc=1
done
echo "__RC=$rc"
'''
POD_GROUP_SYNC_TIMEOUT_SEC = float(os.getenv("POD_GROUP_SYNC_TIMEOUT_SEC", "15"))


# 떠 있는 Pod 의 /etc/group 에서 보조 그룹 멤버십을 뺀다. 이미 빠져 있거나 그룹이 없으면 할 일이 없다.
# 판정은 /etc/group 의 멤버 목록으로만 한다 — id -nG 는 primary 그룹도 섞여 나온다.
_POD_GROUP_REMOVE_SCRIPT = r'''
u="$1"; shift
rc=0
for n in "$@"; do
    members="$(getent group "$n" | cut -d: -f4)"
    printf '%s\n' "$members" | tr ',' '\n' | grep -qFx "$u" || continue
    gpasswd -d "$u" "$n" >/dev/null || rc=1
done
echo "__RC=$rc"
'''


def _exec_reading_stdin(v1, name, namespace, command, stdin_data):
    """exec 에 stdin_data 를 표준입력으로 넘기고 표준출력을 모아 돌려준다. 인자로 넘기면 Pod 안의
    프로세스 목록(ps)에 값이 보이므로, 비밀번호 해시처럼 드러나면 안 되는 값은 이 경로로 보낸다."""
    resp = stream(
        v1.connect_get_namespaced_pod_exec, name, namespace, command=command,
        stderr=True, stdin=True, stdout=True, tty=False, _preload_content=False,
        _request_timeout=POD_GROUP_SYNC_TIMEOUT_SEC,
    )
    try:
        resp.write_stdin(stdin_data)
        out = []
        deadline = time.monotonic() + POD_GROUP_SYNC_TIMEOUT_SEC
        while resp.is_open() and time.monotonic() < deadline:
            resp.update(timeout=1)
            if resp.peek_stdout():
                out.append(resp.read_stdout())
            if resp.peek_stderr():
                resp.read_stderr()
        return "".join(out)
    finally:
        resp.close()


def _exec_on_running_pods(username: str, script: str, args: list, tag: str, stdin_data: Optional[str] = None) -> dict:
    """사용자의 Running Pod 마다 script 를 root 로 실행한다. 인자는 셸 문자열에 끼우지 않고 위치
    인자로 넘기고, 드러나면 안 되는 값은 stdin_data 로 넘긴다. 부가 효과라 예외를 던지지 않는다 —
    반환: {"synced": [...], "failed": [...]}. Pod 목록부터 못 읽으면 "대상 Pod 없음"과 구분되도록
    "error": "POD_LIST_FAILED" 를 더한다."""
    result = {"synced": [], "failed": []}
    namespace = app.config["NAMESPACE"]
    try:
        load_k8s()
        v1 = client.CoreV1Api()
        pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"username={username}").items
    except Exception:
        app.logger.exception(f"[{tag}] Pod 목록 조회 실패: user={username}")
        result["error"] = "POD_LIST_FAILED"
        return result

    for pod in pods:
        if pod.status.phase != "Running":
            continue
        name = pod.metadata.name
        command = ["/bin/sh", "-c", script, "decs-group-sync", username, *args]
        try:
            if stdin_data is None:
                out = stream(
                    v1.connect_get_namespaced_pod_exec, name, namespace, command=command,
                    stderr=True, stdin=False, stdout=True, tty=False,
                    _request_timeout=POD_GROUP_SYNC_TIMEOUT_SEC,
                ) or ""
            else:
                out = _exec_reading_stdin(v1, name, namespace, command, stdin_data) or ""
        except Exception:
            app.logger.exception(f"[{tag}] exec 실패: pod={name}")
            result["failed"].append(name)
            continue
        if "__RC=0" in out:
            result["synced"].append(name)
        else:
            app.logger.warning(f"[{tag}] 반영 실패: pod={name} args={args} out={out.strip()}")
            result["failed"].append(name)
    return result


def sync_running_pod_groups(username: str, groups: dict) -> dict:
    """이미 떠 있는 사용자 Pod 의 /etc/group 에 새 보조 그룹을 더한다(admin_infra_server#25).

    Pod 의 /etc/group 은 기동 시 DECS_SUPPLEMENTAL_GROUPS 로 한 번만 채워지므로, 승인 뒤에도
    재생성 전까지 새 그룹이 보이지 않는다. 새로 여는 세션부터 반영되고, 이미 떠 있는 프로세스와
    컨테이너 재시작(Pod 의 env 는 그대로)에는 반영되지 않는다.

    groups: {그룹 이름: gid}. 반환은 _exec_on_running_pods 와 같다."""
    if not groups:
        return {"synced": [], "failed": []}
    specs = [f"{name}:{gid}" for name, gid in sorted(groups.items())]
    return _exec_on_running_pods(username, _POD_GROUP_SYNC_SCRIPT, specs, "POD GROUP SYNC")


def remove_running_pod_groups(username: str, groups: list) -> dict:
    """이미 떠 있는 사용자 Pod 의 /etc/group 에서 보조 그룹 멤버십을 뺀다. 권한 판정은 NAS 가 AD 로
    하므로 이것은 id·ls 표시와 ~/shared 링크 정리(다음 로그인 때)를 맞추는 용도다.

    groups: 그룹 이름 목록. 반환은 _exec_on_running_pods 와 같다."""
    if not groups:
        return {"synced": [], "failed": []}
    return _exec_on_running_pods(username, _POD_GROUP_REMOVE_SCRIPT, sorted(groups), "POD GROUP REMOVE")

# 이미지 entrypoint.sh 가 처음 기동할 때 하는 것과 같은 방식(chpasswd -e)으로 해시를 넣는다. 해시는
# 표준입력 한 줄로 받는다 — 인자로 넘기면 Pod 안의 프로세스 목록에 보인다. 호출 전에 SHA-512 crypt
# 형식으로 검증돼 콜론·줄바꿈이 없다.
_POD_PASSWORD_SYNC_SCRIPT = r'''
u="$1"
IFS= read -r h || { echo "__RC=1"; exit 0; }
if printf '%s:%s\n' "$u" "$h" | chpasswd -e; then echo "__RC=0"; else echo "__RC=1"; fi
'''


def sync_running_pod_password(username: str, passwd_hash: str) -> dict:
    """이미 떠 있는 사용자 Pod 의 /etc/shadow 에 새 로그인 비밀번호 해시를 넣는다.

    Pod 의 비밀번호는 기동 때 계정 Secret(USER_PW_HASH)으로 한 번만 설정되므로, Secret 만 바꾸면
    다음 재생성 전까지 옛 비밀번호가 남는다. 반환은 _exec_on_running_pods 와 같다."""
    return _exec_on_running_pods(username, _POD_PASSWORD_SYNC_SCRIPT, [], "POD PASSWORD SYNC",
                                 stdin_data=passwd_hash + "\n")


def update_account_secrets(username: str, passwd_hash: str) -> list:
    """사용자의 모든 Pod(상태 무관)의 계정 Secret 에서 USER_PW_HASH 를 바꾼다. 멈춘 Pod·다시 만드는 Pod
    (마이그레이션은 옛 Pod 의 Secret 을 이어받는다)도 새 비밀번호로 뜨게 한다. 반환: 바꾼 Secret 이름 목록.

    Secret 은 Pod 를 소유자로 두어 Pod 와 함께 지워지므로, Secret 목록(list 권한 없음) 대신 Pod 목록에서
    이름을 만든다. Secret 이 없는 Pod(만드는 중)는 건너뛴다. 그 밖의 실패는 예외로 던진다 — 같은
    요청을 다시 보내면 이어서 끝난다."""
    from lifecycle_steps.provision import account_secret_name  # provision 이 main 을 거쳐 utils 를 import 한다
    namespace = app.config["NAMESPACE"]
    load_k8s()
    v1 = client.CoreV1Api()
    pods = v1.list_namespaced_pod(namespace=namespace, label_selector=f"username={username}").items
    updated = []
    for pod in pods:
        name = account_secret_name(pod.metadata.name)
        try:
            v1.patch_namespaced_secret(name, namespace, {"stringData": {"USER_PW_HASH": passwd_hash}})
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
            continue
        updated.append(name)
    return sorted(updated)


def generate_pod_name(username: str) -> str:
    suffix = uuid.uuid4().hex[:8]
    pod_name = f"ailab-{username}-{suffix}"

    app.logger.debug(f"Generated pod name: {pod_name}")

    return pod_name


def delete_pod_util(pod_name, namespace):

    app.logger.info(f"[POD DELETE] deleting pod={pod_name} namespace={namespace}")

    try:
        load_k8s()
        v1 = client.CoreV1Api()
        # Pod 삭제
        v1.delete_namespaced_pod(pod_name, namespace)

        app.logger.info(f"[POD DELETE] pod deleted: {pod_name}")

    except Exception:
        app.logger.exception(f"[POD DELETE] failed for pod={pod_name}")
        raise


# ============================
#  NodePort Service 관련
# ============================

def create_nodeport_services(username: str, namespace: str, pod_name: str, extra_ports: List[dict]):
    """
    사용자 Pod용 NodePort Service 생성 (여러 포트 지원)

    Args:
        username: 사용자명
        namespace: k8s 네임스페이스
        extra_ports: [{"internal_port": 8888, "external_port": 10001, "usage_purpose": "jupyter"}, ...]
    """
    app.logger.info(
        f"[SERVICE CREATE] username={username} pod={pod_name} ports={extra_ports}"
    )

    load_k8s()
    v1 = client.CoreV1Api()

    for port_info in extra_ports:
        internal_port = port_info["internal_port"]  # Pod 내부 포트
        external_port = port_info["external_port"]  # NodePort (10000-15000)
        purpose = port_info.get("usage_purpose", "custom")

        service_name = f"ailab-{username}-{purpose}-{external_port}"

        app.logger.debug(
            f"[SERVICE CREATE] service={service_name} "
            f"{internal_port}->{external_port}"
        )

        service_body = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=service_name,
                namespace=namespace,
                labels={
                    "app": "ailab-nodeport",
                    "username": username,
                    "pod_name": pod_name,
                    "purpose": purpose
                }
            ),
            spec=client.V1ServiceSpec(
                type="NodePort",
                selector={"pod_name": pod_name},
                ports=[client.V1ServicePort(
                    name=purpose,
                    protocol="TCP",
                    port=internal_port,
                    target_port=internal_port,
                    node_port=external_port
                )]
            )
        )

        try:
            # 기존 Service가 있으면 삭제 후 재생성
            try:
                v1.delete_namespaced_service(service_name, namespace)
                app.logger.debug(f"[SERVICE CREATE] old service deleted {service_name}")
            except client.exceptions.ApiException as e:
                if e.status != 404:
                    raise

            v1.create_namespaced_service(namespace, service_body)
            app.logger.info(
                f"[SERVICE CREATE] created {service_name} "
                f"nodeport={external_port}"
            )
        except Exception as e:
            app.logger.exception(f"[SERVICE CREATE] failed for {service_name}")
            raise


def delete_nodeport_services(pod_name: str, namespace: str):
    """사용자 Pod 삭제 시 관련 NodePort Service도 모두 삭제"""
    app.logger.info(f"[SERVICE DELETE] pod={pod_name}")
    load_k8s()
    v1 = client.CoreV1Api()

    try:
        # username 라벨로 모든 관련 Service 조회
        services = v1.list_namespaced_service(
            namespace=namespace,
            label_selector=f"pod_name={pod_name},app=ailab-nodeport"
        )

        app.logger.debug(
            f"[SERVICE DELETE] {len(services.items)} services found"
        )

        for svc in services.items:
            v1.delete_namespaced_service(svc.metadata.name, namespace)
            app.logger.info(
                f"[SERVICE DELETE] deleted service {svc.metadata.name}"
            )
    except Exception as e:
        app.logger.exception(f"[SERVICE DELETE] failed for pod {pod_name}: {e}")
        raise


# ============================
#  Docker / Image 관련
# ============================

def load_user_image(username: str, base_image: str) -> str:
    """
    NFS에 tar가 있으면 docker load 실행, 성공 시 user-{username}:latest 사용
    """
    image_dir = app.config.get("IMAGE_STORE_DIR", "/image-store/images")
    docker_bin = app.config.get("DOCKER_BIN", "/usr/bin/docker")
    tar_path = os.path.join(image_dir, f"user-{username}.tar")

    if not os.path.exists(tar_path):
        app.logger.info(f"[{username}] no saved image → base image")
        save_image_metadata(username, status="base_used", path=tar_path)
        return base_image

    try:
        subprocess.run([docker_bin, "load", "-i", tar_path], check=True)
        app.logger.info(f"[{username}] image loaded from {tar_path}")
        save_image_metadata(username, status="loaded", path=tar_path)
        return f"user-{username}:latest"
    except Exception as e:
        app.logger.exception(f"[{username}] docker load failed")
        save_image_metadata(username, status="load_failed", path=tar_path)
        return base_image

def commit_and_save_user_image(username, pod_name, namespace):
    """
    User Pod 내부에서 save_image.sh 실행
    """
    load_k8s()
    v1 = client.CoreV1Api()

    try:
        app.logger.info(f"[{username}] exec save_image.sh in pod {pod_name}")

        stream(
            v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            command=["/usr/local/bin/save_image.sh"],
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )

        # 성공했다고 가정 (실패 시 stream에서 예외 발생)
        save_image_metadata(
            username=username,
            status="success",
            version=int(datetime.utcnow().timestamp())
        )

        return True

    except Exception as e:
        app.logger.exception(f"[{username}] image save failed")
        save_image_metadata(username, status="error")
        return False


# ============================
#  Group / Volume 관련
# ============================
# ---- File lock helpers ----
def _local_lockfile_path(nfs_path: str) -> str:
    """NFS 경로에 대응하는 로컬(/tmp) 락 파일 경로를 반환한다.
    NFS 위에서는 flock/lockf 모두 NFS lockd 의존으로 불안정하므로,
    락 자체는 로컬 파일시스템에서 수행한다."""
    safe = nfs_path.replace("/", "_")
    return f"/tmp/cssh_lock{safe}"


_thread_locks_guard = threading.Lock()
_thread_locks = {}


def _thread_lock_for_path(path: str):
    lock_path = _local_lockfile_path(path)
    with _thread_locks_guard:
        if lock_path not in _thread_locks:
            _thread_locks[lock_path] = threading.RLock()
        return _thread_locks[lock_path]


class LockedFile:
    """Context manager for file locks using a local(/tmp) lock file.
    NFS 마운트 위의 파일을 안전하게 읽고 쓰기 위해 락은 로컬 파일로 관리한다."""
    def __init__(self, path: str, mode: str):
        self.path = path
        self.mode = mode
        self.f = None
        self._lock_f = None
        self._thread_lock = None

    def __enter__(self):
        lock_type = fcntl.LOCK_SH if "r" in self.mode and "+" not in self.mode and "w" not in self.mode and "a" not in self.mode else fcntl.LOCK_EX
        self._thread_lock = _thread_lock_for_path(self.path)
        self._thread_lock.acquire()
        try:
            self._lock_f = open(_local_lockfile_path(self.path), "a+")
            fcntl.lockf(self._lock_f.fileno(), lock_type)
            self.f = open(self.path, self.mode)
            return self.f
        except Exception:
            if self._lock_f:
                try:
                    fcntl.lockf(self._lock_f.fileno(), fcntl.LOCK_UN)
                finally:
                    self._lock_f.close()
                    self._lock_f = None
            self._thread_lock.release()
            self._thread_lock = None
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.f:
                self.f.close()
        finally:
            try:
                if self._lock_f:
                    fcntl.lockf(self._lock_f.fileno(), fcntl.LOCK_UN)
                    self._lock_f.close()
            finally:
                if self._thread_lock:
                    self._thread_lock.release()

# ---- 원장(passwd/group/shadow) 교차 Pod 잠금 ----
# 원장은 NFS(/kube_share)에 있고 API 서버·제어기·크론잡이 서로 다른 Pod에서 고친다. LockedFile의
# 로컬(/tmp) 락은 같은 Pod 안에서만 통하므로, 원장 전체를 하나의 MySQL 이름 잠금으로 묶어
# 읽기부터 쓰기까지를 Pod 사이에서도 직렬화한다. 같은 스레드가 겹쳐 잡으면 바깥 한 번만 잡는다.
LEDGER_LOCK_NAME = "cssh-ledger"
LEDGER_LOCK_TIMEOUT_SEC = int(os.getenv("LEDGER_LOCK_TIMEOUT_SEC", "30"))
_ledger_local = threading.local()
# LEDGER_LOCK_BACKEND=local(시험용)에서 쓰는 프로세스 안 잠금.
_ledger_process_lock = threading.Lock()
_ledger_log = logging.getLogger(__name__)


class LedgerLockTimeout(RuntimeError):
    pass


def _ledger_lock_connection():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        autocommit=True,
        connect_timeout=5,
        read_timeout=LEDGER_LOCK_TIMEOUT_SEC + 10,
    )


def _acquire_ledger_lock():
    """잠금을 잡고 풀 때 쓸 연결을 돌려준다. 못 잡으면 잠금 없이 진행하지 않고 예외를 던진다."""
    if os.getenv("LEDGER_LOCK_BACKEND", "mysql") == "local":
        if not _ledger_process_lock.acquire(timeout=LEDGER_LOCK_TIMEOUT_SEC):
            raise LedgerLockTimeout(f"ledger lock not acquired within {LEDGER_LOCK_TIMEOUT_SEC}s")
        return None
    conn = _ledger_lock_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, %s)", (LEDGER_LOCK_NAME, LEDGER_LOCK_TIMEOUT_SEC))
            (got,) = cur.fetchone()
    except Exception:
        conn.close()
        raise
    if got != 1:
        conn.close()
        raise LedgerLockTimeout(f"ledger lock not acquired within {LEDGER_LOCK_TIMEOUT_SEC}s")
    return conn


def _release_ledger_lock(conn) -> None:
    if conn is None:
        _ledger_process_lock.release()
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK(%s)", (LEDGER_LOCK_NAME,))
    except Exception:
        # 연결을 닫으면 MySQL이 잠금을 풀어 주므로 해제 실패는 기록만 한다.
        _ledger_log.exception("ledger lock release failed; closing connection releases it")
    finally:
        conn.close()


@contextmanager
def ledger_lock():
    depth = getattr(_ledger_local, "depth", 0)
    if depth:
        _ledger_local.depth = depth + 1
        try:
            yield
        finally:
            _ledger_local.depth -= 1
        return
    conn = _acquire_ledger_lock()
    _ledger_local.depth = 1
    try:
        yield
    finally:
        _ledger_local.depth = 0
        _release_ledger_lock(conn)

# ---- Ensure base etc layout ----

def ensure_dir(path: str) -> None:
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def ensure_file(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "a"):
            pass


def ensure_seeded_file(path: str, template_name: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

    template_dir = app.config.get("BASE_ETC_TEMPLATE_DIR", DEFAULT_BASE_ETC_TEMPLATE_DIR)
    template_path = os.path.join(template_dir, template_name)

    thread_lock = _thread_lock_for_path(path)
    with thread_lock:
        with open(_local_lockfile_path(path), "a+") as lock_f:
            fcntl.lockf(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                with open(path, "a+", encoding="utf-8") as f:
                    f.seek(0, os.SEEK_END)
                    if f.tell() > 0:
                        return

                    if not os.path.exists(template_path):
                        app.logger.warning("[ETC INIT] template missing for %s: %s", path, template_path)
                        return

                    with open(template_path, "r", encoding="utf-8") as tf:
                        content = tf.read()

                    f.seek(0)
                    f.write(content)
                    f.truncate()
                    app.logger.info("[ETC INIT] seeded %s from %s", path, template_path)
            finally:
                fcntl.lockf(lock_f.fileno(), fcntl.LOCK_UN)

def ensure_etc_layout() -> None:
    ensure_dir(app.config["BASE_ETC_DIR"])
    ensure_dir(app.config["SUDOERS_DIR"])
    ensure_seeded_file(app.config["PASSWD_PATH"], "passwd")
    ensure_seeded_file(app.config["GROUP_PATH"], "group")
    ensure_seeded_file(app.config["SHADOW_PATH"], "shadow")
    ensure_seeded_file(app.config["BASH_LOGOUT_PATH"], "bash.bash_logout")
    ensure_seeded_file(app.config["BASHRC_PATH"], "bashrc")

# ---- /etc/passwd & /etc/group parsing ----
PASSWD_FIELDS = ["name","passwd","uid","gid","gecos","home","shell"]
GROUP_FIELDS = ["name","passwd","gid","members"]

_passwd_line_re = re.compile(r"^(?P<name>[^:]+):(?P<passwd>[^:]*):(?P<uid>\d+):(?P<gid>\d+):(?P<gecos>[^:]*):(?P<home>[^:]*):(?P<shell>[^\n]*)$")
_group_line_re  = re.compile(r"^(?P<name>[^:]+):(?P<passwd>[^:]*):(?P<gid>\d+):(?P<members>[^\n]*)$")


def read_passwd_lines() -> List[str]:
    ensure_etc_layout()
    with ledger_lock(), LockedFile(app.config["PASSWD_PATH"], "r") as f:
        return f.read().splitlines()


def write_passwd_lines(lines: List[str]) -> None:
    ensure_etc_layout()
    with ledger_lock(), LockedFile(app.config["PASSWD_PATH"], "r+") as f:
        content = "\n".join(lines) + "\n" if lines and not lines[-1].endswith("\n") else "\n".join(lines)
        f.seek(0)
        f.write(content)
        f.truncate()


def read_group_lines() -> List[str]:
    ensure_etc_layout()
    with ledger_lock(), LockedFile(app.config["GROUP_PATH"], "r") as f:
        return f.read().splitlines()


def write_group_lines(lines: List[str]) -> None:
    ensure_etc_layout()
    with ledger_lock(), LockedFile(app.config["GROUP_PATH"], "r+") as f:
        content = "\n".join(lines) + "\n" if lines and not lines[-1].endswith("\n") else "\n".join(lines)
        f.seek(0)
        f.write(content)
        f.truncate()


def parse_passwd_line(line: str) -> Optional[dict]:
    m = _passwd_line_re.match(line)
    if not m:
        return None
    d = m.groupdict()
    d["uid"] = int(d["uid"]) if d["uid"].isdigit() else d["uid"]
    d["gid"] = int(d["gid"]) if d["gid"].isdigit() else d["gid"]
    return d


def format_passwd_entry(d: dict) -> str:
    return f"{d['name']}:{d.get('passwd','x')}:{int(d['uid'])}:{int(d['gid'])}:{d.get('gecos','')}:{d.get('home','')}:{d.get('shell','')}"


def parse_group_line(line: str) -> Optional[dict]:
    m = _group_line_re.match(line)
    if not m:
        return None
    d = m.groupdict()
    d["gid"] = int(d["gid"]) if d["gid"].isdigit() else d["gid"]
    d["members"] = [x for x in d["members"].split(",") if x]
    return d


def format_group_entry(d: dict) -> str:
    members = ",".join(d.get("members", []))
    return f"{d['name']}:{d.get('passwd','x')}:{int(d['gid'])}:{members}"

# ---- /etc/shadow parsing ----
_shadow_line_re = re.compile(r"^(?P<name>[^:]+):(?P<passwd>[^:]*):(?P<lastchg>\d*):(?P<min>\d*):(?P<max>\d*):(?P<warn>\d*):(?P<inactive>\d*):(?P<expire>\d*):(?P<flag>[^\n:]*)$")


def read_shadow_lines() -> List[str]:
    ensure_etc_layout()
    with ledger_lock(), LockedFile(app.config["SHADOW_PATH"], "r") as f:
        return f.read().splitlines()


def write_shadow_lines(lines: List[str]) -> None:
    ensure_etc_layout()
    with ledger_lock(), LockedFile(app.config["SHADOW_PATH"], "r+") as f:
        content = "\n".join(lines) + "\n" if lines and not lines[-1].endswith("\n") else "\n".join(lines)
        f.seek(0)
        f.write(content)
        f.truncate()


def parse_shadow_line(line: str) -> Optional[dict]:
    m = _shadow_line_re.match(line)
    if not m:
        return None
    d = m.groupdict()
    # Convert numeric fields if present
    for k in ["lastchg", "min", "max", "warn", "inactive", "expire"]:
        if d.get(k):
            try:
                d[k] = int(d[k])
            except ValueError:
                pass
    return d


def format_shadow_entry(d: dict) -> str:
    # Fill defaults similar to Debian/Ubuntu: min=0, max=99999, warn=7
    return (
        f"{d['name']}:{d['passwd']}:{d.get('lastchg', 0)}:"
        f"{d.get('min', 0)}:{d.get('max', 99999)}:{d.get('warn', 7)}:"
        f"{d.get('inactive', '')}:{d.get('expire', '')}:{d.get('flag', '')}"
    )


_VALID_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def ensure_sudoers_dir():
    ensure_etc_layout()


def ensure_sudoers_file(sudoers_dir: str, username: str, policy: str) -> str:
    if not _VALID_USERNAME_RE.match(username):
        raise ValueError(f"invalid username for sudoers: {username!r}")

    os.makedirs(sudoers_dir, exist_ok=True)
    target = os.path.join(sudoers_dir, username)
    lockfile = target + ".lock"

    tmp = target + f".tmp.{os.getpid()}"
    with LockedFile(lockfile, "a+") as _:
        if os.path.exists(target) and os.path.getsize(target) > 0:
            return target
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o440)
        with os.fdopen(fd, "w") as tf:
            tf.write(policy if policy.endswith("\n") else policy + "\n")
        os.replace(tmp, target)
    return target


def _nas_ssh_client():
    import paramiko
    ssh = paramiko.SSHClient()
    # 배포 때 수집한 호스트 키가 있으면 그것만 믿는다(모르는 키면 접속 거절). 없으면 예전처럼 받아들인다.
    known_hosts = os.environ.get("SSH_KNOWN_HOSTS_FILE", "/etc/ssh-known-hosts/known_hosts")
    if os.path.isfile(known_hosts) and os.path.getsize(known_hosts) > 0:
        ssh.load_host_keys(known_hosts)
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=os.environ["NAS_SSH_HOST"],
        port=int(os.environ.get("NAS_SSH_PORT", "22")),
        username=os.environ["NAS_SSH_USER"],
        key_filename=os.environ["NAS_SSH_KEY_PATH"],
    )
    return ssh


def _ssh_run(ssh, cmd: str) -> None:
    _, stdout, stderr = ssh.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    if exit_code != 0:
        raise RuntimeError(
            f"NAS SSH command failed (exit {exit_code}): {cmd}\n"
            f"{stderr.read().decode(errors='replace')}"
        )


def _ssh_capture(ssh, cmd: str) -> tuple:
    """실패를 예외로 올리지 않고 (종료코드, 표준출력)을 그대로 돌려준다. 대상이 없을 때의
    0이 아닌 종료코드를 정상 흐름으로 다뤄야 하는 조회에 쓴다."""
    _, stdout, _ = ssh.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    return exit_code, stdout.read().decode(errors="replace").strip()


class HomeOwnerMismatch(RuntimeError):
    """이미 있는 홈의 소유자가 배정하려는 uid 와 달라서 덮어쓰지 않고 멈춘 경우."""


TEAM_DIR_PREFIX = "_g_"


def _user_home_path(username: str) -> str:
    """홈 경로를 만들기 전에 이름부터 검증한다. 이 경로는 원격 셸 명령에 그대로 들어가고 그중 하나는
    되돌릴 수 없는 삭제다 — 이름에 공백이나 셸 특수문자가 섞이면 의도하지 않은 대상을 지울 수 있다.
    이름은 admin_be가 만들고 내부 토큰으로 보호되지만, 지우는 쪽에 방어를 두는 편이 맞다."""
    if not _VALID_USERNAME_RE.match(username):
        raise ValueError(f"invalid username for home directory: {username!r}")
    # 팀 디렉터리와 같은 자리에 놓이므로 이 접두어는 홈으로 쓰지 않는다 — 지우는 경로가 팀 데이터를 지운다.
    if username.startswith(TEAM_DIR_PREFIX):
        raise ValueError(f"username uses the reserved team directory prefix: {username!r}")
    return f"{os.environ['NFS_USER_SHARE_PATH']}/{username}"


def team_dir_path(group_name: str) -> str:
    if not _VALID_USERNAME_RE.match(group_name):
        raise ValueError(f"invalid group name for team directory: {group_name!r}")
    return f"{os.environ['NFS_USER_SHARE_PATH']}/{TEAM_DIR_PREFIX}{group_name}"


class TeamDirGroupMismatch(RuntimeError):
    """이미 있는 팀 디렉터리의 그룹이 배정하려는 gid 와 달라서 덮어쓰지 않고 멈춘 경우."""


def create_team_directory(group_name: str, gid: int) -> None:
    """팀 공유 디렉터리(/home/_g_<팀>)를 root:<gid> 2770 으로 만든다. 여러 번 불러도 같다.

    user-share 전체가 Pod 의 /home 에 마운트되므로 Pod 쪽은 바꿀 것이 없다. setgid 비트가
    새 파일의 그룹을 팀으로 고정하고, 로그인 umask 002 가 그룹 쓰기를 남긴다 — 사용자가
    chgrp 할 일이 없다(#154). other 는 막는다. 팀 밖 사용자가 읽을 이유가 없다.

    이미 있는 디렉터리의 그룹이 다르면 다른 팀의 데이터일 수 있어 건드리지 않는다."""
    path = team_dir_path(group_name)
    quoted = shlex.quote(path)
    app.logger.info(f"[NAS SSH] ensuring team dir {path} gid={gid}")
    with _nas_ssh_client() as ssh:
        code, current = _ssh_capture(ssh, f"stat -c %g {quoted}")
        if code == 0 and current.isdigit() and int(current) != int(gid):
            raise TeamDirGroupMismatch(
                f"팀 디렉터리 {path} 가 이미 gid {current} 소유인데 {gid} 로 배정하려 했습니다. "
                f"다른 팀의 데이터일 수 있어 덮어쓰지 않습니다. NAS 에서 소유 그룹을 확인하십시오."
            )
        _ssh_run(ssh, f"sudo mkdir -p {quoted}")
        _ssh_run(ssh, f"sudo chown 0:{int(gid)} {quoted}")
        _ssh_run(ssh, f"sudo chmod 2770 {quoted}")


def create_user_home_directory(username: str, uid: int, gid: int) -> None:
    """홈을 만들고 소유권을 배정한다. 이미 있는 홈의 소유자가 배정하려는 uid 와 다르면
    덮어쓰지 않고 HomeOwnerMismatch 로 멈춘다.

    NAS 가 Kerberos 주체를 푸는 값이 계정 대장의 uid 와 다를 수 있다. 실제로 구 운영에서
    넘어온 계정은 NAS 캐시에 옛 uid 가 남아 있어서, 대장이 준 새 uid 로 chown 하면
    NAS 판정 기준으로는 소유자가 아닌 상태가 되어 사용자가 자기 홈을 통째로 잃는다
    (2026-09-19 yoon6yo 사례: NAS 는 20016, 대장은 21000). 그 경우 Pod 는 Running 이고
    sshd 도 뜨기 때문에 내부 점검으로는 드러나지 않는다.

    NAS 가 쓰는 값은 NAS 에서 `id -u 'FARM\\<사용자명>'` 으로 확인한다."""
    path = _user_home_path(username)
    quoted = shlex.quote(path)
    app.logger.info(f"[NAS SSH] creating home dir {path} uid={uid} gid={gid}")
    with _nas_ssh_client() as ssh:
        # 상위 디렉터리가 755 라 홈이 700 이어도 조회는 된다. 없으면 0 이 아닌 코드가 온다.
        code, current = _ssh_capture(ssh, f"stat -c %u {quoted}")
        if code == 0 and current.isdigit() and int(current) != int(uid):
            app.logger.error(
                f"[NAS SSH] home owner mismatch {path}: nas={current} requested={uid}"
            )
            raise HomeOwnerMismatch(
                f"{username} 의 홈이 이미 uid {current} 소유인데 {uid} 로 배정하려 했습니다. "
                f"덮어쓰면 이 사용자가 홈에 접근하지 못합니다. "
                f"NAS 에서 id -u 'FARM\\{username}' 으로 실제 값을 확인한 뒤, "
                f"계정 대장의 uid 를 그 값으로 맞추거나 홈을 옮기고 다시 시도하십시오."
            )
        _ssh_run(ssh, f"sudo mkdir -p {quoted}")
        _ssh_run(ssh, f"sudo chown {int(uid)}:{int(gid)} {quoted}")
        _ssh_run(ssh, f"sudo chmod 700 {quoted}")


NAS_GSS_FLUSH_COMMAND = "sudo -n /usr/local/sbin/decs-nfs-flush-gss"


def nas_shared_gids_for_users(usernames, gid_min: int, gid_max) -> dict:
    """NAS 가 지금 각 사용자의 보조 그룹으로 보고 있는 공용 대역 gid 들.

    NFS 그룹 판정은 NAS 가 winbind 로 직접 한다(티켓 PAC 은 쓰이지 않는다 — 2026-09-22 실측,
    #153). 그래서 "NAS 가 아직 모르는가"를 판정할 수 있는 곳도 NAS 뿐이다.

    이름이 AD 에 없으면(레거시 계정 등) `id` 가 실패한다. 그런 사용자는 결과에서 빠지므로
    호출자가 "모름"과 "그룹 없음"을 구분할 수 있다."""
    if not usernames:
        return {}
    out = {}
    with _nas_ssh_client() as ssh:
        # 한 세션 안에서 한 번에 묻는다. 사용자 수만큼 SSH 를 새로 열면 재조정 한 번이 분 단위가 된다.
        script = "\n".join(
            'echo "%s|$(id -G %s 2>/dev/null)"' % (u, shlex.quote("FARM\\" + u))
            for u in sorted(usernames)
        )
        _, text = _ssh_capture(ssh, script)
    for line in text.splitlines():
        name, _, gids = line.partition("|")
        gids = gids.strip()
        if not gids:
            continue          # id 실패 — AD 에 없는 이름. "그룹 없음"과 구분해 빼 둔다.
        out[name] = {int(g) for g in gids.split()
                     if g.isdigit() and gid_min <= int(g) and (gid_max is None or int(g) <= gid_max)}
    return out


def nas_flush_gss_cache() -> None:
    """NAS 의 GSS 컨텍스트 캐시를 비워 클라이언트가 컨텍스트를 다시 맺게 한다.

    NAS 는 컨텍스트를 맺을 때 그룹 목록을 한 번 풀어 auth.rpcsec.context 에 고정하고,
    컨텍스트 수명(= 티켓 수명 24h) 동안 다시 조회하지 않는다. 이걸 비우지 않으면 AD 를
    고쳐도 이미 떠 있는 Pod 에 최대 하루 반영되지 않는다(#153).

    비우는 파일이 0600 root 라 NAS 에 전용 스크립트를 두고 NOPASSWD 로 연다
    (admin_infra_server kerberos-nfs/script/nas/decs-nfs-flush-gss). chmod 로 잠깐 열었다
    닫는 우회는 그 사이 NAS 로컬 사용자 누구나 쓸 수 있어 쓰지 않는다."""
    app.logger.info("[NAS SSH] flushing GSS context cache")
    with _nas_ssh_client() as ssh:
        _ssh_run(ssh, NAS_GSS_FLUSH_COMMAND)


def delete_user_home_directory(username: str) -> None:
    path = _user_home_path(username)
    app.logger.info(f"[NAS SSH] deleting home dir {path}")
    with _nas_ssh_client() as ssh:
        _ssh_run(ssh, f"sudo rm -rf {shlex.quote(path)}")


def get_node_gpu_score(node: str, prom_url: str, timeout: float) -> float:
    """
    GPU 사용량 score
    - 낮을수록 여유 있음
    """
    import requests

    query = f"""
    (
      (avg(DCGM_FI_DEV_GPU_UTIL{{Hostname="{node}"}}) or vector(0)) +
      ((avg(DCGM_FI_DEV_FB_USED{{Hostname="{node}"}}) or vector(0)) / 1024) +
      ((avg(DCGM_FI_DEV_GPU_TEMP{{Hostname="{node}"}}) or vector(0)) / 100)
    )
    """

    try:
        resp = requests.get(
            f"{prom_url}/api/v1/query",
            params={"query": query},
            timeout=timeout
        )
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        if not result:
            return float("inf")
        return float(result[0]["value"][1])
    except Exception as e:
        app.logger.warning(f"[GPU SCORE] failed for node={node}: {e}")
        return float("inf")


def select_best_node_from_prometheus(node_list: List[str], prom_url: str, timeout: float):
    """Select the best node from a list based on Prometheus metrics

    Args:
        node_list: List of node names to evaluate
        prom_url: Prometheus server URL
        timeout: Request timeout in seconds

    Returns:
        str: Name of the best node, or None if all queries fail
    """
    import requests

    best_node = None
    best_score = float("inf")

    app.logger.debug(f"Starting Prometheus node selection for nodes: {node_list}")

    for node in node_list:
        query = f"""
        (
          (avg(DCGM_FI_DEV_GPU_UTIL{{Hostname="{node}"}}) or vector(0)) +
          ((avg(DCGM_FI_DEV_FB_USED{{Hostname="{node}"}}) or vector(0)) / 1024) +
          ((avg(DCGM_FI_DEV_GPU_TEMP{{Hostname="{node}"}}) or vector(0)) / 100)
        )
        """
        try:
            app.logger.debug(f"Querying Prometheus for node {node}")
            response = requests.get(f"{prom_url}/api/v1/query", params={"query": query}, timeout=timeout)
            prom_result = response.json()
            value = float(prom_result["data"]["result"][0]["value"][1])
            app.logger.debug(f"Node {node} score: {value}")
        except Exception as e:
            app.logger.debug(f"Failed to query node {node}: {e}")
            value = float("inf")

        if value < best_score:
            best_score = value
            best_node = node

    app.logger.debug(f"Best node selected: {best_node} with score: {best_score}")
    return best_node
