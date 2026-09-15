"""작업 단계 기록 조회 API — 신청 상세 화면용."""
import json
from datetime import datetime

import main


class FakeCursor:
    """execute가 불릴 때마다 준비한 결과를 순서대로 돌려준다."""

    def __init__(self, results):
        self.results, self.queries, self.current = list(results), [], []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        self.current = self.results.pop(0) if self.results else []

    def fetchall(self):
        return self.current

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _db(monkeypatch, *results):
    cur = FakeCursor(results)
    monkeypatch.setattr(main, "get_log_db_connection", lambda: FakeConn(cur))
    return cur


def _t(sec):
    return datetime(2026, 9, 15, 5, 7, sec)


def test_unknown_kind_is_404(api):
    assert api.get("/operations/migrate/3/steps").status_code == 404


def test_request_without_jobs_returns_empty_list(api, monkeypatch):
    _db(monkeypatch, [])
    body = api.get("/operations/provision/3/steps").get_json()
    assert body == {"request_id": "3", "kind": "provision", "jobs": []}


def test_steps_are_grouped_per_job_newest_first_without_step_start_rows(api, monkeypatch):
    cur = _db(monkeypatch, [(432,), (358,)], [
        (358, "PROVISION", "START", 1, None, None, None, _t(0)),
        (358, "PROVISION", "FAIL", 1, None, "DEGRADED",
         json.dumps({"degraded": True, "step": "step_verify_uid", "reason": "RETRIES_EXHAUSTED",
                     "error": {"detail": "'uid'"}}), _t(10)),
        (432, "PROVISION", "START", 1, None, None, None, _t(41)),
        (432, "CREATE_POD_K8S", "START", 1, "pod", None, None, _t(44)),
        (432, "CREATE_POD_K8S", "SUCCESS", 1, "pod", None, None, _t(45)),
        (432, "VERIFY_ACCESS", "FAIL", 1, "endpoint", None,
         json.dumps({"scope": "tcp 192.168.2.12:32252", "connected": False,
                     "banner": "[Errno 111] Connection refused"}), _t(48)),
        (432, "PROVISION", "RETRY", 2, "step_verify_endpoint", "VERIFY_ENDPOINT_FAILED",
         "VERIFY_ENDPOINT_FAILED", _t(48)),
        (432, "VERIFY_ACCESS", "SUCCESS", 2, "endpoint", None,
         json.dumps({"scope": "tcp 192.168.2.12:32252", "connected": True}), _t(50)),
        (432, "PROVISION", "SUCCESS", 1, None, None, None, _t(50)),
    ])

    body = api.get("/operations/provision/3/steps").get_json()

    assert [j["job_id"] for j in body["jobs"]] == [432, 358]
    latest, older = body["jobs"]
    assert latest["phase"] == "SUCCESS"
    assert latest["started_at"] == "2026-09-15T05:07:41Z" and latest["finished_at"] == "2026-09-15T05:07:50Z"
    assert [s["action"] for s in latest["steps"]] == [
        "CREATE_POD_K8S", "VERIFY_ACCESS", "PROVISION", "VERIFY_ACCESS", "PROVISION"]
    retry = latest["steps"][2]
    assert retry["phase"] == "RETRY" and retry["attempt"] == 2 and retry["step"] == "step_verify_endpoint"
    first_probe = latest["steps"][1]
    assert first_probe["probe"] == "endpoint" and first_probe["summary"] == {"connected": False}
    assert older["phase"] == "FAIL" and older["error_code"] == "DEGRADED"
    assert older["steps"][-1]["summary"] == {"degraded": True, "step": "step_verify_uid",
                                             "reason": "RETRIES_EXHAUSTED"}
    # 두 번째 조회는 첫 조회로 찾은 작업 번호만 대상으로 한다
    assert cur.queries[1][1] == (432, 358)


def test_summary_drops_internal_addresses_mount_paths_and_command_output(api, monkeypatch):
    _db(monkeypatch, [(7,)], [
        (7, "PROVISION", "START", 1, None, None, None, _t(0)),
        (7, "VERIFY_ACCESS", "SUCCESS", 1, "home_io", None, json.dumps({
            "scope": "pod exec su + df + stat", "roundtrip": True,
            "mount_src": "nas.farm.decs.internal:/volume1/share/user", "owner_uid": "65534", "rc": 0}), _t(1)),
        (7, "VERIFY_ACCESS", "SUCCESS", 1, "gpu", None, json.dumps({
            "node": "farm2", "requested": 2, "visible": 2}), _t(2)),
        (7, "VERIFY_ACCESS", "FAIL", 1, "uid", "VERIFY_TOOL_FAILED", "'uid'", _t(3)),
    ])

    steps = api.get("/operations/provision/3/steps").get_json()["jobs"][0]["steps"]

    home, gpu, tool = steps
    assert home["summary"] == {"roundtrip": True, "owner_uid": "65534", "rc": 0}
    assert "mount_src" not in json.dumps(home) and "internal" not in json.dumps(home)
    assert gpu["summary"] == {"node": "farm2", "requested": 2, "visible": 2}
    assert tool["summary"] is None and tool["error_code"] == "VERIFY_TOOL_FAILED"


def test_unfinished_job_reports_start_phase(api, monkeypatch):
    _db(monkeypatch, [(9,)], [
        (9, "REVOKE", "START", 1, None, None, None, _t(0)),
        (9, "DELETE_SERVICE", "SUCCESS", 1, "service", None, None, _t(1)),
    ])
    job = api.get("/operations/revoke/3/steps").get_json()["jobs"][0]
    assert job["phase"] == "START" and job["finished_at"] is None
    assert [s["action"] for s in job["steps"]] == ["DELETE_SERVICE"]
