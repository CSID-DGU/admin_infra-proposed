"""measure.py 를 가짜 스택 위에서 확인한다. system 의 네 stack 함수를 바꿔 끼운다."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure  # noqa: E402
import system  # noqa: E402
from test_measure_ports import FakeStack  # noqa: E402

PASSWORD = "Pw-only-in-memory-0123456789"
JWT = "jwt-signing-key-0123456789"


class FakeCluster(FakeStack):
    def __init__(self):
        super().__init__()
        self.lock = None
        self.kube_calls = []
        self.log_lines = ""
        self.pods = ""

    def sql(self, host, namespace, database, statement):
        if statement.startswith("SELECT u.ubuntu_username"):
            return self._rows(["username"], [["exp-fu-mabc01"]])
        if statement.startswith("SELECT UNIX_TIMESTAMP"):
            return self._rows(["t", "action", "resource_type", "phase", "attempt", "error_code"],
                              [["1790000000.000", "PROVISION", "JOB", "START", "1", None],
                               ["1790000020.000", "PROVISION", "JOB", "SUCCESS", "1", None]])
        return super().sql(host, namespace, database, statement)

    def kube(self, host, namespace, args, *, stdin=None):
        self.kube_calls.append(args)
        ok = {"rc": 0, "stdout": "", "stderr": ""}
        if args[:2] == ["create", "configmap"]:
            if self.lock:
                return {"rc": 1, "stdout": "", "stderr": "AlreadyExists"}
            self.lock = dict(a.split("=", 1) for a in (x.removeprefix("--from-literal=") for x in args[3:]))
            return ok
        if args[:2] == ["get", "configmap"]:
            if not self.lock:
                return {"rc": 1, "stdout": "", "stderr": "NotFound"}
            return {**ok, "stdout": json.dumps({"data": self.lock})}
        if args[:2] == ["delete", "configmap"]:
            self.lock = None
            return ok
        if args[:2] == ["get", "deployment"]:
            items = [{"metadata": {"name": n}, "spec": {"template": {"spec": {"containers": [{"image": f"{n}:1"}]}}}}
                     for n in args[2:5]]
            return {**ok, "stdout": json.dumps({"items": items})}
        if args[:2] == ["get", "pods"]:
            return {**ok, "stdout": self.pods}
        if args[0] == "logs":
            return {**ok, "stdout": self.log_lines}
        return ok


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    fake = FakeCluster()
    monkeypatch.setattr(system, "stack_sql", fake.sql)
    monkeypatch.setattr(system, "stack_http", fake.http_call)
    monkeypatch.setattr(system, "stack_kube", fake.kube)
    monkeypatch.setattr(system, "stack_secret", lambda h, n, k: {"rc": 0, "value": JWT, "stderr": ""})
    monkeypatch.setattr(measure.secrets, "token_urlsafe", lambda n: PASSWORD)
    monkeypatch.setattr(measure.secrets, "token_hex", lambda n: "abc")
    clock = [0.0]

    def sleep(sec):
        clock[0] += sec
        deleted = any(p.endswith("/ubuntu-account") for _, p, _, _ in fake.http)
        fake.request_status = "DELETED" if deleted else "FULFILLED"

    monkeypatch.setattr(measure.time, "sleep", sleep)
    monkeypatch.setattr(measure.time, "monotonic", lambda: clock[0])
    env = tmp_path / "measure.env"
    env.write_text(f"KUBE_HOST=farm1\nADMIN_INFRA_SERVER={tmp_path}\n")
    monkeypatch.setenv("MEASURE_ENV", str(env))
    monkeypatch.setenv(system.ENV_VAR, "")
    return fake


def _pair(tmp_path, *extra):
    return measure.main(["pair", "--stack", "full", "--reps", "1", "--horizon", "30", "--poll", "10",
                         "--out", str(tmp_path / "out"), *extra])


def test_missing_config_names_keys(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MEASURE_ENV", str(tmp_path / "none.env"))
    assert measure.main(["release", "--stack", "full", "--force"]) == 2
    err = capsys.readouterr().err
    assert "KUBE_HOST" in err and "ADMIN_INFRA_SERVER" in err


def test_operation_stack_rejected():
    with pytest.raises(SystemExit) as e:
        measure.main(["pair", "--stack", "operation"])
    assert e.value.code == 2


def test_pair_holds_lock_writes_two_files_without_password(cluster, tmp_path, capsys):
    assert _pair(tmp_path) == 0
    creates = [a for a in cluster.kube_calls if a[:2] == ["create", "configmap"]]
    assert len(creates) == 1 and "--from-literal=run=abc" in creates[0]
    assert cluster.lock is None
    files = sorted((tmp_path / "out").glob("*.json"))
    assert [f.name for f in files] == ["full-abc-01-c.json", "full-abc-01-r.json"]
    for f in files:
        text = f.read_text()
        assert PASSWORD not in text and JWT not in text
    assert json.loads(files[0].read_text())["system_declaration"]["value"] == "FULFILLED"
    out = capsys.readouterr().out
    assert "exp-fu-mabc01" in out and "full-abc-01-r" in out


def test_pair_exits_3_when_locked_and_creates_no_user(cluster, tmp_path):
    cluster.lock = {"owner": "e2e", "run": "other", "started_at": "x"}
    assert _pair(tmp_path) == 3
    assert not any(s.startswith("INSERT INTO users") for _, s in cluster.statements)
    assert cluster.lock["run"] == "other"


def test_out_inside_repo_rejected(cluster, capsys):
    assert measure.main(["pair", "--stack", "full", "--out", str(measure.REPO_ROOT / "harness" / "x")]) == 2
    assert "레포 안" in capsys.readouterr().err
    assert cluster.lock is None and cluster.kube_calls == []


def test_logs_merges_journal_and_log_lines_in_time_order(cluster, capsys):
    cluster.log_lines = "\n".join([
        "2026-09-21T14:13:30.000000000Z step done request=812",   # 1790000010
        "2026-09-21T14:13:31.000000000Z unrelated line",
        "2026-09-21T14:13:50.000000000Z exp-fu-mabc01 pod ready",  # 1790000030
    ])
    assert measure.main(["logs", "--stack", "full", "--request", "812"]) == 0
    lines = capsys.readouterr().out.splitlines()
    kinds = [line.split()[2] + ":" + line.split()[3] for line in lines]
    assert kinds == ["journal:action=PROVISION", "log:step", "journal:action=PROVISION", "log:exp-fu-mabc01"]
    assert "phase=START" in lines[0] and "phase=SUCCESS" in lines[2]
    assert not any("unrelated" in line for line in lines)


def test_password_never_printed(cluster, tmp_path, capsys):
    assert _pair(tmp_path) == 0
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert PASSWORD not in stream and JWT not in stream


def test_release_requires_run_or_force():
    with pytest.raises(SystemExit):
        measure.main(["release", "--stack", "full"])


def test_release_other_run_refused(cluster):
    cluster.lock = {"owner": "measure", "run": "other", "started_at": "x"}
    assert measure.main(["release", "--stack", "full", "--run", "abc"]) == 3
    assert cluster.lock is not None
    assert measure.main(["release", "--stack", "full", "--force"]) == 0
    assert cluster.lock is None



BYSTANDER = "exp-fu-mabcb"


def _records(tmp_path):
    return [json.loads(f.read_text()) for f in sorted((tmp_path / "out").glob("*.json"))]


def test_bystander_created_and_revoked_once_and_in_every_trial(cluster, tmp_path):
    assert _pair(tmp_path, "--reps", "2") == 0
    inserts = [s for _, s in cluster.statements if s.startswith("INSERT INTO users") and BYSTANDER in s]
    assert len(inserts) == 1
    calls = [(m, p) for m, p, _, _ in cluster.http]
    # 방관자 1 + trial 사용자 2. 방관자 신청이 맨 앞, 방관자 회수가 맨 뒤다.
    assert calls.count(("POST", "/api/requests")) == 3 and calls[0] == ("POST", "/api/requests")
    deletes = [c for c in calls if c[0] == "DELETE"]
    assert len(deletes) == 3 and calls[-1] == deletes[-1]
    records = _records(tmp_path)
    assert len(records) == 4
    for r in records:
        assert list(r["protection"]["before"]["bystanders"]) == [BYSTANDER]
        assert list(r["protection"]["after"]["bystanders"]) == [BYSTANDER]
        assert set(r["snapshots"]) == {"before", "after"}


def test_bystander_not_fulfilled_runs_without_protection(cluster, tmp_path, monkeypatch, capsys):
    real_sql = cluster.sql
    held = [True]

    def sql(host, namespace, database, statement):
        if statement.startswith("INSERT INTO users") and "exp-fu-mabc01" in statement:
            held[0] = False
        if held[0] and statement.startswith("SELECT status FROM requests"):
            return cluster._rows(["status"], [["PENDING"]])
        return real_sql(host, namespace, database, statement)

    monkeypatch.setattr(system, "stack_sql", sql)
    assert _pair(tmp_path) == 0
    records = _records(tmp_path)
    assert len(records) == 2 and all(r["protection"] is None for r in records)
    assert "FULFILLED 가 되지 않았다" in capsys.readouterr().out
    # 신청은 나갔으므로 회수도 보낸다.
    assert sum(1 for m, *_ in cluster.http if m == "DELETE") == 2


def test_bystander_password_never_printed_or_saved(cluster, tmp_path, monkeypatch, capsys):
    issued = []

    def token(n):
        issued.append(f"Pw-distinct-{len(issued):02d}-0123456789")
        return issued[-1]

    monkeypatch.setattr(measure.secrets, "token_urlsafe", token)
    assert _pair(tmp_path) == 0
    captured = capsys.readouterr()
    texts = [captured.out, captured.err] + [f.read_text() for f in (tmp_path / "out").glob("*.json")]
    assert len(issued) == 3  # 관리자, 방관자, trial 사용자
    for pw in issued:
        assert all(pw not in t for t in texts)


def test_bystander_resources_are_not_residue(cluster, tmp_path):
    cluster.pods = f"ailab-{BYSTANDER}-1234abcd {BYSTANDER}\n"
    assert _pair(tmp_path) == 0
    for r in _records(tmp_path):
        assert r["environment"]["evidence"]["residue"] == []
        assert r["environment"]["verdict"] != "DIRTY"
        assert BYSTANDER in r["snapshots"]["before"]["by_user"]
