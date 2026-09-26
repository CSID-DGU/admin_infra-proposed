"""E2E 도구 진입점.

  python -m harness.e2e list
  python -m harness.e2e run --stack full [--case C01 --case C04] [--allow-faults] [--keep]
  python -m harness.e2e reset --stack full --run <runid>

접속 정보는 저장소 밖의 ~/.ailab-exp/e2e.env(E2E_ENV로 변경)에 둔다:
  SSH_TARGET, SSH_PORT, SSH_KEY, KUBECONFIG, URL_<STACK>(예: URL_FULL), [CA_FILE], [INSECURE_TLS=1]
보고서는 ~/.ailab-exp/e2e-reports/<runid>.json 에 쓴다(공개 저장소에 올리지 않는다).
"""
import argparse
import json
import os
import pathlib
import string
import sys
import time

from . import catalog
from .faults import Faults
from .ports import BeApi, Cluster, load_env
from .resetter import Resetter, admin_email
from .runner import Run

_RANGES = pathlib.Path(__file__).resolve().parents[2] / "ops" / "proposed-stack" / "uid-ranges.yaml"


def stack_prefix(stack):
    import yaml
    for entry in yaml.safe_load(_RANGES.read_text()):
        if entry["stack"] == stack:
            return entry["prefix"]
    raise SystemExit(f"uid-ranges.yaml에 없는 스택: {stack}")


def new_run_id():
    digits = string.digits + string.ascii_lowercase
    n, out = int(time.time()), ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out[-5:]


def connect(stack):
    if stack == "operation":
        raise SystemExit("operation 스택에서는 E2E를 돌리지 않는다")
    env = load_env()
    cluster = Cluster(env, stack)
    url = env.get(f"URL_{stack.upper()}")
    if not url:
        raise SystemExit(f"e2e.env에 URL_{stack.upper()}가 없음")
    api = BeApi(url, cluster.secret_value("jwt_secret"), ca_file=env.get("CA_FILE") or None,
                insecure_tls=env.get("INSECURE_TLS") == "1")
    return cluster, api


def cmd_list(_args):
    data = catalog.load()
    for case in data["cases"]:
        kind = "fault" if catalog.is_fault_case(case) else ("real" if "steps" in case else "ref")
        print(f"{case['id']:4} {kind:5} {case.get('title', '')}")


def cmd_run(args):
    data = catalog.load()
    cases = [c for c in catalog.real_cases(data) if not args.case or c["id"] in args.case]
    unknown = set(args.case or []) - {c["id"] for c in cases}
    if unknown:
        raise SystemExit(f"실제로 돌릴 수 있는 사례가 아님: {sorted(unknown)}")
    cluster, api = connect(args.stack)
    run_id = new_run_id()
    faults = Faults(cluster, run_id)
    faults.clear_leftovers()
    run = Run(cluster, api, faults, stack_prefix=stack_prefix(args.stack), run_id=run_id, progress=print)
    print(f"run {run_id} on {args.stack}: {len(cases)} cases")
    results = []
    try:
        run.prepare()
        for case in cases:
            if not catalog.is_fault_case(case) or args.allow_faults:
                print(f"  {case['id']} 시작: {case.get('title', '')}")
            result = run.run_case(case, allow_faults=args.allow_faults)
            results.append(result)
            detail = f"  step {result.get('step')}: {result.get('error')}" if result["result"] == "FAIL" else ""
            print(f"  {result['id']} {result['result']} ({result.get('seconds', 0)}s){detail}")
    finally:
        residue = None
        if not args.keep:
            residue = Resetter(cluster, api, stack_prefix(args.stack), run_id).reset(run.admin_id)
            print("  reset: " + ("clean" if not residue else f"남은 것 {len(residue)}건: {residue}"))
        report = {"run": run_id, "stack": args.stack, "results": results, "residue": residue}
        out = pathlib.Path(os.path.expanduser("~/.ailab-exp/e2e-reports"))
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{run_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"  report: {out / (run_id + '.json')}")
    failed = [r for r in results if r["result"] == "FAIL"]
    return 1 if failed or residue else 0


def cmd_reset(args):
    cluster, api = connect(args.stack)
    rows = cluster.sql(f"SELECT user_id FROM users WHERE email='{admin_email(args.run)}';")
    run = Run(cluster, api, Faults(cluster, args.run), stack_prefix=stack_prefix(args.stack), run_id=args.run)
    admin_id = int(rows[0][0]) if rows else run._insert_user(email=admin_email(args.run), username=None, role="ADMIN")
    Faults(cluster, args.run).heal_all()
    residue = Resetter(cluster, api, stack_prefix(args.stack), args.run).reset(admin_id)
    print("clean" if not residue else f"남은 것 {len(residue)}건: {residue}")
    return 1 if residue else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m harness.e2e")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    run = sub.add_parser("run")
    run.add_argument("--stack", required=True)
    run.add_argument("--case", action="append")
    run.add_argument("--allow-faults", action="store_true")
    run.add_argument("--keep", action="store_true", help="끝나고 정리하지 않는다(조사용). 나중에 reset으로 지운다")
    run.set_defaults(fn=cmd_run)
    reset = sub.add_parser("reset")
    reset.add_argument("--stack", required=True)
    reset.add_argument("--run", required=True)
    reset.set_defaults(fn=cmd_reset)
    args = parser.parse_args(argv)
    # 파일이나 파이프로 보내도 줄마다 바로 보이게 한다(기본은 끝날 때까지 버퍼에 쌓인다).
    sys.stdout.reconfigure(line_buffering=True)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
