"""접근 검증 probe — VERIFY_MODE=full 전용 (Proposed-Full).

자원 상태 조회와 달리, 사용자 관점에서 실제 접근을 시험한다(논문 §3.4.1 live access test):
계정 권한 명령, 홈 I/O, 인증 티켓, GPU, 외부 endpoint. 다섯 시험을 모두 통과해야
생성이 완료로 기록되고, 회수는 접근 차단이 확인돼야 완료로 기록된다.

- 각 probe는 순수 관찰이라 재실행이 안전하다(main.RERUN_SAFE에 등록). 실패하면 재시도
  엔진이 VERIFY_MAX_ATTEMPTS까지 재시도하고, 소진되면 DEGRADED로 관리자에게 넘어간다 —
  작업 SUCCESS 행이 남지 않으므로 완료 기록이 차단된다.
- 근거(논문 §3.4.2): probe마다 operation_log에 VERIFY_* 행. resource_type이 시험 이름,
  error_detail(JSON)이 관찰값·범위, created_at이 관찰 시각이다.
- 회수 차단 판정: 연결 실패 단독은 증거가 아니다 — 시험 경로가 살아있음을 먼저 증명하고
  (노드 ssh 포트 접속), 자원 부재 증거(Pod·Service·포트 할당)와 일치할 때만 차단으로 본다.
"""
import json
import os
import socket
import time

from flask import current_app as app
from kubernetes import client
from kubernetes.stream import stream

from error import infra_error
from adapters.operation_log import Action, Phase, current_attempt, log_operation
from utils import load_k8s, get_db_connection

class _MainProxy:
    """main 속성의 늦은 바인딩 — main 로드를 첫 속성 접근 시점까지 미뤄 import 순서와
    무관하게 순환이 성립하지 않는다. StepFailed 등은 어차피 호출 시점에만 쓴다."""

    def __getattr__(self, name):
        import main
        return getattr(main, name)


_main = _MainProxy()

VERIFY_EXEC_TIMEOUT_SEC = float(os.getenv("VERIFY_EXEC_TIMEOUT_SEC", "20"))
VERIFY_TCP_TIMEOUT_SEC = float(os.getenv("VERIFY_TCP_TIMEOUT_SEC", "5"))
# 생성 직후 kinit 타이머·NFS 전파 지연이 있어 일반 단계(3회)보다 여유 있게 재시도한다.
VERIFY_MAX_ATTEMPTS = int(os.getenv("VERIFY_MAX_ATTEMPTS", "5"))
# 접속 포트(NodePort Service)는 만든 직후 노드에 규칙이 반영되기까지 1~2초 걸려 그 사이 접속이 거절된다.
# 거절·무응답일 때만 이 시간 안에서 다시 붙어 보고, 붙었는데 SSH가 아니면 곧바로 실패로 본다.
VERIFY_ENDPOINT_WAIT_SEC = float(os.getenv("VERIFY_ENDPOINT_WAIT_SEC", "20"))
VERIFY_ENDPOINT_INTERVAL_SEC = 1.0

# 화면의 "(현재 단계: …)"에 보일 생성 접근 시험 이름
PROBE_LABELS = {"uid": "계정 권한", "krb5_ticket": "인증 티켓", "home_io": "홈 읽기·쓰기",
                "gpu": "GPU", "endpoint": "외부 SSH 접속"}


# ---------- 저수준 관찰 도구 (테스트가 이 둘만 대역으로 바꾼다) ----------

def _sh(pod_name, command):
    """pod 안에서 sh -c 실행. (출력, 종료코드) 반환."""
    load_k8s()
    v1 = client.CoreV1Api()
    out = stream(
        v1.connect_get_namespaced_pod_exec, pod_name, app.config["NAMESPACE"],
        command=["/bin/sh", "-c", command + '; echo "__RC=$?"'],
        stderr=True, stdin=False, stdout=True, tty=False,
        _request_timeout=VERIFY_EXEC_TIMEOUT_SEC,
    )
    out = (out or "").strip()
    rc = -1
    if "__RC=" in out:
        tail = out.rsplit("__RC=", 1)
        out, rc = tail[0].strip(), int(tail[1].strip() or -1)
    return out, rc


def _tcp_check(host, port, expect_banner=None):
    """TCP 접속 시도. (연결 성공 여부, 배너 앞부분) 반환. 실패는 (False, 사유)."""
    try:
        with socket.create_connection((host, int(port)), timeout=VERIFY_TCP_TIMEOUT_SEC) as s:
            banner = ""
            if expect_banner:
                s.settimeout(VERIFY_TCP_TIMEOUT_SEC)
                try:
                    banner = s.recv(64).decode(errors="replace")
                except OSError:
                    pass
            return True, banner
    except OSError as e:
        return False, str(e)


# ---------- probe 공통 실행기 ----------

def _run_probe(ctx, action, probe, check):
    """check(ctx) -> (ok, detail). ok가 None이면 대상 아님(skip) — 근거를 남기고 통과."""
    request_id, username = ctx["request_id"], ctx["username"]
    pod_name = ctx.get("pod_name")
    log_operation(request_id=request_id, username=username, action=action,
                  phase=Phase.START, resource_type=probe, pod_name=pod_name)
    if action == Action.VERIFY_ACCESS:
        # 계정~Service 생성은 수 초라 화면은 곧바로 마지막 생성 단계를 보게 되고, 시험·재시도 동안
        # 그대로 멈춰 보인다. 시험마다 무엇을 몇 번째로 확인 중인지 남긴다. 표시용이라 실패해도 계속한다.
        try:
            _main.set_pod_creation_status(
                request_id, "verifying",
                f"접근 확인 중: {PROBE_LABELS.get(probe, probe)} (시도 {current_attempt.get()}/{VERIFY_MAX_ATTEMPTS})")
        except Exception:
            app.logger.warning("[VERIFY] pod status update failed", exc_info=True)
    try:
        ok, detail = check(ctx)
    except Exception as e:
        log_operation(request_id=request_id, username=username, action=action,
                      phase=_main._fail_phase(e), resource_type=probe, pod_name=pod_name,
                      error_code="VERIFY_TOOL_FAILED", error_detail=str(e)[:800])
        raise _main.StepFailed(infra_error(
            f"VERIFY_{probe.upper()}", "VERIFY_TOOL_FAILED", str(e), pod_name=pod_name), 500, cause=e)
    detail_json = json.dumps(detail, ensure_ascii=False, default=str)[:1000]
    if ok is None:
        log_operation(request_id=request_id, username=username, action=action,
                      phase=Phase.SUCCESS, resource_type=probe, pod_name=pod_name,
                      error_detail=detail_json)  # skip 사유도 근거로 남긴다
        return
    log_operation(request_id=request_id, username=username, action=action,
                  phase=Phase.SUCCESS if ok else Phase.FAIL, resource_type=probe,
                  pod_name=pod_name, error_detail=detail_json)
    if not ok:
        raise _main.StepFailed(infra_error(
            f"VERIFY_{probe.upper()}", f"VERIFY_{probe.upper()}_FAILED", detail,
            pod_name=pod_name), 500)


# ---------- 생성 검증 5종 ----------

def _account_uid(ctx):
    """시험 대상 계정의 uid. 이번 작업이 계정을 만들었으면 문맥에 있고, 기존 계정을 재사용한 작업
    (계정 단계 없음)은 계정 대장에서 읽는다. 대장에도 없으면 None."""
    if ctx.get("uid") is not None:
        return ctx["uid"]
    for line in _main.read_passwd_lines():
        entry = _main.parse_passwd_line(line)
        if entry and entry["name"] == ctx["username"]:
            ctx["uid"] = int(entry["uid"])
            return ctx["uid"]
    return None


_NO_ACCOUNT = {"scope": "passwd", "reason": "계정 대장에 사용자가 없음"}


def step_verify_uid(ctx):
    """① 발급된 계정 권한으로 명령이 실행되는가 — su 성공 + uid 일치."""
    def check(ctx):
        uid = _account_uid(ctx)
        if uid is None:
            return False, _NO_ACCOUNT
        expected = str(uid)
        out, rc = _sh(ctx["pod_name"], f"su -s /bin/sh {ctx['username']} -c 'id -u'")
        observed = out.splitlines()[-1].strip() if out else ""
        return (rc == 0 and observed == expected), {
            "scope": "pod exec su", "expected_uid": expected, "observed": observed, "rc": rc}
    _run_probe(ctx, Action.VERIFY_ACCESS, "uid", check)


def step_verify_home_io(ctx):
    """② 사용자 권한으로 홈에 쓰기/읽기 왕복 + 홈이 NFS 마운트인지(silent split 검출)."""
    def check(ctx):
        u, token = ctx["username"], f".verify-{ctx['request_id']}"
        # 소유권도 함께 관측한다 — sec=krb5 마운트에서 티켓이 없으면 NFS 클라이언트가 모든 파일을
        # nobody(65534)로 매핑하고 쓰기를 거부한다. 그 경우 "권한 문제"가 아니라 인증 문제다.
        # 구분자로 자른다 — 내용(첫 토큰에 "/"나 ":" 포함)으로 df 줄을 찾으면, stderr가 stdout에
        # 섞여 오는 _sh 특성상 "su: warning: ..." 같은 stderr 줄이 콜론 때문에 df 줄로 오인된다.
        # __SU=$?로 rm 실패(잔재가 쌓이는 사고)도 왕복 실패로 잡는다.
        cmd = (f"su -s /bin/sh {u} -c 'echo ok > ~/{token} && cat ~/{token} && rm ~/{token}'"
               f"; echo __SU=$?; echo __DF; df -P /home/{u} | tail -1; echo __OWNER; stat -c %u /home/{u}")
        out, rc = _sh(ctx["pod_name"], cmd)
        head, _, rest = out.partition("__DF")
        df_line, _, owner = rest.partition("__OWNER")
        roundtrip = "ok" in head.split() and "__SU=0" in head
        mount_src = (df_line.split() or [""])[0]
        owner_uid = owner.strip()
        # NFS 마운트 소스는 host:/path 꼴이다. 로컬 디스크에 조용히 쓰이는 사고를 여기서 잡는다.
        on_nfs = ":" in mount_src
        detail = {"scope": "pod exec su + df + stat", "roundtrip": roundtrip,
                  "mount_src": mount_src, "owner_uid": owner_uid, "stat_rc": rc}
        if not roundtrip and owner_uid == "65534":
            detail["likely_cause"] = "NFS가 소유자를 nobody로 매핑 — 인증 티켓 없음(krb5) 의심"
        return (roundtrip and on_nfs), detail
    _run_probe(ctx, Action.VERIFY_ACCESS, "home_io", check)


def step_verify_krb5(ctx):
    """③ 인증 티켓 — keytab은 pod에 없고 호스트 타이머가 갱신한 TGT를 /run/user/<uid>로
    공유받으므로, 사용자 권한 klist로 유효 티켓 존재를 확인한다(원장·keytab·타이머·마운트 관통)."""
    def check(ctx):
        u, uid = ctx["username"], _account_uid(ctx)
        if uid is None:
            return False, _NO_ACCOUNT
        cmd = (f"su -s /bin/sh {u} -c 'klist -s"
               f" || KRB5CCNAME=FILE:$(ls /run/user/{uid}/krb5cc* 2>/dev/null | head -1) klist -s'")
        out, rc = _sh(ctx["pod_name"], cmd)
        return rc == 0, {"scope": "pod exec klist", "rc": rc, "observed": out[-200:]}
    _run_probe(ctx, Action.VERIFY_ACCESS, "krb5_ticket", check)


def step_verify_gpu(ctx):
    """④ 신청한 GPU가 컨테이너 안에서 보이는가. GPU 미신청이면 대상 아님으로 기록하고 통과.
    기대 개수는 컨테이너가 실제로 배치된 노드의 num_gpu다. gpu_nodes는 배치 후보 목록이라 노드마다 GPU 수가
    달라서, 후보 중 최댓값을 쓰면 GPU가 적은 노드에 정상 배치된 컨테이너도 실패로 판정된다."""
    def check(ctx):
        gpu_nodes = (ctx.get("user_info") or {}).get("gpu_nodes") or []
        node = str(ctx.get("node") or "").lower()
        placed = [g for g in gpu_nodes if str(g.get("node_name") or "").lower() == node]
        if placed:
            want = int(placed[0].get("num_gpu") or 0)
        else:
            # 배치 노드를 후보 목록에서 찾지 못하면(이름 불일치 등) 가장 엄격한 기준으로 본다.
            want = max((int(g.get("num_gpu") or 0) for g in gpu_nodes), default=0)
        if want <= 0:
            return None, {"scope": "skip", "reason": "GPU 미신청"}
        out, rc = _sh(ctx["pod_name"], "nvidia-smi -L | wc -l")
        seen = int(out.splitlines()[-1].strip() or 0) if rc == 0 and out else 0
        return (rc == 0 and seen >= want), {
            "scope": "pod exec nvidia-smi", "node": ctx.get("node"), "placed_in_candidates": bool(placed),
            "requested": want, "visible": seen, "rc": rc}
    _run_probe(ctx, Action.VERIFY_ACCESS, "gpu", check)


def step_verify_endpoint(ctx):
    """⑤ 외부 접속 경로 — 노드 내부 IP의 SSH NodePort에 TCP 접속해 SSH 배너를 확인한다.
    (실행 위치는 제어기. 배포 서버 발신 경로와의 동등성은 하네스의 독립 검증이 재확인한다)"""
    def check(ctx):
        node = _main._get_farm_node_info(ctx["node"])
        ssh_ports = [p["external_port"] for p in ctx.get("allocated_ports") or []
                     if p.get("usage_purpose") == "ssh"]
        if not ssh_ports:
            return False, {"scope": "tcp", "reason": "ssh 포트 할당 없음"}
        deadline = time.monotonic() + VERIFY_ENDPOINT_WAIT_SEC
        tries = 0
        while True:
            tries += 1
            ok, banner = _tcp_check(node["host"], ssh_ports[0], expect_banner=True)
            if (ok and banner) or time.monotonic() >= deadline:
                break
            time.sleep(VERIFY_ENDPOINT_INTERVAL_SEC)
        return (ok and banner.startswith("SSH-")), {
            "scope": f"tcp {node['host']}:{ssh_ports[0]}", "connected": ok, "banner": banner[:40], "tries": tries}
    _run_probe(ctx, Action.VERIFY_ACCESS, "endpoint", check)


# 순서 주의: 인증 티켓을 홈 I/O보다 먼저 본다. 홈은 sec=krb5로 마운트되어 티켓이 없으면
# 쓰기가 거부되므로, 순서를 바꾸면 "홈 쓰기 실패"(증상)가 아니라 "티켓 없음"(원인)이 보고된다.
VERIFY_ACCESS_STEPS = [step_verify_uid, step_verify_krb5, step_verify_home_io,
                       step_verify_gpu, step_verify_endpoint]


# ---------- 회수 검증 ----------

def step_capture_access_targets(ctx):
    """회수 삭제가 시작되기 전에, 나중에 차단을 검사할 대상(노드·외부 포트)을 저장한다.
    삭제 후에는 할당 기록이 사라져 어느 포트를 검사해야 하는지 알 수 없다."""
    pod_name = ctx.get("pod_name")
    if not pod_name:
        return
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT node_port, node_name FROM nodeport_allocations WHERE pod_name=%s",
                        (pod_name,))
            rows = cur.fetchall()
    finally:
        conn.close()
    ctx["verify_ports"] = [r[0] for r in rows]
    ctx["verify_node"] = rows[0][1] if rows else ctx.get("node_name")


def step_verify_revoked(ctx):
    """회수 차단 확인. 연결 실패 단독은 증거가 아니다(논문 §5.1) — 순서:
    (1) 자원 부재 관찰: Pod 404 · Service 없음 · 포트 할당 행 없음
    (2) 시험 경로 생존 증명: 노드 ssh 포트 접속 성공 (실패면 판정 불능 → 재시도/DEGRADED)
    (3) 옛 NodePort 전부 닫힘
    세 관찰이 일치할 때만 차단으로 기록한다."""
    def check(ctx):
        pod_name = ctx["pod_name"]
        ns = app.config["NAMESPACE"]
        load_k8s()
        v1 = client.CoreV1Api()
        try:
            v1.read_namespaced_pod(pod_name, ns)
            pod_absent = False
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
            pod_absent = True
        services = v1.list_namespaced_service(ns, label_selector=f"pod_name={pod_name}").items
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM nodeport_allocations WHERE pod_name=%s", (pod_name,))
                db_rows = cur.fetchone()[0]
        finally:
            conn.close()
        detail = {"scope": "k8s+db+tcp", "pod_absent": pod_absent,
                  "services": len(services), "db_rows": db_rows}

        ports = ctx.get("verify_ports") or []
        node_name = ctx.get("verify_node") or ctx.get("node_name")
        if ports and node_name:
            node = _main._get_farm_node_info(node_name)
            path_ok, why = _tcp_check(node["host"], node["port"])  # 노드 ssh — 경로·클라이언트 생존
            detail["path_ok"] = path_ok
            if not path_ok:
                # 경로가 죽어 있으면 "포트가 닫혔다"를 판정할 수 없다 — 차단 성공으로 기록하지 않는다.
                return False, {**detail, "reason": f"시험 경로 판정 불능: {why}"}
            open_ports = [p for p in ports if _tcp_check(node["host"], p)[0]]
            detail["open_ports"] = open_ports
            blocked = not open_ports
        else:
            detail["path_ok"] = None  # 검사할 endpoint가 애초에 없던 회수 — 자원 부재로만 판정
            blocked = True
        return (pod_absent and not services and db_rows == 0 and blocked), detail
    _run_probe(ctx, Action.VERIFY_REVOKED, "revoked", check)


def step_verify_account_revoked(ctx):
    """계정 회수(delete_account) 검증 — 계정 원장과 keytab Secret이 실제로 사라졌는가."""
    def check(ctx):
        username = ctx["username"]
        in_passwd = any(l.split(":")[0] == username for l in _main.read_passwd_lines())
        load_k8s()
        try:
            client.CoreV1Api().read_namespaced_secret(
                name=f"krb5-keytab-{username}", namespace=app.config["NAMESPACE"])
            secret_absent = False
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
            secret_absent = True
        return (not in_passwd and secret_absent), {
            "scope": "passwd+secret", "in_passwd": in_passwd, "secret_absent": secret_absent}
    _run_probe(ctx, Action.VERIFY_REVOKED, "account_revoked", check)


# 순수 관찰 단계 — 재실행 안전(main.RERUN_SAFE에 합류)
RERUN_SAFE_STEPS = {f.__name__ for f in VERIFY_ACCESS_STEPS} | {
    "step_capture_access_targets", "step_verify_revoked", "step_verify_account_revoked"}
