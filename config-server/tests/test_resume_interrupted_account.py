"""계정 단계 도중 제어기가 죽은 작업의 이어하기 (#210).

계정 단계는 계정 파일에 쓰기 직전에 그 작업의 진행 기록(job_control saved_ctx)에 "쓰기 시작 표시"
(account_write_started = 정한 uid)를 남긴다. 이어받은 제어기는 완료 기록이 없는 계정 단계를
다시 실행하기 전에 그 표시와 계정 파일을 대조한다.

  표시 없음                               → 쓰기 전에 죽었다. 그대로 실행(같은 이름이 있으면 지금처럼 실패)
  표시 + 이름·uid 일치 + 계정 파일 모두 있음 → 이 작업이 끝까지 쓴 계정. 완료로 보고 다음 단계로
  표시 + 이름·uid 일치 + 일부 누락           → 이 작업이 쓰다 만 계정. 치우고 다시 만든다
  표시 + 이름은 있는데 uid 다름              → 예상 밖. 건드리지 않고 DEGRADED
  표시 + 계정 없음                         → 쓰기 전에 죽었다. 그대로 실행
"""
import base64
import json

import main
from adapters import job_control
from test_e2e_virtual import env, PW, tick, rows, result, passwd_names, passwd_line, shadow_line  # noqa: F401


def _write_account(name, rid, pg_name=None, supp_groups=()):
    """죽은 제어기가 계정 단계를 끝까지 실행한 상태를 만든다. 정해진 uid를 돌려준다."""
    ctx = {"request_id": rid, "name": name, "pg_name": pg_name or name, "supp_groups": list(supp_groups),
           "gecos": "", "plaintext_pw": base64.b64decode(PW).decode()}
    with main.app.app_context():
        main.step_create_account(ctx)
    return ctx["uid"]


def _register_interrupted(e, lease_env, rid, name, saved_ctx, **account):
    """작업을 등록하고, 이전 제어기가 실행하다 죽은 상태(입력 running·만료 lease)로 만든다."""
    jid = e.api.post("/operations/provision", json={"request_id": rid, "username": name,
                                                    "account": {"passwd_base64": PW, **account}}).get_json()["job_id"]
    e.redis[("PROVISION", rid)]["state"] = "running"
    lease_env[jid] = {"owner": "dead-controller", "alive": False, "done": [], "ctx": dict(saved_ctx)}
    return jid


def _end_row(e, rid):
    return e.db.execute("SELECT phase, error_code, error_detail FROM operation_log WHERE request_id=?"
                        " AND action='PROVISION' AND phase!='START' ORDER BY id DESC", (rid,)).fetchone()


def _account_rows(e, rid):
    return [(p, c) for p, c in e.db.execute(
        "SELECT phase, error_code FROM operation_log WHERE request_id=? AND action='CREATE_ACCOUNT' ORDER BY id",
        (rid,))]


# ---------- 계정 단계가 쓰기 직전에 표시를 남긴다 ----------

def test_account_step_checkpoints_before_writing_account_files(env):
    """체크포인트는 uid를 정한 뒤, 계정 파일에 아무것도 쓰기 전에 불린다."""
    seen = []

    def checkpoint():
        seen.append((ctx.get("account_write_started"), "exp-np-cp1" in passwd_names()))

    ctx = {"request_id": "400", "name": "exp-np-cp1", "pg_name": "exp-np-cp1", "supp_groups": [], "gecos": "",
           "plaintext_pw": "pw", "_checkpoint": checkpoint}
    with main.app.app_context():
        main.step_create_account(ctx)
    assert seen == [(ctx["uid"], False)]


def test_job_records_write_marker_before_account_step_completes(env, lease_env, monkeypatch):
    """제어기 경로에서 표시가 진행 기록에 실제로 저장된다 — 계정 단계 완료 기록보다 먼저."""
    e = env
    records = []
    real = job_control.record_step

    def spy(job_id, done_steps, saved_ctx, owner=None, timeouts=None):
        records.append((list(done_steps), dict(saved_ctx), timeouts))
        return real(job_id, done_steps, saved_ctx, owner)
    monkeypatch.setattr(job_control, "record_step", spy)

    e.api.post("/operations/provision", json={"request_id": "401", "username": "exp-np-cp2",
                                              "account": {"passwd_base64": PW}})
    tick(e)

    assert result(e, "provision", "401")["phase"] == "SUCCESS"
    uid = int(passwd_line("exp-np-cp2").split(":")[2])
    first = records[0]
    assert first == ([], {"account_write_started": uid}, job_control.CHECKPOINT_DB_TIMEOUTS)
    assert all(t is None for _d, _s, t in records[1:])        # 단계 사이 기록에는 시간 제한이 없다


# ---------- 이어하기 판정 ----------

def test_resume_after_account_fully_written_continues(env, lease_env):
    """#210 재현: 계정을 다 쓰고 완료 기록 전에 죽음 → 이어받아 SUCCESS, 계정 그대로."""
    e = env
    uid = _write_account("exp-np-ra1", "410")
    _register_interrupted(e, lease_env, "410", "exp-np-ra1", {"account_write_started": uid})

    tick(e)

    assert result(e, "provision", "410")["phase"] == "SUCCESS", rows(e, "410")
    assert passwd_names().count("exp-np-ra1") == 1
    assert int(passwd_line("exp-np-ra1").split(":")[2]) == uid
    assert ("FAIL", "USER_ALREADY_EXISTS") not in _account_rows(e, "410")
    assert e.api.get("/operations/provision/410").get_json()["result"]["uid"] == uid


def test_resume_after_partial_account_rewrites_it(env, lease_env):
    """passwd만 쓰고 죽음(shadow 없음) → 쓰다 만 계정을 치우고 다시 만들어 SUCCESS."""
    e = env
    uid = _write_account("exp-np-ra2", "411")
    with main.app.app_context():
        main.write_shadow_lines([l for l in main.read_shadow_lines() if not l.startswith("exp-np-ra2:")])
    _register_interrupted(e, lease_env, "411", "exp-np-ra2", {"account_write_started": uid})

    tick(e)

    assert result(e, "provision", "411")["phase"] == "SUCCESS", rows(e, "411")
    assert passwd_names().count("exp-np-ra2") == 1
    assert shadow_line("exp-np-ra2").split(":")[1].startswith("$6$")


def test_resume_when_marker_written_but_account_absent_runs_step(env, lease_env):
    """표시만 남기고 쓰기 전에 죽음 → 계정 단계를 그대로 실행해 SUCCESS."""
    e = env
    _register_interrupted(e, lease_env, "412", "exp-np-ra3", {"account_write_started": 50999})

    tick(e)

    assert result(e, "provision", "412")["phase"] == "SUCCESS", rows(e, "412")
    assert passwd_names().count("exp-np-ra3") == 1


def test_resume_without_marker_keeps_existing_account_conflict(env, lease_env):
    """표시가 없으면 이 작업은 계정 파일에 쓰지 않았다. 같은 이름 계정(예전 신청이 만든 것)이 있으면
    지금처럼 USER_ALREADY_EXISTS로 멈추고 그 계정은 건드리지 않는다."""
    e = env
    uid = _write_account("exp-np-ra4", "413-old")
    _register_interrupted(e, lease_env, "413", "exp-np-ra4", {})

    tick(e)

    end = _end_row(e, "413")
    assert end[0] == "FAIL" and "already exists" in end[1], rows(e, "413")
    assert passwd_names().count("exp-np-ra4") == 1
    assert int(passwd_line("exp-np-ra4").split(":")[2]) == uid


def test_resume_with_mismatched_uid_treats_account_as_another_requests(env, lease_env):
    """표시의 uid와 계정 파일의 uid가 다르면 이 작업은 쓰지 못했고, 같은 사람의 다른 신청이 그 사이 만든 계정이다
    (admin_be는 같은 사람의 신청을 동시에 승인할 수 있다). DEGRADED로 멈추지 않고 표시 없음과 같이
    USER_ALREADY_EXISTS로 실패한다 — admin_be가 신청을 PENDING으로 되돌리고 재승인 때 계정을 재사용한다.
    그 계정은 건드리지 않고, 보상도 돌지 않는다."""
    e = env
    uid = _write_account("exp-np-ra5", "414-other")                  # 다른 신청이 만든 계정
    _register_interrupted(e, lease_env, "414", "exp-np-ra5", {"account_write_started": uid - 1})

    tick(e)

    phase, code, detail = _end_row(e, "414")
    assert phase == "FAIL" and "already exists" in code, rows(e, "414")
    assert json.loads(detail)["compensation"] is None
    assert int(passwd_line("exp-np-ra5").split(":")[2]) == uid
    assert shadow_line("exp-np-ra5")                                 # 치우지 않았다


def test_baseline_resume_is_unchanged(env, lease_env, monkeypatch):
    """baseline은 운영의 "죽으면 끝"을 재현한다 — 표시가 있어도 판정하지 않고 지금처럼 실패한다."""
    e = env
    monkeypatch.setattr(main, "VERIFY_MODE", "baseline")
    uid = _write_account("exp-np-ra6", "415")
    _register_interrupted(e, lease_env, "415", "exp-np-ra6", {"account_write_started": uid})

    tick(e)

    end = _end_row(e, "415")
    assert end[0] == "FAIL" and "already exists" in end[1], rows(e, "415")


def test_baseline_does_not_record_write_marker(env, lease_env, monkeypatch):
    """baseline은 표시를 남기지 않는다(운영 경로에 없는 쓰기를 더하지 않는다)."""
    e = env
    monkeypatch.setattr(main, "VERIFY_MODE", "baseline")
    records = []
    real = job_control.record_step
    monkeypatch.setattr(job_control, "record_step",
                        lambda j, d, s, owner=None, timeouts=None: (records.append(dict(s)), real(j, d, s, owner))[1])
    e.api.post("/operations/provision", json={"request_id": "416", "username": "exp-np-ra7",
                                              "account": {"passwd_base64": PW}})
    tick(e)
    assert result(e, "provision", "416")["phase"] == "SUCCESS"
    assert all("account_write_started" not in s for s in records)


def _groups():
    with main.app.app_context():
        return [g for line in main.read_group_lines() if (g := main.parse_group_line(line))]


def test_resume_partial_account_with_own_primary_group_name(env, lease_env):
    """개인 그룹 이름을 따로 받은 계정(primary_group_name)도 쓰다 만 상태에서 치우고 다시 만든다.
    개인 그룹 줄이 남으면 다시 만들 때 primary group conflict로 막힌다."""
    e = env
    uid = _write_account("exp-np-rb1", "420", pg_name="exp-np-pg1")
    with main.app.app_context():
        main.write_shadow_lines([l for l in main.read_shadow_lines() if not l.startswith("exp-np-rb1:")])
    _register_interrupted(e, lease_env, "420", "exp-np-rb1", {"account_write_started": uid},
                          primary_group_name="exp-np-pg1")

    tick(e)

    assert result(e, "provision", "420")["phase"] == "SUCCESS", rows(e, "420")
    new_gid = int(passwd_line("exp-np-rb1").split(":")[3])
    assert [g["gid"] for g in _groups() if g["name"] == "exp-np-pg1"] == [new_gid]


def test_resume_partial_account_missing_supplementary_membership(env, lease_env, monkeypatch):
    """보조 그룹 멤버 등록이 빠진 계정도 쓰다 만 것으로 본다."""
    e = env
    monkeypatch.setattr(main, "_ad_enabled", lambda: False)   # AD 그룹 반영은 이 시험의 관심사가 아니다
    team = {"name": "exp-team-rs", "gid": main.SHARED_GID_MIN + 5}
    uid = _write_account("exp-np-rb2", "421", supp_groups=[team])
    with main.app.app_context():
        lines = [main.format_group_entry(dict(g, members=[])) if g["gid"] == team["gid"] else main.format_group_entry(g)
                 for g in _groups()]
        main.write_group_lines(lines)
    _register_interrupted(e, lease_env, "421", "exp-np-rb2", {"account_write_started": uid},
                          supplementary_groups=[team])

    tick(e)

    assert result(e, "provision", "421")["phase"] == "SUCCESS", rows(e, "421")
    assert "exp-np-rb2" in next(g for g in _groups() if g["gid"] == team["gid"])["members"]


def test_checkpoint_failure_is_retried_under_its_own_code(env, lease_env, monkeypatch):
    """표시 기록이 실패하면 아무것도 쓰지 않은 채 CHECKPOINT_FAILED로 남기고 재시도한다."""
    e = env
    real = job_control.record_step
    state = {"n": 0}

    def flaky(job_id, done_steps, saved_ctx, owner=None, timeouts=None):
        if "account_write_started" in saved_ctx and not done_steps and state["n"] == 0:
            state["n"] += 1
            raise RuntimeError("log db timeout")
        return real(job_id, done_steps, saved_ctx, owner)
    monkeypatch.setattr(job_control, "record_step", flaky)

    e.api.post("/operations/provision", json={"request_id": "422", "username": "exp-np-rb3",
                                              "account": {"passwd_base64": PW}})
    tick(e)

    assert ("FAIL", "CHECKPOINT_FAILED") in _account_rows(e, "422")
    assert result(e, "provision", "422")["phase"] == "SUCCESS", rows(e, "422")
    assert passwd_names().count("exp-np-rb3") == 1


def test_checkpoint_lease_lost_stops_before_writing(env, lease_env, monkeypatch):
    """표시를 남기려는데 소유권을 잃었으면 계정 파일에 쓰지 않고 물러난다(새 소유자가 이어간다)."""
    e = env

    def lost(job_id, done_steps, saved_ctx, owner=None, timeouts=None):
        raise job_control.LeaseLost(str(job_id))
    monkeypatch.setattr(job_control, "record_step", lost)

    e.api.post("/operations/provision", json={"request_id": "423", "username": "exp-np-rb4",
                                              "account": {"passwd_base64": PW}})
    tick(e)

    assert "exp-np-rb4" not in passwd_names()
    assert _end_row(e, "423") is None                                      # 끝 행을 남기지 않았다
    assert ("FAIL", "PASSWD_WRITE_FAILED") not in _account_rows(e, "423")
