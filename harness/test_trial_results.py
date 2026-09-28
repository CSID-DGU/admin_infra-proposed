"""trial_results.py 계약 검증."""
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trial_results  # noqa: E402


def _record(trial_id="trial-0001"):
    return {
        "trial_id": trial_id,
        "method": "full",
        "timestamps": {"submitted": "2026-09-28 10:00:00", "declared": None},
        "system_declaration": {"value": "FULFILLED", "at": "2026-09-28 10:01:00"},
        "independent_verdict": {"at_declaration": {"verdict": "UNKNOWN"}, "at_horizon": None},
    }


def test_save_then_load_roundtrip(tmp_path):
    rec = _record()
    path = trial_results.save(tmp_path, rec, secrets=())
    assert path == tmp_path / "trial-0001.json"
    assert trial_results.load(tmp_path, "trial-0001") == {**rec, "schema_version": trial_results.SCHEMA_VERSION}


def test_second_save_raises_and_keeps_original(tmp_path):
    trial_results.save(tmp_path, _record(), secrets=())
    before = (tmp_path / "trial-0001.json").read_bytes()
    changed = {**_record(), "method": "noprobe"}
    with pytest.raises(FileExistsError):
        trial_results.save(tmp_path, changed, secrets=())
    assert (tmp_path / "trial-0001.json").read_bytes() == before


def test_missing_trial_is_none(tmp_path):
    assert trial_results.load(tmp_path, "trial-none") is None


def test_corrupt_file_raises(tmp_path):
    (tmp_path / "trial-0001.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        trial_results.load(tmp_path, "trial-0001")


def test_no_temp_file_left(tmp_path):
    trial_results.save(tmp_path, _record(), secrets=())
    with pytest.raises(FileExistsError):
        trial_results.save(tmp_path, _record(), secrets=())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["trial-0001.json"]


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "a" * 65])
def test_bad_trial_id_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        trial_results.save(tmp_path, _record(bad), secrets=())
    with pytest.raises(ValueError):
        trial_results.load(tmp_path, bad)


def test_bytes_value_saved_as_string(tmp_path):
    rec = {**_record(), "evidence": {"raw": b"\x00ok"}}
    trial_results.save(tmp_path, rec, secrets=())
    loaded = trial_results.load(tmp_path, "trial-0001")
    assert loaded["evidence"]["raw"] == repr(b"\x00ok")


def test_schema_version_in_record_rejected(tmp_path):
    with pytest.raises(ValueError):
        trial_results.save(tmp_path, {**_record(), "schema_version": 1}, secrets=())
    assert not (tmp_path / "trial-0001.json").exists()


def test_missing_subdirectory_created(tmp_path):
    target = tmp_path / "a" / "b"
    trial_results.save(target, _record(), secrets=())
    assert trial_results.load(target, "trial-0001")["trial_id"] == "trial-0001"


PASSWORD = "Pa55-word-for-trial"


def _deep(value):
    return {**_record(), "evidence": {"checks": [{"login": {"stdout": ["x", {"echo": value}]}}]}}


def test_secret_deep_in_evidence_rejected_without_any_file(tmp_path):
    with pytest.raises(ValueError):
        trial_results.save(tmp_path, _deep(f"prefix {PASSWORD} suffix"), secrets=[PASSWORD])
    assert list(tmp_path.iterdir()) == []


def test_escaped_str_secret_detected(tmp_path):
    secret = 'a"b\\c\'d\nefgh'
    assert json.dumps(secret)[1:-1] != secret
    with pytest.raises(ValueError):
        trial_results.save(tmp_path, _deep(secret), secrets=[secret])
    assert list(tmp_path.iterdir()) == []


KEYTAB = b"\x05\x02\x00\x00keytab'\"\\\xff-body"


@pytest.mark.parametrize("value", [
    KEYTAB,
    KEYTAB.hex(),
    base64.b64encode(KEYTAB).decode("ascii"),
], ids=["repr", "hex", "base64"])
def test_bytes_secret_detected_in_every_form(tmp_path, value):
    with pytest.raises(ValueError):
        trial_results.save(tmp_path, _deep(value), secrets=[KEYTAB])
    assert list(tmp_path.iterdir()) == []


def test_error_message_does_not_contain_secret(tmp_path):
    with pytest.raises(ValueError) as exc:
        trial_results.save(tmp_path, _deep(PASSWORD), secrets=["unrelated-secret", PASSWORD])
    assert PASSWORD not in str(exc.value)
    assert "secrets[1]" in str(exc.value)


@pytest.mark.parametrize("secret", ["1234567", "", b"1234567", b""])
def test_short_or_empty_secret_rejected(tmp_path, secret):
    with pytest.raises(ValueError):
        trial_results.save(tmp_path, _record(), secrets=[secret])
    assert list(tmp_path.iterdir()) == []


def test_saves_normally_when_secret_absent(tmp_path):
    trial_results.save(tmp_path, _record(), secrets=[PASSWORD, KEYTAB])
    assert trial_results.load(tmp_path, "trial-0001")["trial_id"] == "trial-0001"


def test_secrets_argument_is_required(tmp_path):
    with pytest.raises(TypeError):
        trial_results.save(tmp_path, _record())
