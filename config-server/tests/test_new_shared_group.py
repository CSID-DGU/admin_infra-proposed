"""새 공유 그룹 — admin_be 가 gid 없이 보낸 보조 그룹을 생성 작업이 만들고, 정한 gid 를 결과로 돌려준다.

admin_be 는 "새로 만들기" 때 그룹을 자기 DB 에만 만들고(gid 없음), 그 그룹을 고른 신청이 승인될 때 이 작업으로
인프라 그룹(원장·AD·팀 디렉터리)을 만든다. 작업이 끝나기 전에는 admin_be 도 gid 를 모르므로 설정 조회(accept-info)의
그 그룹 gid 는 null 이다.
"""
import pytest

import main
from lifecycle_steps import group as group_steps
from test_e2e_virtual import env, tick, rows, result, passwd_line, PW  # noqa: F401  (env는 pytest fixture)

GPU_NODES = [{"node_name": "farm2", "num_gpu": 1, "gpu_models": ["RTX A5000"], "cpu_limit": "4", "memory_limit": "16Gi"}]


@pytest.fixture(autouse=True, params=["noprobe", "baseline"])
def mode(request, monkeypatch):
    """운영(ailab-operation)은 baseline 으로 돈다(ops/proposed-stack/stack-up.sh) — 두 방식 모두에서 본다."""
    monkeypatch.setattr(main, "VERIFY_MODE", request.param)
    return request.param


@pytest.fixture
def ad(env, monkeypatch):
    """AD·팀 디렉터리·NAS 캐시 호출을 가로챈다. 반환: AD 로 나간 원격 명령, 만든 팀 디렉터리."""
    sent, dirs = [], []
    monkeypatch.setattr(main, "_farm_ad_ssh", lambda cmd, stdin_data="": sent.append(cmd) or "")
    monkeypatch.setattr(main, "create_team_directory", lambda name, gid: dirs.append((name, gid)))
    import reconcile_krb5
    monkeypatch.setattr(reconcile_krb5, "trigger_nas_gss_flush_ondemand", lambda: True)
    monkeypatch.setattr(main, "SHARED_GID_MIN", 70000)
    monkeypatch.setattr(main, "SHARED_GID_MAX", 79999)
    return sent, dirs


def _was_with_groups(env, groups):
    """admin_be 설정 조회 응답. 새 그룹은 gid 가 null 로 온다."""
    env.was = lambda url: env.Resp(200, {"image": "dguailab/decs:1", "passwd_base64": PW,
                                         "groups": groups, "gpu_nodes": GPU_NODES})


def _ledger():
    with main.app.app_context():
        return {r["name"]: r for l in main.read_group_lines() if (r := main.parse_group_line(l))}


def _seed_group(line):
    with main.app.app_context():
        main.ensure_etc_layout()
        main.write_group_lines(main.read_group_lines() + [line])


def _pod_groups_env(env):
    body = env.v1.pods[next(iter(env.v1.pods))].body
    by_name = {v["name"]: v.get("value") for v in body["spec"]["containers"][0]["env"]}
    return by_name["DECS_SUPPLEMENTAL_GROUPS"].split(",")[1:]


def _provision_new_account(env, rid, username, groups):
    return env.api.post("/operations/provision", json={
        "request_id": rid, "username": username,
        "account": {"passwd_base64": PW, "supplementary_groups": groups}})


# ---------- 등록 ----------

def test_registration_accepts_groups_without_gid(env, ad):
    assert _provision_new_account(env, "801", "exp-np-ng", [{"name": "vision-lab"}]).status_code == 202
    assert env.api.post("/operations/provision", json={
        "request_id": "802", "username": "exp-np-ng2", "supplementary_groups": [{"name": "vision-lab"}]}).status_code == 202


def test_jobs_without_new_groups_keep_their_steps(env, ad):
    """예전 admin_be 처럼 gid 를 다 보내면 새 단계가 끼지 않는다 — 단계 목록이 지금과 같다."""
    job = {"username": "u", "account": {"supp_groups": [{"name": "ailab", "gid": 70001}]}}
    assert main.step_resolve_new_groups not in main._job_steps("provision", job)
    job = {"username": "u", "account": {"supp_groups": [{"name": "vision-lab", "gid": None}]}}
    assert main._job_steps("provision", job)[0] is main.step_resolve_new_groups
    job = {"username": "u", "supp_groups_only": [{"name": "vision-lab", "gid": None}]}
    assert main._job_steps("provision", job)[0] is main.step_resolve_new_groups


# ---------- 새 계정 ----------

def test_new_account_creates_the_new_group_and_returns_its_gid(env, ad):
    sent, dirs = ad
    _was_with_groups(env, [{"gid": None, "name": "vision-lab"}])
    _provision_new_account(env, "810", "exp-np-ng", [{"name": "vision-lab"}])

    tick(env)

    res = result(env, "provision", "810")
    assert res["phase"] == "SUCCESS", rows(env, "810")
    gid = _ledger()["vision-lab"]["gid"]
    assert gid == 70000                                         # 공용 대역 첫 번호
    assert _ledger()["vision-lab"]["members"] == ["exp-np-ng"]
    assert res["result"]["groups"] == [{"name": "vision-lab", "gid": 70000}]
    assert "group-create vision-lab 70000" in sent              # AD 에도 같은 gid 로
    assert ("vision-lab", 70000) in dirs                        # 팀 디렉터리
    assert _pod_groups_env(env) == ["vision-lab:70000"]         # 컨테이너 안 그룹도 같은 gid


def test_new_and_existing_groups_together(env, ad):
    _seed_group("ailab:x:70001:")
    _was_with_groups(env, [{"gid": 70001, "name": "ailab"}, {"gid": None, "name": "vision-lab"}])
    _provision_new_account(env, "811", "exp-np-ng", [{"name": "ailab", "gid": 70001}, {"name": "vision-lab"}])

    tick(env)

    res = result(env, "provision", "811")
    assert res["phase"] == "SUCCESS", rows(env, "811")
    # 70001 은 이미 쓰이므로 새 그룹은 다른 번호를 받는다
    new_gid = _ledger()["vision-lab"]["gid"]
    assert new_gid not in (70001,) and 70000 <= new_gid <= 79999
    assert sorted(res["result"]["groups"], key=lambda g: g["name"]) == [
        {"name": "ailab", "gid": 70001}, {"name": "vision-lab", "gid": new_gid}]
    assert sorted(_pod_groups_env(env)) == sorted(["ailab:70001", f"vision-lab:{new_gid}"])


def test_issued_gid_is_recorded_so_it_is_never_given_again(env, ad):
    """발급한 번호는 그룹 줄이 지워져도 다시 주지 않는다(proposed#177 과 같은 규칙)."""
    _was_with_groups(env, [{"gid": None, "name": "vision-lab"}])
    _provision_new_account(env, "812", "exp-np-ng", [{"name": "vision-lab"}])
    tick(env)
    assert _ledger()["vision-lab"]["gid"] == 70000
    with main.app.app_context():
        main.write_group_lines([l for l in main.read_group_lines() if not l.startswith("vision-lab:")])
        assert main.read_issued_id_max("shared_gid") >= 70000

    _was_with_groups(env, [{"gid": None, "name": "nlp-lab"}])
    _provision_new_account(env, "813", "exp-np-ng3", [{"name": "nlp-lab"}])
    tick(env)
    assert result(env, "provision", "813")["phase"] == "SUCCESS", rows(env, "813")
    assert _ledger()["nlp-lab"]["gid"] == 70001


# ---------- 기존 계정 ----------

def test_existing_account_joins_a_new_group(env, ad):
    env.api.post("/operations/provision", json={"request_id": "820", "username": "exp-np-old",
                                                "account": {"passwd_base64": PW}})
    tick(env)
    assert result(env, "provision", "820")["phase"] == "SUCCESS"

    _was_with_groups(env, [{"gid": None, "name": "vision-lab"}])
    env.api.post("/operations/provision", json={"request_id": "821", "username": "exp-np-old",
                                                "supplementary_groups": [{"name": "vision-lab"}]})
    tick(env)

    res = result(env, "provision", "821")
    assert res["phase"] == "SUCCESS", rows(env, "821")
    assert _ledger()["vision-lab"]["members"] == ["exp-np-old"]
    assert res["result"]["groups"] == [{"name": "vision-lab", "gid": _ledger()["vision-lab"]["gid"]}]


# ---------- 같은 이름의 줄이 이미 있을 때 ----------

def test_second_teammate_reuses_the_group_the_first_job_made(env, ad):
    """같은 그룹을 고른 두 신청이 차례로 승인된 경우. admin_be 가 첫 결과로 gid 를 채우기 전에 두 번째가 오면
    이름만 오지만, 원장의 줄을 이어받아 같은 gid 를 쓴다."""
    _was_with_groups(env, [{"gid": None, "name": "vision-lab"}])
    _provision_new_account(env, "830", "exp-np-a", [{"name": "vision-lab"}])
    tick(env)
    _provision_new_account(env, "831", "exp-np-b", [{"name": "vision-lab"}])
    tick(env)

    assert result(env, "provision", "831")["phase"] == "SUCCESS", rows(env, "831")
    assert result(env, "provision", "831")["result"]["groups"] == [{"name": "vision-lab", "gid": 70000}]
    assert _ledger()["vision-lab"]["members"] == ["exp-np-a", "exp-np-b"]


def test_leftover_line_with_another_member_is_reused(env, ad):
    """기존 계정의 실패한 작업은 멤버를 되돌리지 않는다 — 다른 팀원이 남긴 줄도 이어받아야 한다."""
    _seed_group("vision-lab:x:70005:exp-np-other")
    _was_with_groups(env, [{"gid": None, "name": "vision-lab"}])
    _provision_new_account(env, "832", "exp-np-ng", [{"name": "vision-lab"}])

    tick(env)

    assert result(env, "provision", "832")["phase"] == "SUCCESS", rows(env, "832")
    assert _ledger()["vision-lab"]["gid"] == 70005
    assert sorted(_ledger()["vision-lab"]["members"]) == ["exp-np-ng", "exp-np-other"]


def test_same_name_outside_the_shared_band_is_not_reused(env, ad):
    """공용 대역 밖의 줄(개인·시스템 그룹)은 팀 그룹이 아니다 — 이어받지 않고 재시도 없이 실패한다."""
    _seed_group("vision-lab:x:30000:")
    _was_with_groups(env, [{"gid": None, "name": "vision-lab"}])
    _provision_new_account(env, "833", "exp-np-ng", [{"name": "vision-lab"}])

    tick(env)

    res = result(env, "provision", "833")
    assert res["phase"] == "FAIL" and res["error_code"] == "GROUP_NAME_EXISTS", rows(env, "833")
    assert env.v1.pods == {}
    assert "exp-np-ng" not in _ledger()["vision-lab"]["members"]


@pytest.mark.parametrize("name,code", [("video", "GROUP_NAME_RESERVED"), ("exp-np-taken", "GROUP_NAME_CONFLICTS_USER")])
def test_invalid_new_group_names_fail_without_retry(env, ad, name, code):
    env.api.post("/operations/provision", json={"request_id": "834", "username": "exp-np-taken",
                                                "account": {"passwd_base64": PW}})
    tick(env)
    _was_with_groups(env, [{"gid": None, "name": name}])
    _provision_new_account(env, "835", "exp-np-ng", [{"name": name}])

    tick(env)

    res = result(env, "provision", "835")
    assert res["phase"] == "FAIL" and res["error_code"] == code, rows(env, "835")
    assert not any(p == "RETRY" for a, p in rows(env, "835"))
    assert env.v1.pods and all("exp-np-ng" not in n for n in env.v1.pods)


# ---------- 이어하기 ----------

def test_resumed_job_finds_the_same_gid_again(env, ad, lease_env, mode):
    """정한 gid 는 이어하기 컨텍스트에 남지 않는다. 계정 단계까지 끝내고 죽은 작업을 이어받아도 원장의 줄에서
    같은 gid 를 다시 찾아 나머지 단계(AD·컨테이너·결과)를 끝낸다."""
    if mode == "baseline":
        pytest.skip("baseline 은 중단된 작업을 이어하지 않는다(_interrupt_baseline_job)")
    ctx = {"request_id": "840", "name": "exp-np-ng", "pg_name": "exp-np-ng", "gecos": "",
           "supp_groups": [{"name": "vision-lab", "gid": None}], "plaintext_pw": "pw-e2e"}
    with main.app.app_context():
        group_steps.step_resolve_new_groups(ctx)
        for step in main.ACCOUNT_CREATE_STEPS:
            step(ctx)
    gid = _ledger()["vision-lab"]["gid"]
    _was_with_groups(env, [{"gid": None, "name": "vision-lab"}])
    r = _provision_new_account(env, "840", "exp-np-ng", [{"name": "vision-lab"}])
    jid = r.get_json()["job_id"]
    env.redis[("PROVISION", "840")]["state"] = "running"
    lease_env[jid] = {"owner": "dead-controller", "alive": False,
                      # 끝난 단계로 기록돼 있어도 gid 를 정하는 단계는 다시 돈다 — 건너뛰면 뒤 단계가 gid 를 모른다.
                      "done": ["step_resolve_new_groups"] + [s.__name__ for s in main.ACCOUNT_CREATE_STEPS],
                      "ctx": {"uid": ctx["uid"], "gid": ctx["gid"]}}

    tick(env)

    res = result(env, "provision", "840")
    assert res["phase"] == "SUCCESS", rows(env, "840")
    assert res["result"]["groups"] == [{"name": "vision-lab", "gid": gid}]
    assert _pod_groups_env(env) == [f"vision-lab:{gid}"]
    with main.app.app_context():
        lines = [l for l in main.read_group_lines() if l.startswith("vision-lab:")]
    assert len(lines) == 1                                                  # 줄이 하나뿐(두 번 만들지 않음)


def test_failed_job_then_reapproval_reuses_the_same_gid(env, ad):
    """작업이 실패하면 admin_be 는 신청을 승인 대기로 되돌리고 그룹의 gid 를 비워 둔다. 다시 승인되면 이름만 다시
    오는데, 앞선 시도가 써 둔 줄을 이어받아 같은 gid 로 끝낸다 — 번호를 두 번 쓰지 않는다."""
    env.was = lambda url: env.Resp(404, {"status": 404})          # 계정까지 만든 뒤 설정 조회에서 실패
    _provision_new_account(env, "860", "exp-np-ng", [{"name": "vision-lab"}])
    tick(env)
    assert result(env, "provision", "860")["phase"] == "FAIL"
    first_gid = _ledger()["vision-lab"]["gid"]

    # 계정이 남아 있으니 account 없이 다시 등록한다(admin_be 의 재승인과 같은 모양)
    _was_with_groups(env, [{"gid": None, "name": "vision-lab"}])
    assert env.api.post("/operations/provision", json={
        "request_id": "860", "username": "exp-np-ng", "supplementary_groups": [{"name": "vision-lab"}]}).status_code == 202
    tick(env)

    res = result(env, "provision", "860")
    assert res["phase"] == "SUCCESS", rows(env, "860")
    assert res["result"]["groups"] == [{"name": "vision-lab", "gid": first_gid}]
    with main.app.app_context():
        assert len([l for l in main.read_group_lines() if l.startswith("vision-lab:")]) == 1


def test_resolve_step_is_idempotent(env, ad):
    with main.app.app_context():
        first = {"username": "exp-np-ng", "supp_groups": [{"name": "vision-lab"}]}
        group_steps.step_resolve_new_groups(first)
        main.write_group_lines([l if not l.startswith("vision-lab:") else l + "exp-np-ng"
                                for l in main.read_group_lines()])    # 뒤 단계가 멤버를 넣은 상태
        again = {"username": "exp-np-ng", "supp_groups": [{"name": "vision-lab"}]}
        group_steps.step_resolve_new_groups(again)
    assert first["supp_groups"] == again["supp_groups"] == [{"name": "vision-lab", "gid": 70000}]


# ---------- 설정 조회의 null gid ----------

def test_null_gid_this_job_did_not_create_fails_instead_of_dropping_the_group(env, ad):
    """설정 조회에 gid 없는 그룹이 있는데 이 작업이 그 그룹을 정하지 않았다면, 그 그룹을 빼고 컨테이너를 만들지
    않고 실패한다 — 빼면 그룹 없는 컨테이너가 성공으로 끝난다."""
    _was_with_groups(env, [{"gid": None, "name": "someone-else"}])
    _provision_new_account(env, "850", "exp-np-ng", [])

    tick(env)

    res = result(env, "provision", "850")
    assert res["phase"] == "FAIL" and res["error_code"] == "GROUP_GID_UNRESOLVED", rows(env, "850")
    assert env.v1.pods == {}
