"""trial 하나의 관측 기록을 JSON 파일 하나로 남기고 다시 읽는 유일한 자리.

네 결과 축 가운데 명령 결과와 검증 결과는 대상 시스템이 operation_log 에 남기지만, 시스템
선언과 독립 판정은 run_trial 의 반환 기록에만 있다. 이 모듈이 그 둘을 영속하는 유일한 자리이고,
여기서 저장하지 않으면 프로세스가 끝나는 순간 핵심 측정값이 사라진다. 파일과 trial_manifest
행은 trial_id 로 잇는다.

한 trial 의 기록은 한 번만 쓴다. 같은 trial_id 의 파일이 이미 있으면 덮어쓰지 않고 예외를
올린다. 측정값을 조용히 바꿔치기하면 어느 값이 원래 관측이었는지 말할 수 없기 때문이다.
임시 파일에 다 쓴 뒤 os.link 로 최종 이름을 붙이므로, 반쯤 쓰인 파일이 최종 이름으로
보이지 않고 이미 있는 파일도 덮어쓰지 않는다.

없는 것과 못 읽은 것을 가른다. 파일이 없으면 None 을 돌려주고 Metrics Analyzer 는 이를
결측으로 센다. 파일은 있는데 JSON 으로 읽히지 않으면 예외를 올린다. 손상된 기록을 결측으로
접으면 손상 자체가 보이지 않는다.

저장 위치는 부르는 쪽이 넘긴다. 이 모듈은 기본 디렉터리를 갖지 않는다.
"""
import json
import os
import pathlib
import re
import tempfile

SCHEMA_VERSION = 1

# trial_manifest.trial_id 가 VARCHAR(64) 다. 파일 이름으로 쓰이므로 경로 문자를 막는다.
_TRIAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _path(directory, trial_id):
    if not isinstance(trial_id, str) or not _TRIAL_ID.fullmatch(trial_id):
        raise ValueError(f"trial_id 가 허용 형식이 아니다: {trial_id!r}")
    return pathlib.Path(directory) / f"{trial_id}.json"


def save(directory, record):
    """record 에 schema_version 을 붙여 <trial_id>.json 으로 한 번만 쓰고 경로를 돌려준다.

    이미 파일이 있으면 FileExistsError 를 올리고 기존 파일은 그대로 둔다. JSON 으로 옮길 수
    없는 값(bytes 등)은 repr 문자열로 남긴다. 그 값 하나 때문에 trial 을 통째로 잃지 않기 위해서다.
    """
    if "schema_version" in record:
        raise ValueError("record 에 schema_version 키가 이미 있다")
    path = _path(directory, record.get("trial_id"))
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({**record, "schema_version": SCHEMA_VERSION},
                      indent=2, sort_keys=True, ensure_ascii=False, default=repr)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)  # 대상이 있으면 FileExistsError. 덮어쓰지 않는다.
    finally:
        os.unlink(tmp)
    return path


def load(directory, trial_id):
    """저장된 객체를 그대로 돌려준다. 파일이 없으면 None, 읽히지 않으면 예외를 올린다."""
    path = _path(directory, trial_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return json.loads(text)
