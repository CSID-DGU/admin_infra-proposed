"""홈 소유자 불일치 방어: 이미 있는 홈의 소유자가 배정하려는 uid 와 다르면 덮어쓰지 않고 멈춘다.

NAS 가 Kerberos 주체를 푸는 값과 계정 대장의 uid 가 어긋날 수 있다. 구 운영에서 넘어온 계정이
그런 경우이고, 그대로 chown 하면 사용자가 자기 홈을 잃는다(2026-09-19 yoon6yo 사례).
"""
import paramiko
import pytest

import main
import utils


class _Chan:
    def __init__(self, code):
        self._code = code

    def recv_exit_status(self):
        return self._code


class _Out:
    def __init__(self, code, data=b""):
        self.channel = _Chan(code)
        self._data = data

    def read(self):
        return self._data


class _FakeSSH:
    """stat 은 미리 정한 값을 돌려주고, 나머지 명령은 실행 기록만 남긴다."""

    def __init__(self, stat_code, stat_out):
        self.stat_code, self.stat_out = stat_code, stat_out
        self.ran = []

    def exec_command(self, cmd):
        if cmd.startswith("stat "):
            return None, _Out(self.stat_code, self.stat_out), _Out(0)
        self.ran.append(cmd)
        return None, _Out(0), _Out(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def nas(monkeypatch, tmp_path):
    monkeypatch.setenv("NFS_USER_SHARE_PATH", "/volume1/share/user")
    monkeypatch.setenv("NAS_SSH_HOST", "nas")
    monkeypatch.setenv("NAS_SSH_USER", "admin")
    monkeypatch.setenv("NAS_SSH_KEY_PATH", str(tmp_path / "k"))

    def install(stat_code, stat_out):
        fake = _FakeSSH(stat_code, stat_out)
        monkeypatch.setattr(utils, "_nas_ssh_client", lambda: fake)
        return fake

    return install


def test_stops_when_existing_home_has_other_owner(nas):
    """대장은 21000 을 주는데 NAS 홈이 20016 소유면 chown 하지 않고 멈춘다."""
    fake = nas(0, b"20016\n")
    with main.app.app_context():
        with pytest.raises(utils.HomeOwnerMismatch) as err:
            utils.create_user_home_directory("yoon6yo", 21000, 21000)
    assert "20016" in str(err.value) and "21000" in str(err.value)
    assert fake.ran == [], "멈춰야 할 상황에서 mkdir·chown·chmod 가 실행되면 안 된다"


def test_creates_when_home_is_absent(nas):
    """홈이 없으면(stat 실패) 평소대로 만든다."""
    fake = nas(1, b"")
    with main.app.app_context():
        utils.create_user_home_directory("newuser", 21005, 21005)
    assert [c.split()[1] for c in fake.ran] == ["mkdir", "chown", "chmod"]
    assert "21005:21005" in fake.ran[1]


def test_creates_when_owner_already_matches(nas):
    """같은 uid 로 다시 프로비저닝하는 경우는 그대로 진행한다(재실행 안전)."""
    fake = nas(0, b"21001\n")
    with main.app.app_context():
        utils.create_user_home_directory("csuhyeon", 21001, 21001)
    assert len(fake.ran) == 3


def test_ignores_unparsable_stat_output(nas):
    """stat 이 숫자가 아닌 값을 돌려주면 판정하지 않고 진행한다. 조회 실패로 계정 생성을
    막지는 않되, 막아야 할 때를 놓치지 않도록 숫자일 때만 비교한다."""
    fake = nas(0, b"?\n")
    with main.app.app_context():
        utils.create_user_home_directory("someone", 21006, 21006)
    assert len(fake.ran) == 3


def test_step_create_home_reports_mismatch_with_own_error_code(monkeypatch, logs):
    """저널과 응답에 NAS_SSH_FAILED 가 아니라 HOME_OWNER_MISMATCH 가 남아야 한다."""
    from lifecycle_steps import provision

    def boom(name, uid, gid):
        raise utils.HomeOwnerMismatch("홈이 이미 uid 20016 소유입니다")

    monkeypatch.setattr(main, "create_user_home_directory", boom)
    monkeypatch.setattr(main, "_rollback_user", lambda name: None)
    with main.app.app_context():
        with pytest.raises(main.StepFailed) as err:
            provision.step_create_home({"request_id": "r1", "name": "yoon6yo", "uid": 21000, "gid": 21000})
    assert err.value.body["error"] == "HOME_OWNER_MISMATCH"
    assert err.value.body["step"] == "CREATE_HOME_DIRECTORY"
    assert any(r.get("error_code") == "HOME_OWNER_MISMATCH" for r in logs)
    assert err.value.retry is False, "사람이 uid를 맞춰야 풀리므로 재시도해도 같은 결과 — 재시도 대상이면 안 된다"


def test_step_create_home_retries_on_plain_nas_failure(monkeypatch, logs):
    """소유자 불일치가 아닌 일반 NAS 장애(SSH 끊김 등)는 여전히 재시도 대상이다."""
    from lifecycle_steps import provision

    def boom(name, uid, gid):
        raise ConnectionError("nas ssh refused")

    monkeypatch.setattr(main, "create_user_home_directory", boom)
    monkeypatch.setattr(main, "_rollback_user", lambda name: None)
    with main.app.app_context():
        with pytest.raises(main.StepFailed) as err:
            provision.step_create_home({"request_id": "r1", "name": "yoon6yo", "uid": 21000, "gid": 21000})
    assert err.value.body["error"] == "NAS_SSH_FAILED"
    assert err.value.retry is True


# ---------- 팀 공유 디렉터리 (#154) ----------

def test_team_dir_is_root_owned_setgid_and_closed_to_others(nas):
    fake = nas(1, b"")
    with main.app.app_context():
        utils.create_team_directory("teamx", 70000)
    assert fake.ran == ["sudo mkdir -p /volume1/share/user/_g_teamx",
                        "sudo chown 0:70000 /volume1/share/user/_g_teamx",
                        "sudo chmod 2770 /volume1/share/user/_g_teamx"]


def test_team_dir_rerun_with_same_gid_is_safe(nas):
    fake = nas(0, b"70000\n")
    with main.app.app_context():
        utils.create_team_directory("teamx", 70000)
    assert len(fake.ran) == 3


def test_team_dir_owned_by_another_group_is_left_alone(nas):
    fake = nas(0, b"70001\n")
    with main.app.app_context():
        with pytest.raises(utils.TeamDirGroupMismatch):
            utils.create_team_directory("teamx", 70000)
    assert fake.ran == []


def test_team_dir_rejects_unsafe_names(nas):
    fake = nas(1, b"")
    with main.app.app_context():
        for bad in ["team x", "a;rm -rf /", "../etc", ""]:
            with pytest.raises(ValueError):
                utils.create_team_directory(bad, 70000)
    assert fake.ran == []


def test_home_path_refuses_the_team_prefix(nas):
    """사용자 홈 삭제가 팀 디렉터리를 지우지 않도록 _g_ 로 시작하는 이름은 홈으로 쓰지 않는다."""
    fake = nas(1, b"")
    with pytest.raises(ValueError):
        utils.delete_user_home_directory("_g_teamx")
    assert fake.ran == []
