"""사용자별 sudoers 파일: 정책이 바뀌면 이미 있는 파일도 새 내용으로 바꾼다."""
import os
import stat

import pytest

import utils


def test_creates_file_readable_only_by_owner_and_group(tmp_path):
    path = utils.ensure_sudoers_file(str(tmp_path), "alice", "alice ALL=(ALL) ALL")
    assert open(path).read() == "alice ALL=(ALL) ALL\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o440


def test_existing_file_follows_a_changed_policy(tmp_path):
    """있다는 것만으로 건너뛰면 정책을 바꿔 배포해도 기존 사용자에게는 옛 정책이 남는다."""
    utils.ensure_sudoers_file(str(tmp_path), "alice", "alice ALL=(ALL) NOPASSWD:ALL")
    path = utils.ensure_sudoers_file(str(tmp_path), "alice", "alice ALL=(ALL) ALL")
    assert open(path).read() == "alice ALL=(ALL) ALL\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o440


def test_same_policy_leaves_the_file_untouched(tmp_path):
    path = utils.ensure_sudoers_file(str(tmp_path), "alice", "alice ALL=(ALL) ALL")
    before = os.stat(path).st_ino
    utils.ensure_sudoers_file(str(tmp_path), "alice", "alice ALL=(ALL) ALL\n")
    assert os.stat(path).st_ino == before


def test_leaves_no_temporary_file_behind(tmp_path):
    utils.ensure_sudoers_file(str(tmp_path), "alice", "one")
    utils.ensure_sudoers_file(str(tmp_path), "alice", "two")
    assert sorted(n for n in os.listdir(tmp_path) if ".tmp." in n) == []


@pytest.mark.parametrize("bad", ["alice\n", "a/b", "../alice", "Alice", ""])
def test_rejects_names_that_are_not_plain_unix_names(tmp_path, bad):
    with pytest.raises(ValueError):
        utils.ensure_sudoers_file(str(tmp_path), bad, "x")
