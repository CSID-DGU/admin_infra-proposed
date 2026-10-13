"""원장 쓰기 — 임시 파일에 다 쓴 뒤 바꿔 끼워, 쓰다 죽어도 옛 내용이나 새 내용 하나만 남는다."""
import os
import stat

import pytest

import main
import utils
from lifecycle_steps import group as group_steps


@pytest.fixture
def etc(tmp_path, monkeypatch):
    """계정 원장을 임시 파일로 돌린다."""
    base = str(tmp_path / "etc")
    monkeypatch.setenv("LEDGER_LOCK_BACKEND", "local")
    for k, val in list(main.app.config.items()):
        if isinstance(val, str) and val.startswith(main.BASE_ETC_DIR):
            monkeypatch.setitem(main.app.config, k, base + val[len(main.BASE_ETC_DIR):])
    with main.app.app_context():
        main.ensure_etc_layout()
        yield base


def _read(path):
    with open(path) as f:
        return f.read()


def _fail_before_swap(monkeypatch):
    """임시 파일을 디스크에 내리는 순간 죽는 상황을 만든다."""
    def boom(fd):
        raise OSError("disk gone")
    monkeypatch.setattr(utils.os, "fsync", boom)


def test_replaces_content_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "ledger"
    path.write_text("old\n")
    utils.replace_file_atomically(str(path), "new\n")
    assert path.read_text() == "new\n"
    assert os.listdir(tmp_path) == ["ledger"]


def test_shorter_content_does_not_keep_old_tail(tmp_path):
    path = tmp_path / "ledger"
    path.write_text("a:x:1:\nb:x:2:\nc:x:3:\n")
    utils.replace_file_atomically(str(path), "a:x:1:\n")
    assert path.read_text() == "a:x:1:\n"


def test_failure_keeps_old_content_and_removes_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "ledger"
    path.write_text("old\n")
    _fail_before_swap(monkeypatch)
    with pytest.raises(OSError):
        utils.replace_file_atomically(str(path), "new-but-never-landed\n")
    assert path.read_text() == "old\n"
    assert os.listdir(tmp_path) == ["ledger"]


def test_keeps_mode_of_existing_file(tmp_path):
    path = tmp_path / "shadow"
    path.write_text("old\n")
    os.chmod(path, 0o640)
    utils.replace_file_atomically(str(path), "new\n")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640


def test_new_file_is_world_readable(tmp_path):
    path = tmp_path / "issued_id_max.json"
    utils.replace_file_atomically(str(path), "{}\n")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644


@pytest.mark.parametrize("key,write", [
    ("PASSWD_PATH", lambda: utils.write_passwd_lines(["alice:x:60001:60001::/home/alice:/bin/bash"])),
    ("GROUP_PATH", lambda: utils.write_group_lines(["teamx:x:70001:alice"])),
    ("SHADOW_PATH", lambda: utils.write_shadow_lines(["alice:!:20000:0:99999:7:::"])),
    ("GROUP_PATH", lambda: group_steps._write_group_line("teamx", 70001, ["alice"])),
])
def test_ledger_writers_keep_old_content_when_write_dies(etc, monkeypatch, key, write):
    path = main.app.config[key]
    before = _read(path)
    assert before
    _fail_before_swap(monkeypatch)
    with pytest.raises(Exception):
        write()
    assert _read(path) == before
    assert not [n for n in os.listdir(etc) if ".tmp." in n]


def test_password_change_keeps_old_shadow_when_write_dies(etc, monkeypatch):
    utils.write_shadow_lines(utils.read_shadow_lines() + ["alice:oldhash:20000:0:99999:7:::"])
    before = _read(main.app.config["SHADOW_PATH"])
    _fail_before_swap(monkeypatch)
    with pytest.raises(OSError):
        main.set_ledger_password("alice", "newhash")
    assert _read(main.app.config["SHADOW_PATH"]) == before


def test_issued_id_record_survives_failed_write(etc, monkeypatch):
    utils.record_issued_id("uid", 60005)
    with monkeypatch.context() as broken:
        _fail_before_swap(broken)
        with pytest.raises(OSError):
            utils.record_issued_id("uid", 60009)
    assert utils.read_issued_id_max("uid") == 60005
