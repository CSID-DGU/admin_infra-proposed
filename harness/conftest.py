"""가상 계층(config-server/tests) 의 fixture 를 harness 시험에서 재사용하는 배선.

베껴 오지 않고 파일 경로로 직접 읽어서 쓴다. pytest_plugins 는 최상위가 아닌 conftest 에서
거부되고, import conftest 는 pytest 가 이미 올려 둔 이 파일 자신을 돌려주기 때문이다.
"""
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "config-server"), str(ROOT / "config-server" / "tests")]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


target_conftest = _load("cs_conftest", ROOT / "config-server" / "tests" / "conftest.py")
e2e = _load("cs_e2e", ROOT / "config-server" / "tests" / "test_e2e_virtual.py")


def _raw(fixture):
    """pytest fixture 객체에서 원래 함수를 꺼낸다."""
    return getattr(fixture, "__wrapped__", fixture)


@pytest.fixture
def lease(monkeypatch):
    return _raw(target_conftest.lease_env)(monkeypatch)


@pytest.fixture
def env(monkeypatch, tmp_path, lease):
    return _raw(e2e.env)(monkeypatch, tmp_path)
