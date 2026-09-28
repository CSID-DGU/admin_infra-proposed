"""사람이 실스택에서 측정을 돌리고, 한 신청의 진행을 보고, 남은 잠금을 푸는 명령.

    python3 harness/measure.py pair    --stack full --reps 3 --horizon 600 --poll 10 [--scenario C06] [--out DIR]
    python3 harness/measure.py logs    --stack full --request 812 [--since 30m]
    python3 harness/measure.py release --stack full --run <run_id> | --force

실제 일은 stack_lock, measure_ports, trial_runner, trial_results 가 한다. 이 파일은 연결과 출력만
한다. 설정은 레포 밖 파일(기본 ~/.ailab-exp/measure.env, MEASURE_ENV 로 바꾼다)에서 읽는다.
결과도 레포 밖에만 쓴다. 공개 레포에 사용자 이름과 운영 정보가 올라가지 않게 하려는 것이다.

비밀번호와 서명 키는 어디에도 출력하지 않는다. 결과 파일에 들어가면 trial_results.save 가 거절한다.
"""
import argparse
import datetime as dt
import functools
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fault_injector  # noqa: E402
import measure_ports  # noqa: E402
import stack_lock  # noqa: E402
import system  # noqa: E402
import trial_results  # noqa: E402
import trial_runner  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
KST = dt.timezone(dt.timedelta(hours=9))
STACKS = {"noprobe": "exp-np-", "full": "exp-fu-", "baseline": "exp-bl-"}  # ops/proposed-stack/uid-ranges.yaml
CONFIG_KEYS = ("KUBE_HOST", "ADMIN_INFRA_SERVER")
# ponytail: D1 이 정해지기 전까지 실스택은 FARM 하나뿐이라 서버 그룹을 고정한다.
SERVER_GROUP = "A"
EXPIRES_DAYS = 3


class ConfigError(Exception):
    pass


def _now_kst():
    return dt.datetime.now(KST)


def _say(*parts):
    print(_now_kst().isoformat(timespec="seconds"), *parts, flush=True)


def load_config():
    path = Path(os.environ.get("MEASURE_ENV") or Path.home() / ".ailab-exp" / "measure.env")
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                values[key.strip()] = value.strip().strip("'\"")
    missing = [k for k in CONFIG_KEYS if not values.get(k)]
    if missing:
        where = path if path.is_file() else f"{path} (파일 없음)"
        raise ConfigError(f"설정에 {', '.join(missing)} 가 없다: {where}")
    # system.cli_path 는 환경변수에서 체크아웃 경로를 읽는다.
    os.environ[system.ENV_VAR] = values["ADMIN_INFRA_SERVER"]
    return values


def _namespace(stack):
    return f"ailab-{stack}"


def _out_dir(arg, stack):
    out = Path(arg).expanduser() if arg else Path.home() / ".ailab-exp" / "measure" / stack
    out = out.resolve()
    if out == REPO_ROOT or REPO_ROOT in out.parents:
        raise ConfigError(f"--out 이 레포 안이다. 레포 밖 경로를 준다: {out}")
    return out


def revisions(host, stack):
    """측정한 판을 쿠버네티스 Deployment 이미지로 남긴다. 대상 시스템의 자기 진술이 아니다."""
    names = [f"config-server-{stack}", f"config-server-{stack}-controller", "admin-prod"]
    row = system.stack_kube(host, _namespace(stack), ["get", "deployment", *names, "-o", "json"])
    if row.get("rc") != 0:
        raise measure_ports.StackSqlError(f"Deployment 이미지를 읽지 못했다: {row.get('stderr', '')}")
    items = json.loads(row["stdout"]).get("items") or []
    found = {i["metadata"]["name"]: ",".join(c["image"] for c in i["spec"]["template"]["spec"]["containers"])
             for i in items}
    missing = [n for n in names if n not in found]
    if missing:
        raise measure_ports.StackSqlError(f"Deployment 이 없다: {missing}")
    return found


def cmd_pair(args, cfg):
    out = _out_dir(args.out, args.stack)
    host, ns, prefix = cfg["KUBE_HOST"], _namespace(args.stack), STACKS[args.stack]
    # 사용자 이름이 AD sAMAccountName 20자 제한 안에 들도록 run_id 를 짧게 둔다.
    run_id = secrets.token_hex(3)
    users, files = [], []
    try:
        with stack_lock.hold(host, ns, owner="measure", run_id=run_id, now=_now_kst().isoformat()):
            _say(f"{args.stack} run={run_id} 잠금을 잡았다")
            revs = revisions(host, args.stack)
            web = measure_ports.StackSql(host, ns, "web_admin")
            journal = measure_ports.StackSql(host, ns, "operation_state_db")
            jwt = system.stack_secret(host, ns, "jwt_secret")
            if jwt.get("rc") != 0 or not jwt.get("value"):
                raise measure_ports.StackSqlError(f"jwt_secret 을 읽지 못했다: {jwt.get('stderr', '')}")
            be = measure_ports.BeClient(host, ns, jwt["value"], clock=time.time)
            admin_id = measure_ports.create_user(
                web, username=None, email=f"m{run_id}-admin@example.com",
                password=secrets.token_urlsafe(24), role="ADMIN")
            for rep in range(1, args.reps + 1):
                username = f"{prefix}m{run_id}{rep:02d}"
                password = secrets.token_urlsafe(24)
                users.append(username)
                user_id = measure_ports.create_user(
                    web, username=username, email=f"{username}@example.com", password=password, role="USER")
                base = f"{args.stack}-{run_id}-{rep:02d}"
                _say(base, f"user={username} 를 만들었다")
                expires = (_now_kst() + dt.timedelta(days=EXPIRES_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
                ports = measure_ports.real_ports(
                    be=be, web_sql=web, user_id=user_id, admin_id=admin_id, expires_at=expires,
                    sleep=time.sleep, clock=time.monotonic, poll_sec=args.poll)
                submit = ports["create_submit"]

                def create_submit(submit=submit, base=base):
                    request_id = submit()
                    _say(f"{base}-c", f"request={request_id} 신청과 승인을 보냈다")
                    return request_id

                ports["create_submit"] = create_submit
                save = functools.partial(trial_results.save, out, secrets=[password, jwt["value"]])

                def save_and_say(record, save=save):
                    files.append(save(record))
                    _say(record["trial_id"], f"request={record['request_id']}",
                         f"declaration={record['system_declaration']['value']}",
                         f"verdict={record['independent_verdict']['at_horizon']['verdict']}")

                faults = {}
                if args.scenario:
                    # 장애는 시나리오의 operation 에 맞는 trial 에만 건다. 짝의 다른 trial 은 장애 없이 돈다.
                    kind = "create" if fault_injector.SCENARIOS[args.scenario][0] == "CREATE" else "revoke"
                    faults = {f"{kind}_scenario_id": args.scenario,
                              f"{kind}_fault": fault_injector.Fault(journal, scenario=args.scenario,
                                                                     username=username)}

                trial_runner.run_pair(
                    journal, create_trial_id=f"{base}-c", revoke_trial_id=f"{base}-r",
                    method=args.stack, server_group=SERVER_GROUP, horizon_sec=args.horizon,
                    repetition=rep, revisions=revs, username=username, save=save_and_say,
                    **faults, **ports)
    except stack_lock.LockError as e:
        print(f"잠금 문제로 멈췄다: {e}", file=sys.stderr)
        print(f"남은 잠금이면: python3 harness/measure.py release --stack {args.stack} --force", file=sys.stderr)
        return 3
    finally:
        for f in files:
            print(f"결과 파일: {f}")
        if users:
            # ponytail: Environment Resetter(vasc-10) 가 생기기 전까지는 사람이 지운다.
            print(f"사람이 정리할 사용자({ns}): {' '.join(users)}")
    return 0


def _parse_since(since):
    unit = {"s": 1, "m": 60, "h": 3600}[since[-1]]
    return int(since[:-1]) * unit


def _epoch_of_log(line):
    """kubectl logs --timestamps 의 첫 칸(RFC3339, 나노초)을 epoch 로. 없으면 None."""
    stamp, _, rest = line.partition(" ")
    try:
        head = stamp.rstrip("Z").split(".")[0]
        return dt.datetime.fromisoformat(head).replace(tzinfo=dt.timezone.utc).timestamp(), rest
    except ValueError:
        return None, line


def cmd_logs(args, cfg):
    host, ns = cfg["KUBE_HOST"], _namespace(args.stack)
    rid = int(args.request)
    web = measure_ports.StackSql(host, ns, "web_admin")
    rows = web.query("SELECT u.ubuntu_username AS username FROM requests r JOIN users u ON u.user_id = r.user_id"
                     " WHERE r.request_id = %s", (rid,))
    username = rows[0]["username"] if rows and rows[0]["username"] else None
    # UNIX_TIMESTAMP 는 세션 시간대를 반영하므로 서버 시간대와 무관하게 로그 시각과 맞출 수 있다.
    journal = measure_ports.StackSql(host, ns, "operation_state_db").query(
        "SELECT UNIX_TIMESTAMP(created_at) AS t, action, resource_type, phase, attempt, error_code"
        " FROM operation_log WHERE request_id = %s AND created_at >= NOW(3) - INTERVAL %s SECOND ORDER BY id",
        (str(rid), _parse_since(args.since)))
    events = [(float(r["t"]), "journal", " ".join(f"{k}={r[k]}" for k in
                                                   ("action", "resource_type", "phase", "attempt", "error_code")))
              for r in journal]
    log = system.stack_kube(host, ns, ["logs", f"deployment/config-server-{args.stack}-controller",
                                       "--timestamps", f"--since={args.since}"])
    if log.get("rc") != 0:
        print(f"제어기 로그를 읽지 못했다: {log.get('stderr', '')}", file=sys.stderr)
    needles = [str(rid)] + ([username] if username else [])
    for line in log.get("stdout", "").splitlines():
        if any(n in line for n in needles):
            t, rest = _epoch_of_log(line)
            if t is not None:
                events.append((t, "log", rest))
    for t, source, text in sorted(events, key=lambda e: e[0]):
        stamp = dt.datetime.fromtimestamp(t, KST).isoformat(timespec="milliseconds")
        print(stamp, f"request={rid}", source, text)
    return 0


def cmd_release(args, cfg):
    host, ns = cfg["KUBE_HOST"], _namespace(args.stack)
    try:
        if args.force:
            stack_lock.force_release(host, ns)
        else:
            stack_lock.release(host, ns, run_id=args.run)
    except stack_lock.LockError as e:
        print(f"잠금을 풀지 못했다: {e}", file=sys.stderr)
        return 3
    print(f"{ns} 의 {stack_lock.LOCK_NAME} 를 풀었다")
    return 0


def _positive(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError(f"양수여야 한다: {value}")
    return n


def _since(value):
    try:
        _parse_since(value)
    except (KeyError, ValueError, IndexError):
        raise argparse.ArgumentTypeError(f"30s, 30m, 2h 꼴이어야 한다: {value}") from None
    return value


def parser():
    p = argparse.ArgumentParser(prog="measure.py", description="실스택 측정 CLI")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, help_text):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--stack", required=True, choices=sorted(STACKS))
        return s

    pair = add("pair", "생성 trial 과 회수 trial 짝을 반복한다")
    pair.add_argument("--reps", type=_positive, default=1)
    pair.add_argument("--horizon", type=_positive, default=600, help="관측 구간 H (초)")
    pair.add_argument("--poll", type=_positive, default=10, help="선언 확인 간격 (초)")
    pair.add_argument("--scenario", choices=sorted(fault_injector.SCENARIOS),
                      help="장애 시나리오. 스택이 FAULT_INJECTION=1 로 떠 있어야 발동한다")
    pair.add_argument("--out", help="결과 디렉터리. 레포 밖이어야 한다")
    pair.set_defaults(func=cmd_pair)

    logs = add("logs", "한 신청의 저널 행과 제어기 로그를 시간 순으로 본다")
    logs.add_argument("--request", required=True, type=_positive)
    logs.add_argument("--since", type=_since, default="30m")
    logs.set_defaults(func=cmd_logs)

    release = add("release", "남은 잠금을 푼다")
    who = release.add_mutually_exclusive_group(required=True)
    who.add_argument("--run", help="자기 run_id. 잠금이 이 run 의 것일 때만 푼다")
    who.add_argument("--force", action="store_true", help="확인 없이 푼다")
    release.set_defaults(func=cmd_release)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        cfg = load_config()
        return args.func(args, cfg)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
