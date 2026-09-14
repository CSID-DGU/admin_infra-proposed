"""job_control의 실제 SQL 경로 검증 (sqlite로 실행 — %s만 ?로 치환).
lease 의미론: 선점·재선점·만료 인수·인수 경쟁·fencing·해제."""
import sqlite3
import time

import pytest

import job_control

# conftest의 autouse lease_env가 대역으로 바꾸기 전에 실제 구현을 잡아 둔다
_REAL = {n: getattr(job_control, n) for n in ("claim", "record_step", "release", "renew")}


class Sql:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        outer = self

        class Cur:
            def __enter__(self):
                self.c = outer.db.cursor()
                return self

            def __exit__(self, *a):
                pass

            def execute(self, sql, params=()):
                self.c.execute(sql.replace("%s", "?"), params)

            def fetchone(self):
                return self.c.fetchone()

            @property
            def rowcount(self):
                return self.c.rowcount
        return Cur()

    def commit(self):
        self.db.commit()

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("""CREATE TABLE job_control (job_id INTEGER PRIMARY KEY, request_id TEXT, action TEXT,
        owner TEXT NOT NULL, lease_until REAL NOT NULL, done_steps TEXT, saved_ctx TEXT)""")
    monkeypatch.setattr(job_control, "get_log_db_connection", lambda: Sql(conn))
    for n, fn in _REAL.items():                       # 이 파일은 실제 SQL 경로를 검증한다
        monkeypatch.setattr(job_control, n, fn)
    return conn


def test_claim_new_job_then_reclaim_by_same_owner(db):
    assert job_control.claim(1, "9", "PROVISION", owner="me") == ([], {})
    job_control.record_step(1, ["s_a"], {"pod_name": "p1"}, owner="me")
    assert job_control.claim(1, "9", "PROVISION", owner="me") == (["s_a"], {"pod_name": "p1"})


def test_live_lease_blocks_other_owner_until_expiry(db):
    job_control.claim(1, "9", "PROVISION", owner="alive")
    assert job_control.claim(1, "9", "PROVISION", owner="other") is None
    db.execute("UPDATE job_control SET lease_until=?", (time.time() - 1,))   # 만료
    assert job_control.claim(1, "9", "PROVISION", owner="other") == ([], {})


def test_expired_lease_takeover_race_has_single_winner(db):
    job_control.claim(1, "9", "PROVISION", owner="dead")
    db.execute("UPDATE job_control SET lease_until=?", (time.time() - 1,))
    until = db.execute("SELECT lease_until FROM job_control").fetchone()[0]
    assert job_control.claim(1, "9", "PROVISION", owner="w1") == ([], {})
    # w2는 w1과 같은 만료 행을 봤더라도 UPDATE의 WHERE(직전 소유자·시각)에서 진다
    row = db.execute("SELECT owner FROM job_control").fetchone()
    assert row[0] == "w1"
    # 소유자가 이미 바뀐 뒤의 늦은 인수 UPDATE는 rowcount 0 — 경쟁의 패자는 물러난다
    with Sql(db).cursor() as cur:
        cur.execute("UPDATE job_control SET owner=%s, lease_until=%s WHERE job_id=%s AND owner=%s AND lease_until=%s",
                    ("w2", time.time() + 30, 1, "dead", until))
        assert cur.rowcount == 0


def test_record_step_after_ownership_change_raises_lease_lost(db):
    job_control.claim(1, "9", "PROVISION", owner="old")
    db.execute("UPDATE job_control SET owner='new'")
    db.commit()
    with pytest.raises(job_control.LeaseLost):
        job_control.record_step(1, ["s_a"], {}, owner="old")


def test_renew_only_touches_own_jobs_and_release_removes_row(db):
    job_control.claim(1, "9", "PROVISION", owner="me")
    job_control.claim(2, "10", "PROVISION", owner="other")
    assert job_control.renew([1, 2], owner="me") == 1
    job_control.release(1, owner="other")                                    # 남의 해제는 무시
    assert db.execute("SELECT COUNT(*) FROM job_control").fetchone()[0] == 2
    job_control.release(1, owner="me")
    assert db.execute("SELECT COUNT(*) FROM job_control WHERE job_id=1").fetchone()[0] == 0
