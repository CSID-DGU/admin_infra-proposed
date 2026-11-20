"""GPU 유실 점검 CronJob.

GPU 디바이스는 컨테이너 생성 시점에만 주입되고 그 이후 호스트에서 재생성되면
갱신되지 않는다 (#139/#140 참고). 이미 떠 있는 컨테이너를 무중단으로 복구할 방법이
없어서, 자동 재시작 대신 GPU가 사라진 걸 감지하면 관리자에게 Slack으로만 알리고
실제 재시작/재생성은 사람이 직접 판단해서 진행한다 (#146 — 자동 재시작은 사용자
동의 없이 진행 중인 작업을 강제로 끊을 수 있어 채택하지 않음).

점검 항목: ① Pod에 배정된 GPU(NVIDIA_VISIBLE_DEVICES의 UUID)가 컨테이너 안에서 하나씩 다 보이는지
② Pod의 sshd가 응답하는지. 문제가 있으면 같은 Pod는 1시간에 한 번만 알린다. GPU가 사라졌을 때는 그 시점의
nvidia-smi 출력과 노드 커널 로그를 ConfigMap에 남긴다. 노드 재부팅(boot id 변경)을 감지하면 그 노드의
SSH·NFS·GPU 점검 결과와 내려간 Pod 목록을 알린다. ③ 실행 중인 사용자 Pod마다 컨테이너 안에서 사용자 계정으로
홈(NFS)을 읽어 본다. 읽히지 않는 Pod는 한 메시지에 모아 알리고, 그 Pod의 홈은 1시간 동안 다시 읽지 않는다.
어느 경우에도 Pod를 자동으로 건드리지 않는다.
"""
from main import app, load_k8s, admin_be_headers
from adapters import gpu_check_state, node_diag
from kubernetes import client
from kubernetes.stream import stream
import requests

import os
import re
import socket
import time

# 같은 차트를 다른 네임스페이스에 띄우면 그 네임스페이스의 Pod만 점검해야 한다. 하드코딩돼
# 있으면 다른 스택의 크론잡이 운영 Pod를 점검하고 운영 채널로 알림을 보낸다.
NAMESPACE = os.getenv("NAMESPACE", "ailab-infra")
GPU_POD_LABEL_SELECTOR = "has-gpu=true"
EXEC_TIMEOUT_SEC = 15
SSH_PORT = 22
SSH_TIMEOUT_SEC = 5
REALERT_INTERVAL_SEC = 3600
CRON_PERIOD_SEC = 600
# 실행 제한(300초) 안에 끝나도록, 노드 커널 로그 수집은 시작 뒤 이 시간이 지나면 하지 않는다.
DIAG_BUDGET_SEC = 150
# 홈 읽기: timeout 이 이 시간 뒤에 TERM 을 보내고, 그래도 남아 있으면 NFS_CHECK_KILL_AFTER_SEC 뒤에 KILL 을 보낸다.
# 둘을 더한 값이 EXEC_TIMEOUT_SEC 보다 작아야 종료 코드를 받는다.
NFS_CHECK_TIMEOUT_SEC = 10
NFS_CHECK_KILL_AFTER_SEC = 2
# 홈 읽기에 실패한 Pod 는 이 시간 동안 다시 읽지 않는다. NFS 가 멈춰 있으면 읽던 프로세스가 끝나지 못하고
# 컨테이너에 남는데, 실행(10분)마다 다시 읽으면 그런 프로세스가 계속 쌓인다.
HOME_RECHECK_SEC = REALERT_INTERVAL_SEC
# 실행 제한(300초) 안에 끝나도록, 시작 뒤 이 시간이 지나면 남은 Pod 의 홈 읽기는 다음 실행으로 미룬다.
# NAS 가 응답하지 않으면 Pod 하나에 EXEC_TIMEOUT_SEC 씩 걸린다.
HOME_CHECK_DEADLINE_SEC = 240
# MIG- UUID 는 nvidia-smi --query-gpu=uuid 에 나오지 않아(부모 GPU- 만 나온다) 하나씩 비교할 수 없다.
_UUID_PREFIXES = ("GPU-",)
_SAFE_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def assigned_gpus(pod):
    """Pod에 배정된 GPU UUID 목록. GPU가 없거나(void) UUID로 적혀 있지 않아 하나씩 비교할 수 없으면 None."""
    for container in pod.spec.containers or []:
        if container.name != "shell":
            continue
        for env in container.env or []:
            if env.name == "NVIDIA_VISIBLE_DEVICES":
                items = [i.strip() for i in (env.value or "").split(",") if i.strip()]
                if items and all(i.startswith(_UUID_PREFIXES) for i in items):
                    return items
                return None
    return None


def missing_gpus(assigned, visible_output):
    """배정됐는데 컨테이너 안에서 보이지 않는 GPU. `nvidia-smi --query-gpu=uuid` 출력의 UUID 줄과 하나씩 맞춘다."""
    visible = {line.strip() for line in (visible_output or "").splitlines() if line.strip().startswith(_UUID_PREFIXES)}
    return [g for g in assigned if g not in visible]


def tcp_ssh_alive(host, port=SSH_PORT, timeout=SSH_TIMEOUT_SEC) -> bool:
    """sshd가 배너를 돌려주는지. 인증은 하지 않는다."""
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(4) == b"SSH-"
    except OSError:
        return False


def _exec(v1, pod_name, command) -> str:
    return stream(
        v1.connect_get_namespaced_pod_exec, pod_name, NAMESPACE, container="shell",
        command=command, stderr=True, stdin=False, stdout=True, tty=False,
        _request_timeout=EXEC_TIMEOUT_SEC)


def _exec_rc(v1, pod_name, command):
    """sh -c 로 실행해 (출력, 종료코드). stream 은 종료코드를 돌려주지 않아 직접 붙인다.
    종료코드 뒤에 다른 출력이 더 올 수 있다(timeout 이 su 를 죽이면 su 가 "Killed"를 뒤늦게 찍는다)."""
    out = _exec(v1, pod_name, ["/bin/sh", "-c", command + '; echo "__RC=$?"']) or ""
    head, sep, tail = out.rpartition("__RC=")
    code = re.match(r"-?\d+", tail)
    if not sep or not code:
        return out.strip(), -1
    return (head + tail[code.end():]).strip(), int(code.group())


def _read_home(v1, pod_name, user):
    """컨테이너 안에서 사용자 계정으로 홈을 읽어 본다. (판정, 설명)을 돌려준다. 판정은 "ok"·"fail"·"unknown"이다.

    krb5 마운트는 root 가 아니라 사용자 티켓으로 열리므로 su 로 그 사용자가 되어 읽는다. 생성 직후 점검
    (step_verify_home_io)은 홈에 파일을 썼다 지우지만, 이 점검은 사용 중인 홈에 파일을 만들지 않으려고 읽기만 한다.
    stat -f 는 캐시를 쓰지 않고 NAS 에 요청(STATFS)을 보내고, ls 는 홈 목록을 읽는다.
    timeout 이 su 를 끝내도 읽던 프로세스가 남아 출력 통로를 잡고 있으면, 종료코드는 받지만 exec 는
    EXEC_TIMEOUT_SEC 까지 기다린다. exec 자체가 실패하면 예외를 그대로 올린다(부른 쪽이 판단 불가로 다룬다)."""
    started = time.monotonic()
    out, rc = _exec_rc(v1, pod_name,
                       f"timeout -k {NFS_CHECK_KILL_AFTER_SEC} {NFS_CHECK_TIMEOUT_SEC} "
                       f"su -s /bin/sh {user} -c 'stat -f ~ >/dev/null && ls ~ >/dev/null'")
    if rc == 0:
        return "ok", ""
    if rc in (124, 137):  # timeout 이 TERM(124) 또는 KILL(137)로 끝냈다
        return "fail", f"{NFS_CHECK_TIMEOUT_SEC}초 안에 읽히지 않음"
    if rc == -1:
        # 종료코드를 받지 못했다. exec 제한 시간까지 기다렸다면 읽기가 끝나지 않은 것이고, 그 전에 끊겼다면 알 수 없다.
        if time.monotonic() - started >= EXEC_TIMEOUT_SEC - 1:
            return "fail", f"{EXEC_TIMEOUT_SEC}초 안에 읽기가 끝나지 않음"
        return "unknown", "점검 명령의 종료코드를 받지 못함"
    if rc in (126, 127):  # 컨테이너에 timeout·su 가 없거나 실행할 수 없다
        return "unknown", f"점검 명령을 실행할 수 없음 (rc={rc})"
    last_line = out.splitlines()[-1].strip()[:150] if out else ""
    return "fail", f"읽기 실패 (rc={rc}{': ' + last_line if last_line else ''})"


def check_gpu_pods() -> None:
    load_k8s()
    v1 = client.CoreV1Api()
    now = time.time()
    state = gpu_check_state.CheckState(v1, NAMESPACE, app.logger, now)
    kernel_logs = {}

    try:
        pods = v1.list_namespaced_pod(NAMESPACE, label_selector=GPU_POD_LABEL_SELECTOR).items
    except Exception as e:
        app.logger.error(f"[GPU CHECK] GPU pod 목록 조회 실패: {e}")
        return

    app.logger.info(f"[GPU CHECK] 점검 대상 {len(pods)}개")
    state.prune_alerts({p.metadata.name for p in pods}, now, REALERT_INTERVAL_SEC)
    gpu_by_node = {}

    for pod in pods:
        pod_name = pod.metadata.name
        if pod.status.phase != "Running":
            app.logger.debug(f"[GPU CHECK] Running 아님, 스킵: pod={pod_name} phase={pod.status.phase}")
            continue

        username = (pod.metadata.labels or {}).get("username", "unknown")
        node_name = pod.spec.node_name
        problems, kinds = [], []
        gpu_lost = False
        node_gpu = gpu_by_node.setdefault(node_name, {"ok": 0, "lost": 0, "unknown": 0})

        assigned = assigned_gpus(pod)
        if assigned is None:
            app.logger.debug(f"[GPU CHECK] 배정 GPU를 UUID로 알 수 없어 GPU 비교 스킵: pod={pod_name}")
            node_gpu["unknown"] += 1
        else:
            try:
                output = _exec(v1, pod_name, ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"])
                lost = missing_gpus(assigned, output)
                if lost:
                    gpu_lost = True
                    node_gpu["lost"] += 1
                    kinds.append("gpu")
                    problems.append(f"GPU 유실: 배정 {len(assigned)}개 중 {len(lost)}개가 보이지 않음 ({', '.join(lost)})")
            except Exception as e:
                # exec 자체가 실패하는 건 sshd/컨테이너 상태 문제일 수 있어 GPU 유실로
                # 단정하지 않는다 — 로그만 남기고 SSH 점검은 계속한다.
                app.logger.warning(f"[GPU CHECK] exec 실패, GPU 상태 판단 불가: pod={pod_name} — {e}")
                node_gpu["unknown"] += 1
            else:
                if not lost:
                    node_gpu["ok"] += 1

        if not tcp_ssh_alive(pod.status.pod_ip):
            kinds.append("ssh")
            problems.append("SSH 접속 불가: sshd가 응답하지 않음")

        if not problems:
            app.logger.debug(f"[GPU CHECK] 정상: pod={pod_name}")
            continue

        app.logger.error(f"[GPU CHECK] 이상 감지: pod={pod_name} username={username} node={node_name} {problems}")
        key = f"{pod_name}:{'+'.join(kinds)}"  # 문제 종류가 바뀌면 억제 중이어도 새로 알린다
        if not state.should_alert(key, now, REALERT_INTERVAL_SEC):
            continue
        if not app.config["INFRA_SLACK_WEBHOOK_URL"]:
            # 알릴 수 없는데 진단 Pod 를 띄우지 않는다(알림이 안 나가면 매 실행마다 반복된다).
            app.logger.warning(f"[GPU CHECK] INFRA_SLACK_WEBHOOK_URL 미설정 — 알림·진단 스킵: pod={pod_name}")
            continue
        diag = _save_gpu_diagnostics(v1, pod, node_name, kernel_logs, now) if gpu_lost else None
        if _notify_pod_problems(pod_name, username, node_name, problems, diag):
            state.mark_alerted(key, now)
            state.save()  # 중간에 제한 시간으로 끊겨도 보낸 알림이 다시 나가지 않게 알릴 때마다 남긴다

    _check_node_reboots(v1, state, gpu_by_node)
    state.save()
    _check_homes(v1, state, now)
    state.save()
    state.report_degraded(now, _send_slack)


def _save_gpu_diagnostics(v1, pod, node_name, kernel_logs, started):
    """GPU 유실 시점의 nvidia-smi 출력과 노드 커널 로그를 남긴다. 어느 쪽이 실패해도 알림은 막지 않는다.
    커널 로그는 노드마다 한 번만 모으고(kernel_logs 에 담는다), 실행 제한에 닿지 않게 시간 예산 안에서만 모은다."""
    sections = {}
    try:
        sections["nvidia-smi"] = _exec(v1, pod.metadata.name, ["nvidia-smi"])
    except Exception as e:
        sections["nvidia-smi"] = f"collect failed: {e}"
    if node_name not in kernel_logs:
        if time.time() - started > DIAG_BUDGET_SEC:
            kernel_logs[node_name] = "skipped: run time budget exhausted"
        else:
            try:
                kernel_logs[node_name] = node_diag.kernel_gpu_log(
                    v1, NAMESPACE, app.config["IMAGE_HELPER_IMAGE"], node_name)
            except Exception as e:
                kernel_logs[node_name] = f"collect failed: {e}"
    sections["kernel-log"] = kernel_logs[node_name]
    try:
        return gpu_check_state.save_diagnostics(v1, NAMESPACE, pod, sections)
    except Exception as e:
        app.logger.warning(f"[GPU CHECK] 진단 자료 저장 실패: pod={pod.metadata.name} — {e}")
        return None


def _notify_pod_problems(pod_name, username, node_name, problems, diag) -> bool:
    lines = ["▶ " + p for p in problems]
    message = (
        ":warning: *Pod 이상 감지*\n"
        f"▶ pod: {pod_name}\n"
        f"▶ 사용자: {username}\n"
        f"▶ 노드: {node_name}\n"
        + "\n".join(lines) + "\n"
        "▶ 자동 재시작은 하지 않습니다 (사용자 작업 중일 수 있음) — 확인 후 필요 시 사용자와 조율해서 "
        "pod을 재생성해주세요. 같은 pod은 1시간에 한 번만 알립니다."
    )
    if diag:
        message += f"\n▶ 진단 자료(nvidia-smi·커널 로그): ConfigMap {diag}"
    return _send_slack(message, f"pod={pod_name}")


def _node_internal_ip(node):
    return next((a.address for a in node.status.addresses or [] if a.type == "InternalIP"), None)


def _is_user_pod(pod) -> bool:
    return bool((pod.metadata.labels or {}).get("username"))


def _pod_is_down(pod) -> bool:
    """사용자 Pod 가 꺼져 있는가. 끝난 Pod(Succeeded)와 크론잡·진단 Pod 같은 사용자 Pod 가 아닌 것은 세지 않는다."""
    if not _is_user_pod(pod) or pod.status.phase == "Succeeded":
        return False
    if pod.status.phase != "Running":
        return True
    return any(not cs.ready for cs in pod.status.container_statuses or [])


def _check_node_reboots(v1, state, gpu_by_node) -> None:
    """노드의 boot id가 지난 점검과 다르면 재부팅이다. 처음 보는 노드는 기준만 기록하고 알리지 않는다."""
    try:
        nodes = v1.list_node().items
        all_pods = v1.list_namespaced_pod(NAMESPACE).items
    except Exception as e:
        app.logger.warning(f"[GPU CHECK] 노드·Pod 목록 조회 실패, 재부팅 감지 스킵: {e}")
        return

    for node in nodes:
        name = node.metadata.name
        boot_id = node.status.node_info.boot_id if node.status and node.status.node_info else None
        if not boot_id:
            continue
        previous = state.boot_ids.get(name)
        if previous == boot_id:
            continue
        if previous is None:
            state.set_boot_id(name, boot_id)
            continue
        on_node = [p for p in all_pods if p.spec.node_name == name]
        if _notify_node_reboot(name, _node_report(v1, node, on_node, gpu_by_node.get(name))):
            state.set_boot_id(name, boot_id)
            state.save()


def _nfs_state(v1, on_node) -> str:
    """재부팅한 노드의 NFS 홈이 사용자 권한으로 읽히는가. 그 노드에서 실행 중인 사용자 Pod 하나를 골라
    컨테이너 안에서 홈을 읽어 본다(_read_home)."""
    running = next((p for p in on_node if _home_check_target(p)), None)
    if running is None:
        return "확인 불가(점검에 쓸 실행 중 Pod 없음)"
    try:
        verdict, why = _read_home(v1, running.metadata.name, running.metadata.labels["username"])
    except Exception as e:
        return f"확인 실패 ({str(e)[:100]})"
    if verdict == "ok":
        return "정상"
    return f"확인 불가 ({why})" if verdict == "unknown" else f"이상 (홈 {why})"


def _home_check_target(pod) -> bool:
    """홈을 읽어 볼 Pod 인가. 실행 중이고 지워지는 중이 아닌 사용자 Pod 만 본다. 계정 이름은 셸 명령에 넣으므로
    형식이 맞는 것만 받는다."""
    return (_is_user_pod(pod) and pod.status.phase == "Running"
            and getattr(pod.metadata, "deletion_timestamp", None) is None
            and bool(_SAFE_USERNAME.fullmatch(pod.metadata.labels["username"])))


def _check_homes(v1, state, now) -> None:
    """실행 중인 사용자 Pod 마다 컨테이너 안에서 사용자 계정으로 홈(NFS)을 읽어 본다.

    NAS 가 응답하지 않거나 사용자의 Kerberos 티켓이 만료되면 Pod 는 Running 이고 sshd 도 응답하지만 홈이
    읽히지 않는다. 읽히지 않는 Pod 는 한 메시지에 모아 알린다(NAS 가 응답하지 않으면 모든 Pod 가 함께 실패한다).
    실패한 Pod 의 홈은 HOME_RECHECK_SEC 동안 다시 읽지 않고, 그 뒤에 다시 읽어 여전히 실패하면 다시 알린다."""
    if not state.home_check_allowed(now, HOME_RECHECK_SEC):
        return
    try:
        pods = [p for p in v1.list_namespaced_pod(NAMESPACE).items if _home_check_target(p)]
    except Exception as e:
        app.logger.warning(f"[GPU CHECK] Pod 목록 조회 실패, 홈 읽기 점검 스킵: {e}")
        return
    state.prune_home_failures({p.metadata.name for p in pods})

    postponed = 0
    for pod in pods:
        pod_name = pod.metadata.name
        previous = state.home_failures.get(pod_name)
        if previous and now - previous.get("at", 0) < HOME_RECHECK_SEC:
            continue
        if time.time() - now > HOME_CHECK_DEADLINE_SEC:
            postponed += 1
            continue
        try:
            verdict, why = _read_home(v1, pod_name, pod.metadata.labels["username"])
        except Exception as e:
            verdict, why = "unknown", str(e)[:100]
        if verdict == "fail":
            app.logger.error(f"[GPU CHECK] 홈 읽기 실패: pod={pod_name} node={pod.spec.node_name} — {why}")
            state.record_home_failure(pod_name, now, why)
        else:
            if verdict == "unknown":
                app.logger.warning(f"[GPU CHECK] 홈 읽기 판단 불가: pod={pod_name} — {why}")
            state.clear_home_failure(pod_name)
    if postponed:
        app.logger.warning(f"[GPU CHECK] 시간이 모자라 홈 읽기 {postponed}개를 다음 실행으로 미룸")
    # 알림을 보내지 못해도 "실패한 Pod 는 다시 읽지 않는다"는 지켜지도록, 알리기 전에 실패 기록부터 저장한다.
    state.save()

    failed = [p for p in pods if p.metadata.name in state.home_failures]
    pending = [p for p in failed if not state.home_failures[p.metadata.name].get("alerted")]
    if pending and _notify_home_failures(pending, state.home_failures, len(pods), len(failed)):
        state.mark_home_alerted([p.metadata.name for p in pending])


def _notify_home_failures(pending, records, total, failed_count) -> bool:
    lines = [f"▶ {p.spec.node_name} {p.metadata.name} (사용자 {p.metadata.labels['username']}): "
             f"{records[p.metadata.name].get('why', '')}"
             for p in sorted(pending, key=lambda p: (p.spec.node_name or "", p.metadata.name))]
    message = (
        ":warning: *사용자 홈(NFS) 읽기 이상*\n"
        + "\n".join(lines) + "\n"
        f"▶ 실행 중인 사용자 Pod {total}개 중 {failed_count}개의 홈이 컨테이너 안에서 읽히지 않습니다.\n"
        "▶ NAS와 해당 노드의 NFS client 상태를 확인해주세요. Pod는 자동으로 건드리지 않습니다. "
        "같은 Pod는 1시간에 한 번만 알리고, 그동안은 그 Pod의 홈을 다시 읽지 않습니다."
    )
    return _send_slack(message, f"home pods={len(pending)}")


def _gpu_state(counts) -> str:
    """이번 점검에서 이 노드의 GPU Pod 로 본 GPU 상태. 노드 전체 GPU 수는 알 수 없어(Pod 에 배정된 것만 보인다) 그렇게 밝힌다."""
    if not counts or not (counts["ok"] or counts["lost"]):
        return "확인 불가(GPU를 비교할 수 있는 Pod 없음)"
    text = f"GPU Pod 점검 {counts['ok'] + counts['lost']}개 중 유실 {counts['lost']}개"
    return text + (f", 판단 불가 {counts['unknown']}개" if counts["unknown"] else "")


def _node_ssh_state(node) -> str:
    """노드 sshd 응답. 노드는 22 가 아닌 포트(노드마다 다름)로 sshd 를 열어 두므로 설정의 FARM_NODES 에서
    host:port 를 찾는다. 설정에 없으면 포트를 모르므로 22 로 찔러 "응답 없음"이라고 거짓 경보를 내지 않고
    확인 불가로 남긴다."""
    entry = next((n for n in app.config["FARM_NODES"] if n.get("name") == node.metadata.name), None)
    if not entry or not entry.get("port"):
        return "확인 불가(이 노드의 SSH 포트가 설정에 없음)"
    host = entry.get("host") or _node_internal_ip(node)
    return "정상" if tcp_ssh_alive(host, int(entry["port"])) else "응답 없음"


def _node_report(v1, node, on_node, gpu_counts):
    down = [f"{p.metadata.name}({p.status.phase})" for p in on_node if _pod_is_down(p)]
    return {"ssh": _node_ssh_state(node), "nfs": _nfs_state(v1, on_node),
            "gpu": _gpu_state(gpu_counts), "down": down}


def _notify_node_reboot(node_name, report) -> bool:
    down = ", ".join(report["down"]) if report["down"] else "없음"
    message = (
        ":rotating_light: *서버 재부팅 감지*\n"
        f"▶ 노드: {node_name}\n"
        f"▶ SSH: {report['ssh']}\n"
        f"▶ NFS: {report['nfs']}\n"
        f"▶ GPU: {report['gpu']}\n"
        f"▶ 꺼져 있는 Pod: {down}\n"
        "▶ Pod는 자동으로 다시 만들지 않습니다 — 확인 후 필요한 것만 조치해주세요."
    )
    return _send_slack(message, f"node={node_name}")


def _send_slack(message, what) -> bool:
    webhook_url = app.config["INFRA_SLACK_WEBHOOK_URL"]
    if not webhook_url:
        app.logger.warning("[GPU CHECK] INFRA_SLACK_WEBHOOK_URL 미설정 — Slack 알림 스킵 (로그만 남김)")
        return False
    try:
        resp = requests.post(
            f"{app.config['ADMIN_BE_INTERNAL_URL']}/api/internal/slack/notify",
            json={"webhookUrl": webhook_url, "message": message},
            headers=admin_be_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        app.logger.error(f"[GPU CHECK] Slack 알림 전송 실패: {what} — {e}")
        return False


if __name__ == "__main__":
    with app.app_context():
        check_gpu_pods()
