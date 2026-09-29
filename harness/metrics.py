"""trial 결과 JSON 과 명세의 analysis_view 로 논문팀 지표를 계산하는 유일한 자리.

근거는 docs/domains/scenario-alignment.md 의 "Metrics Analyzer 입력" 과 docs/domains/experiment-records.md
의 R2·R4·"보고 규칙" 이다. 지표의 분모와 분자는 여기서만 정한다. trial_runner 도 평가자도 비율을 내지 않는다.

묶음은 (scenario_id, method) 다. 묶음마다 전체 수, 결측, invalid injection, 환경 판정별 수, 회수 trial 의
시작 상태별 수를 먼저 내고, 지표는 invalid injection 을 뺀 유효 trial 에서 계산한다. 환경 판정과 시작
상태로 층을 나눈 지표도 함께 낸다.

UNKNOWN 을 PASS 나 FAIL 로 접지 않는다. 분자에 들지 않은 UNKNOWN 은 지표마다 unknown 칸에 따로 센다.
분모가 0 이면 비율은 None(해당 없음)이다. 0 으로 적지 않는다.

대상 시스템 저널은 읽지 않는다. 판정 칸은 전부 결과 파일의 independent_verdict 와 protection 과 snapshots
에서 온다 (ADR-004). 결과 파일은 읽기만 한다.
"""
import argparse
import json
import math
import pathlib
import statistics
import sys

import scenario_spec

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
DECLARATIONS = {"CREATE": "FULFILLED", "REVOKE": "DELETED"}
Z95 = 1.959963984540054


def wilson(k, n, z=Z95):
    """(하한, 상한). n 이 0 이면 None."""
    if n == 0:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z / (1 + z * z / n) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def _rate(k, n, unknown=0):
    return {"n": n, "k": k, "unknown": unknown, "rate": k / n if n else None, "wilson95": wilson(k, n)}


def _access(verdict):
    """판정 결과 하나의 access 값. v1 기록은 verdict 키만 있다. 판정이 없거나 모양이 다르면 UNKNOWN."""
    if not isinstance(verdict, dict):
        return UNKNOWN
    value = verdict.get("access_verdict", verdict.get("verdict"))
    return value if value in (PASS, FAIL, UNKNOWN) else UNKNOWN


def _injection(rec):
    return rec.get("injection") if rec.get("schema_version", 1) >= 2 else rec.get("fault")


def _invalid(rec):
    """주입 기록이 있는데 verified 가 참이 아니면 invalid injection 이다. v1 fault 에는 verified 가 없다."""
    inj = _injection(rec)
    return inj is not None and inj.get("verified") is not True


def _scenario_id(rec):
    return (rec.get("scenario") or {}).get("id") or rec.get("scenario_id")


def _declared(rec):
    return (rec.get("system_declaration") or {}).get("value") == DECLARATIONS.get(rec.get("operation"))


def _environment(rec):
    return (rec.get("environment") or {}).get("verdict", UNKNOWN)


def _start_state(rec):
    if rec.get("operation") != "REVOKE":
        return None
    state = rec.get("start_state")
    return state.get("creation_verdict_at_horizon", UNKNOWN) if isinstance(state, dict) else "UNPAIRED"


def _duplicate(rec):
    """PASS(중복 없음) / FAIL(그 사용자 Pod 가 둘 이상) / UNKNOWN(스냅숏이나 사용자를 모름)."""
    after = (rec.get("snapshots") or {}).get("after")
    # 기록 v2 에는 username 칸이 없다. 하네스의 환경 판정(resetter.environment_for)이 evidence 에 적은 것을 쓴다.
    user = rec.get("username") or ((rec.get("environment") or {}).get("evidence") or {}).get("username")
    if not isinstance(after, dict) or "by_user" not in after or not user or (after.get("errors") or {}).get("pod"):
        return UNKNOWN
    return FAIL if len(after["by_user"].get(user, {}).get("pod", [])) >= 2 else PASS


def _protection(rec):
    """None: 방관자 없음. 그 밖에는 H 끝 보호 판정."""
    after = (rec.get("protection") or {}).get("after")
    if after is None:
        return None
    value = after.get("protection_verdict") if isinstance(after, dict) else None
    return value if value in (PASS, FAIL, UNKNOWN) else UNKNOWN


def _quartiles(values):
    if not values:
        return None
    if len(values) == 1:
        return {"n": 1, "median": values[0], "q1": values[0], "q3": values[0]}
    q1, med, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return {"n": len(values), "median": med, "q1": q1, "q3": q3}


def metrics(records, recoverable):
    """유효 trial 목록의 지표. recoverable 은 명세의 analysis.recoverable (명세가 없으면 None)."""
    declared = [r for r in records if _declared(r)]
    at_decl = [_access(r["independent_verdict"].get("at_declaration")) for r in declared]
    at_h = {id(r): _access((r.get("independent_verdict") or {}).get("at_horizon")) for r in records}
    verified = [r for r in declared if at_h[id(r)] == PASS]
    out = {
        "incorrect_completion": _rate(at_decl.count(FAIL), len(declared), at_decl.count(UNKNOWN)),
        "unknown_at_declaration": _rate(at_decl.count(UNKNOWN), len(declared)),
        "verified_completion": _rate(len(verified), len(records),
                                     sum(1 for r in declared if at_h[id(r)] == UNKNOWN)),
    }

    if recoverable:
        safe = unknown = 0
        for r in records:
            if not _declared(r) or _access(r["independent_verdict"].get("at_declaration")) == FAIL:
                continue
            parts = [at_h[id(r)], _protection(r) or PASS, _duplicate(r)]
            if FAIL in parts:
                continue
            if UNKNOWN in parts:
                unknown += 1
            else:
                safe += 1
        out["safe_automatic_recovery"] = _rate(safe, len(records), unknown)
    else:
        out["safe_automatic_recovery"] = None

    revoked = [r for r in declared if r.get("operation") == "REVOKE"]
    residual = unknown = 0
    for r in revoked:
        # 회수 판정은 "막혔음" 이 PASS 다. FAIL 이 하나라도 있으면 접근이 남은 것이다.
        seen = [at_h[id(r)]] + [_access(s.get("verdict")) for s in r["independent_verdict"].get("samples") or []]
        if FAIL in seen:
            residual += 1
        elif UNKNOWN in seen:
            unknown += 1
    out["residual_access"] = _rate(residual, len(revoked), unknown)

    dup = [_duplicate(r) for r in records]
    out["duplicate_resource"] = _rate(dup.count(FAIL), len(records), dup.count(UNKNOWN))

    prot = [p for p in (_protection(r) for r in records) if p is not None]
    out["protection_violation"] = _rate(prot.count(FAIL), len(prot), prot.count(UNKNOWN))

    ts = [r["timestamps"] for r in verified if r["timestamps"].get("verified") is not None]
    out["time_to_verified_completion"] = _quartiles(
        sorted(float(t["verified"]) - float(t["submitted"]) for t in ts))
    return out


def _counts(values):
    counts = {}
    for v in values:
        counts[str(v)] = counts.get(str(v), 0) + 1
    return counts


def analyze(records, specs, missing=()):
    """records 는 결과 기록 목록, specs 는 {scenario_id: analysis_view}, missing 은 결과 파일이 없는
    (scenario_id, method) 목록이다. 묶음 키 "scenario_id|method" 로 결과를 돌려준다."""
    groups = {}
    for rec in records:
        groups.setdefault((_scenario_id(rec), rec.get("method")), []).append(rec)
    for key in missing:
        groups.setdefault(tuple(key), [])
    result = {}
    for (sid, method), recs in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
        view = specs.get(sid)
        recoverable = view["analysis"]["recoverable"] if view else None
        valid = [r for r in recs if not _invalid(r)]
        n_missing = sum(1 for m in missing if tuple(m) == (sid, method))
        by_env = {}
        by_start = {}
        for r in valid:
            by_env.setdefault(_environment(r), []).append(r)
            if r.get("operation") == "REVOKE":
                by_start.setdefault(_start_state(r), []).append(r)
        result[f"{sid}|{method}"] = {
            "scenario_id": sid, "method": method, "spec_hash": view["spec_hash"] if view else None,
            "total": len(recs) + n_missing, "missing": n_missing,
            "invalid_injection": len(recs) - len(valid),
            "environment": _counts(_environment(r) for r in recs),
            "start_state": _counts(_start_state(r) for r in recs if r.get("operation") == "REVOKE"),
            "metrics": metrics(valid, recoverable),
            "by_environment": {str(k): metrics(v, recoverable) for k, v in by_env.items()},
            "by_start_state": {str(k): metrics(v, recoverable) for k, v in by_start.items()},
        }
    return result


def load_dir(directory):
    """결과 디렉터리의 기록을 읽는다. 읽히지 않는 파일은 결측으로 접지 않고 예외를 올린다."""
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(pathlib.Path(directory).glob("*.json"))]


def _fmt(m):
    if m is None:
        return "-"
    if "median" in m:
        return f"n={m['n']} median={m['median']:.1f} q1={m['q1']:.1f} q3={m['q3']:.1f}"
    if m["rate"] is None:
        return f"{m['k']}/{m['n']} unknown={m['unknown']} rate=N/A"
    lo, hi = m["wilson95"]
    return f"{m['k']}/{m['n']} unknown={m['unknown']} rate={m['rate']:.3f} [{lo:.3f},{hi:.3f}]"


def main(argv=None):
    ap = argparse.ArgumentParser(description="trial 결과 JSON 디렉터리에서 논문 지표를 계산한다.")
    ap.add_argument("results", help="trial 결과 JSON 디렉터리")
    ap.add_argument("--specs", required=True, help="시나리오 명세 디렉터리 (harness/scenarios)")
    ap.add_argument("--json", dest="out", help="분석 결과를 JSON 으로 쓸 경로")
    args = ap.parse_args(argv)
    specs = {sid: s.analysis_view() for sid, s in scenario_spec.load_all(args.specs).items()}
    result = analyze(load_dir(args.results), specs)
    for key, g in result.items():
        print(f"{key} total={g['total']} missing={g['missing']} invalid_injection={g['invalid_injection']}"
              f" environment={g['environment']} start_state={g['start_state']}")
        for name, m in g["metrics"].items():
            print(f"{key} {name} {_fmt(m)}")
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True),
                                          encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
