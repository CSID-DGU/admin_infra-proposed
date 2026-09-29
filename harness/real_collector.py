"""실스택에서 평가자의 collect(check, target) 를 채우는 수집기.

모든 관측은 system.py 의 server-state 경로(stack kube, stack probe)를 거친다. 로그인 관측은 데스크톱에서
돈다(--host local). 대상 시스템의 기록(admin_be requests.pod_name, users.ubuntu_uid, config-server 원장)은
읽지 않는다 (ADR-004). 대상 시스템이 붙인 username 라벨은 후보를 찾는 데에만 쓰고, 귀속은 로그인한 Pod
의 hostname 과 EndpointSlice 가 가리키는 Pod uid 로 한다.

비밀번호는 password_of(username) 로 관측할 때마다 받는다. 이 객체의 속성, repr, evidence, 예외 메시지에
넣지 않는다.

관측 하나를 부르지 못하면 그 관측에 기대는 검사만 UNKNOWN 이다. 못 본 것을 막혔다거나 열렸다고 적지 않는다.
"""
import system

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
RECLAMATION = ("login_blocked", "credential_blocked", "compute_blocked", "endpoint_blocked")

POD_COLUMNS = "custom-columns=NAME:.metadata.name,UID:.metadata.uid,HOSTIP:.status.hostIP,PHASE:.status.phase"
SERVICE_COLUMNS = "custom-columns=NAME:.metadata.name,PORT:.spec.ports[0].nodePort"
SLICE_COLUMNS = "custom-columns=UIDS:.endpoints[*].targetRef.uid"
NODE_COLUMNS = 'custom-columns=NAME:.metadata.name,IP:.status.addresses[?(@.type=="InternalIP")].address'


class _Unobserved(Exception):
    """관측 하나를 부르지 못했다. 메시지가 evidence 로 남는다."""


def _none(value):
    return None if value == "<none>" else value


class RealCollector:
    def __init__(self, host, namespace, *, password_of, clock, ttl_sec=20):
        self.host, self.namespace = host, namespace
        self._password_of, self._clock, self._ttl = password_of, clock, ttl_sec
        self._cache = {}

    def __repr__(self):
        return f"RealCollector({self.host!r}, {self.namespace!r})"

    def _kube(self, *args):
        """kubectl 출력의 줄마다 칸 목록."""
        try:
            row = system.stack_kube(self.host, self.namespace, [*args, "--no-headers"])
        except system.SystemCallFailed as e:
            raise _Unobserved(str(e)) from None
        if row.get("rc") != 0:
            raise _Unobserved(f"kubectl {args[1]} rc={row.get('rc')}: {row.get('stderr', '')}")
        return [line.split() for line in (row.get("stdout") or "").splitlines() if line.strip()]

    def _step(self, obs, name, fn):
        try:
            obs[name] = fn()
        except _Unobserved as e:
            obs[name], obs["errors"][name] = None, str(e)

    def _observe(self, username):
        obs = {"errors": {}}
        self._step(obs, "pods", lambda: [
            {"name": c[0], "uid": c[1], "host_ip": _none(c[2]), "phase": c[3]}
            for c in self._kube("get", "pods", "-l", f"username={username}", "-o", POD_COLUMNS)])
        self._step(obs, "service", lambda: next(
            ({"name": c[0], "node_port": int(c[1])} for c in self._kube(
                "get", "services", "-l", f"app=ailab-nodeport,username={username},purpose=ssh",
                "-o", SERVICE_COLUMNS)), None))
        service = obs["service"]
        if service is not None:
            self._step(obs, "backend_pod_uids", lambda: self._backends(service["name"]))
            if obs["pods"] is not None:
                self._step(obs, "address", lambda: self._address(obs["pods"]))
            if obs.get("address"):
                self._step(obs, "probe", lambda: self._probe(obs["address"], service["node_port"], username))
        return obs

    def _backends(self, service_name):
        uids = []
        for cols in self._kube("get", "endpointslices", "-l", f"kubernetes.io/service-name={service_name}",
                               "-o", SLICE_COLUMNS):
            uids += [u for u in cols[0].split(",") if _none(u) and u not in uids]
        return uids

    def _address(self, pods):
        """Pod 가 뜬 노드가 아닌 노드의 InternalIP. 자기 노드의 Pod 로 가는 트래픽은 NetworkPolicy 와 무관하게
        허용되므로 같은 노드에서 재면 차단을 못 본다."""
        pod_ips = {p["host_ip"] for p in pods}
        others = sorted(ip for c in self._kube("get", "nodes", "-o", NODE_COLUMNS)
                        if len(c) == 2 and (ip := _none(c[1])) and ip not in pod_ips)
        if not others:
            raise _Unobserved("Pod 가 뜬 노드가 아닌 노드의 InternalIP 가 없다")
        return others[0]

    def _probe(self, address, port, username):
        try:
            password = self._password_of(username)
        except Exception as e:
            raise _Unobserved(f"password_of 실패: {type(e).__name__}") from None
        try:
            row = system.stack_probe("local", address, port, username, password=password)
        except system.SystemCallFailed as e:
            raise _Unobserved(str(e).replace(password, "***")) from None
        probe = row.get("probe")
        if not isinstance(probe, dict) or probe.get("connect") not in ("ok", "auth_failed", "tcp_failed", "timeout"):
            raise _Unobserved(f"stack probe rc={row.get('rc')}: {str(row.get('stderr', ''))[:200].replace(password, '***')}")
        return probe

    def observation(self, username, phase):
        key = (username, phase)
        at, obs = self._cache.get(key, (None, None))
        now = self._clock()
        if at is None or now - at >= self._ttl:
            obs = self._observe(username)
            self._cache[key] = (now, obs)
        return obs

    def __call__(self, check, target):
        username = target["username"]
        obs = self.observation(username, "reclamation" if check in RECLAMATION else "creation")
        return _judge(check, obs)


def _connect(obs):
    """ssh 로그인 경로의 결과. no_service 는 ssh Service 가 없다고 관측한 것이고, None 은 관측하지 못한 것이다."""
    if obs.get("service") is None:
        return (None, obs["errors"].get("service")) if "service" in obs["errors"] else ("no_service", None)
    if obs.get("probe") is None:
        name = next(n for n in ("pods", "address", "probe") if n in obs["errors"])
        return None, obs["errors"][name]
    return obs["probe"]["connect"], obs["probe"].get("error")


def _pod_uid(obs, hostname):
    return next((p["uid"] for p in obs.get("pods") or [] if p["name"] == hostname), None)


def _judge(check, obs):
    connect, error = _connect(obs)
    base = {"connect": connect, "error": error}
    if check == "credential_blocked":
        return UNKNOWN, {"reason": "farm 노드 keytab 확인 경로 없음 (vasc-16)"}
    if check == "compute_blocked":
        if obs.get("pods") is None:
            return UNKNOWN, {"error": obs["errors"].get("pods")}
        return (FAIL if obs["pods"] else PASS), {"pods": [p["name"] for p in obs["pods"]]}
    if check == "login_blocked":
        result = {"ok": FAIL, "timeout": UNKNOWN, None: UNKNOWN}.get(connect, PASS)
        return result, base
    if check == "endpoint_blocked":
        result = {"no_service": PASS, "tcp_failed": PASS, "timeout": UNKNOWN, None: UNKNOWN}.get(connect, FAIL)
        return result, {**base, "service": obs.get("service")}
    if check == "endpoint":
        uids = obs.get("backend_pod_uids") or []
        backend = (uids[0] if len(uids) == 1 else uids) if uids else None
        result = {"ok": PASS, "auth_failed": PASS, "timeout": UNKNOWN, None: UNKNOWN}.get(connect, FAIL)
        return result, {**base, "service": obs.get("service"), "address": obs.get("address"),
                        "backend_pod_uid": backend, "ambiguous": len(uids) > 1}
    if check == "login":
        return {"ok": PASS, "timeout": UNKNOWN, None: UNKNOWN}.get(connect, FAIL), {**base, **_ids(obs)}
    if check not in ("compute_uid", "storage_rw", "credential", "compute_gpu", "compute_nfs"):
        raise ValueError(f"unknown check: {check}")
    if connect != "ok":
        # 로그인 실패면 compute_uid 만 FAIL 이다. 나머지는 안에 들어가 보지 못했으므로 UNKNOWN 이다.
        failed = connect in ("auth_failed", "tcp_failed", "no_service")
        return (FAIL if check == "compute_uid" and failed else UNKNOWN), {**base, **_ids(obs)}
    c = obs["probe"]["checks"]
    ids = _ids(obs)
    if check == "compute_uid":
        out = c["uid"]["stdout"].strip() if c["uid"]["rc"] == 0 else ""
        uid = int(out) if out.isdigit() else None
        return (UNKNOWN if uid is None else PASS), {**ids, "runtime_uid": uid, "uid_rc": c["uid"]["rc"]}
    if check == "storage_rw":
        rw = c["home_rw"]
        return (PASS if all(rw.values()) else FAIL), {**ids, "home_rw": rw}
    if check == "credential":
        rc = c["krb"]["rc"]
        return (UNKNOWN if rc is None else PASS if rc == 0 else FAIL), {**ids, "klist_rc": rc}
    if check == "compute_gpu":
        gpu, ctx = c["gpu"], c["gpu_context"]
        detail = {**ids, "nvidia_smi_rc": gpu["rc"], "gpus": gpu.get("lines"),
                  "context_checked": ctx is not None, "context_rc": ctx["rc"] if ctx else None}
        if gpu["rc"] is None or (ctx is not None and ctx["rc"] is None):
            return UNKNOWN, detail
        ok = gpu["rc"] == 0 and (gpu.get("lines") or 0) >= 1 and (ctx is None or ctx["rc"] == 0)
        return (PASS if ok else FAIL), detail
    mount = c["home_mount"]
    cols = mount["stdout"].split() if mount["rc"] == 0 else []
    if len(cols) != 2:
        return UNKNOWN, {**ids, "mount_source": None, "findmnt_rc": mount["rc"]}
    source, fstype = cols
    return (PASS if fstype.startswith("nfs") else FAIL), {**ids, "mount_source": source, "fstype": fstype}


def _ids(obs):
    checks = (obs.get("probe") or {}).get("checks") or {}
    host = checks.get("hostname") or {}
    hostname = host.get("stdout", "").strip() if host.get("rc") == 0 else None
    return {"hostname": hostname, "pod_uid": _pod_uid(obs, hostname) if hostname else None}
