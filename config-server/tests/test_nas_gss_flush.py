"""#153 공용 그룹이 바뀌면 NAS 의 GSS 컨텍스트 캐시를 비운다.

NAS 는 컨텍스트를 맺을 때 그룹을 한 번 풀어 고정하고 하루 동안 다시 보지 않는다. 비우지
않으면 AD 를 고쳐도 반영되지 않고, **NAS 가 아직 모르는 상태에서 비우면 더 나빠진다** —
다음 컨텍스트가 옛 목록으로 다시 굳는다. 그래서 "바뀌었나"와 "NAS 가 아는가"를 둘 다 본다.
"""
import os

import pytest

import main
import reconcile_krb5 as rec


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """계정 대장을 임시 파일로 돌리고 NAS 호출을 가로챈다.
    반환: (group 파일 쓰기 함수, flush 호출 횟수를 담은 리스트)"""
    base = str(tmp_path / "etc")
    for k, val in list(main.app.config.items()):
        if isinstance(val, str) and val.startswith(main.BASE_ETC_DIR):
            monkeypatch.setitem(main.app.config, k, base + val[len(main.BASE_ETC_DIR):])
    monkeypatch.setattr(rec, "SHARED_GID_MIN", 70000)
    monkeypatch.setattr(rec, "SHARED_GID_MAX", 79999)

    flushes = []
    monkeypatch.setattr(rec, "nas_flush_gss_cache", lambda: flushes.append(1))

    with main.app.app_context():
        main.ensure_etc_layout()

        def write_group(*lines, users=("alice", "bob")):
            # 대조는 대장의 계정 목록을 훑으므로 passwd 에도 있어야 한다(그룹에서 뺀 사용자를
            # 놓치지 않으려고 멤버가 아니라 계정 기준으로 보기 때문).
            main.write_passwd_lines(
                [f"{u}:x:{21000 + i}:{21000 + i}::/home/{u}:/bin/bash"
                 for i, u in enumerate(users)])
            main.write_group_lines(list(lines))
        yield write_group, flushes


def _nas_says(monkeypatch, mapping):
    monkeypatch.setattr(rec, "nas_shared_gids_for_users",
                        lambda users, lo, hi: {u: mapping[u] for u in users if u in mapping})


def test_flushes_when_nas_already_knows_the_new_group(ledger, monkeypatch):
    write_group, flushes = ledger
    write_group("alice:x:21000:", "teamx:x:70000:alice")
    _nas_says(monkeypatch, {"alice": {70000}})
    with main.app.app_context():
        rec.reconcile_nas_gss_cache()
    assert flushes == [1]


def test_does_not_flush_while_nas_still_lags(ledger, monkeypatch):
    """제일 중요한 시험 — 여기서 비우면 옛 목록이 하루 더 굳는다."""
    write_group, flushes = ledger
    write_group("alice:x:21000:", "teamx:x:70000:alice")
    _nas_says(monkeypatch, {"alice": set()})       # winbind 가 아직 모름
    with main.app.app_context():
        rec.reconcile_nas_gss_cache()
    assert flushes == []


def test_retries_on_the_next_run_after_a_lag(ledger, monkeypatch):
    """건너뛴 주기가 mtime 을 남기지 않아야 다음 주기가 다시 본다."""
    write_group, flushes = ledger
    write_group("alice:x:21000:", "teamx:x:70000:alice")
    _nas_says(monkeypatch, {"alice": set()})
    with main.app.app_context():
        rec.reconcile_nas_gss_cache()
        _nas_says(monkeypatch, {"alice": {70000}})
        rec.reconcile_nas_gss_cache()
    assert flushes == [1]


def test_does_not_flush_again_without_a_change(ledger, monkeypatch):
    write_group, flushes = ledger
    write_group("alice:x:21000:", "teamx:x:70000:alice")
    _nas_says(monkeypatch, {"alice": {70000}})
    with main.app.app_context():
        rec.reconcile_nas_gss_cache()
        rec.reconcile_nas_gss_cache()
        rec.reconcile_nas_gss_cache()
    assert flushes == [1]


def test_removal_also_waits_for_nas(ledger, monkeypatch):
    """회수도 본다 — NAS 가 아직 멤버로 보고 있으면 비우지 않는다.
    더한 것만 보면 그룹에서 뺀 사용자가 하루 더 접근한다."""
    write_group, flushes = ledger
    write_group("alice:x:21000:")                  # teamx 에서 뺐다
    _nas_says(monkeypatch, {"alice": {70000}})     # NAS 는 아직 멤버로 본다
    with main.app.app_context():
        rec.reconcile_nas_gss_cache()
    assert flushes == []


def test_nas_lookup_failure_is_not_recorded_as_done(ledger, monkeypatch):
    write_group, flushes = ledger
    write_group("alice:x:21000:", "teamx:x:70000:alice")

    def boom(users, lo, hi):
        raise RuntimeError("NAS SSH 실패")
    monkeypatch.setattr(rec, "nas_shared_gids_for_users", boom)
    with main.app.app_context():
        rec.reconcile_nas_gss_cache()
        assert flushes == []
        _nas_says(monkeypatch, {"alice": {70000}})
        rec.reconcile_nas_gss_cache()
    assert flushes == [1]


def test_flush_failure_is_not_recorded_as_done(ledger, monkeypatch):
    write_group, flushes = ledger
    write_group("alice:x:21000:", "teamx:x:70000:alice")
    _nas_says(monkeypatch, {"alice": {70000}})

    def boom():
        raise RuntimeError("NAS SSH 실패")
    monkeypatch.setattr(rec, "nas_flush_gss_cache", boom)
    with main.app.app_context():
        rec.reconcile_nas_gss_cache()
        monkeypatch.setattr(rec, "nas_flush_gss_cache", lambda: flushes.append(1))
        rec.reconcile_nas_gss_cache()
    assert flushes == [1]


def test_only_shared_band_counts(ledger, monkeypatch):
    """개인 그룹(gid=uid)은 컨테이너가 스스로 만들고 공용 대역 밖이라 대조 대상이 아니다.
    NAS 가 개인 그룹만 보고 있어도 대장과 일치한 것으로 본다."""
    write_group, flushes = ledger
    write_group("alice:x:21000:", "bob:x:21001:")
    _nas_says(monkeypatch, {"alice": set(), "bob": set()})
    with main.app.app_context():
        rec.reconcile_nas_gss_cache()
    assert flushes == [1]


def test_legacy_account_unknown_to_ad_does_not_block(ledger, monkeypatch):
    """AD 에 없는 레거시 계정은 id 가 실패한다. 공용 그룹이 안 걸려 있으면 막지 않는다."""
    write_group, flushes = ledger
    write_group("alice:x:21000:", "teamx:x:70000:alice")
    _nas_says(monkeypatch, {"alice": {70000}})     # legacy 는 응답에 없다
    with main.app.app_context():
        main.write_passwd_lines(main.read_passwd_lines() + [
            "legacy:x:20999:20999::/home/legacy:/bin/bash"])
        rec.reconcile_nas_gss_cache()
    assert flushes == [1]


def test_member_unknown_to_ad_blocks(ledger, monkeypatch):
    """공용 그룹 멤버인데 NAS 가 이름을 모르면 아직 반영 전이다 — 비우지 않는다."""
    write_group, flushes = ledger
    write_group("alice:x:21000:", "teamx:x:70000:alice")
    _nas_says(monkeypatch, {})
    with main.app.app_context():
        rec.reconcile_nas_gss_cache()
    assert flushes == []


def test_missing_ledger_is_not_an_error(ledger, monkeypatch):
    write_group, flushes = ledger
    with main.app.app_context():
        os.remove(main.app.config["GROUP_PATH"])
        rec.reconcile_nas_gss_cache()
    assert flushes == []
