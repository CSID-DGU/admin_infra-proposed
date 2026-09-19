"""가상 계층의 자원 실체를 읽어서 evaluator 에 넘길 검사 결과를 만드는 수집기.

여기서 읽는 것은 네 가지뿐이다. FakeV1 의 Pod 목록과 Secret 목록, 임시 폴더에 놓인 계정
대장 파일, 그리고 바깥 세계가 실제로 무엇을 당했는지 적힌 호출 기록이다. 홈 디렉터리 생성과
NodePort 배정은 가상 계층이 그 두 함수를 호출 기록용 대역으로 바꿔 두었기 때문에 조회 가능한
자원으로 남지 않고 호출 기록에만 남는다. 그래서 그 기록을 실스택의 NAS 와 방화벽에 해당하는
자원 실체로 취급한다.

이 대응은 가상 계층 전용이다. 실스택에서는 다섯 검사가 전부 harness/system.py 를 거친 실제
접근 시도로 바뀐다. 로그인은 실제 인증이 되고, 홈 읽기·쓰기는 실제 파일 조작이 되고, 컨테이너
신원과 저장소 접근은 Pod 안에서의 실행이 되고, endpoint 는 실제 연결 시도가 된다.

호출 기록을 볼 때에는 순서를 함께 본다. 생성 과정에도 남은 자원을 먼저 치우는 release 와
svc_delete 가 나오기 때문에, 호출 이름이 있다는 사실만으로 회수되었다고 읽으면 정상 생성을
회수로 오독한다.
"""
import sys

from evaluator import FAIL, PASS


def _e2e():
    """conftest 가 경로로 읽어 올려 둔 가상 계층 모듈. 베끼지 않고 그것을 그대로 쓴다."""
    return sys.modules["cs_e2e"]


def _verdict(ok):
    return PASS if ok else FAIL


def _last(e, name, match):
    """해당 호출의 마지막 위치. 없으면 -1 이다."""
    found = -1
    for i, (called, args) in enumerate(e.calls):
        if called != name:
            continue
        if match(args if isinstance(args, tuple) else (args,)):
            found = i
    return found


def _user_pods(e, username):
    """FakeV1 에 살아 있는 그 사용자의 Pod 들."""
    return [p for p in e.v1.pods.values() if p.metadata.labels.get("username") == username]


def _pod_names(e, username):
    """바깥 세계가 그 사용자 이름으로 받은 Pod 이름들. Pod 가 지워진 뒤에도 남는다."""
    return {a[2] for name, a in e.calls if name == "svc_create" and a[0] == username}


def virtual_collector(e):
    """가상 계층 상태 e 를 읽는 collect(check, username) 를 돌려준다."""

    def home_kept(username):
        created = _last(e, "create_home", lambda a: a[0] == username)
        deleted = _last(e, "delete_home", lambda a: a[0] == username)
        return created >= 0 and created > deleted, {"create_home_at": created, "delete_home_at": deleted}

    def endpoint_order(username):
        pods = _pod_names(e, username)
        allocated = _last(e, "allocate", lambda a: a[0] in pods)
        released = _last(e, "release", lambda a: a[0] in pods)
        return allocated, released, {"pods": sorted(pods), "allocate_at": allocated, "release_at": released}

    def collect(check, username):
        if check == "login":
            names = _e2e().passwd_names()
            return _verdict(username in names), {"account_ledger": username in names}
        if check == "login_blocked":
            names = _e2e().passwd_names()
            return _verdict(username not in names), {"account_ledger": username in names}
        if check == "home_read_write":
            kept, detail = home_kept(username)
            return _verdict(kept), detail
        if check == "container_identity":
            pods = _user_pods(e, username)
            return _verdict(bool(pods)), {"pods": [p.metadata.name for p in pods]}
        if check == "container_blocked":
            pods = _user_pods(e, username)
            return _verdict(not pods), {"pods": [p.metadata.name for p in pods]}
        if check == "storage_access":
            pods = _user_pods(e, username)
            mounts = [m["mountPath"] for p in pods
                      for c in p.body["spec"]["containers"] for m in c["volumeMounts"]]
            return _verdict("/home" in mounts), {"mount_paths": mounts}
        if check == "credential_blocked":
            removed = _last(e, "krb5_principal_delete", lambda a: a[0] == username)
            return _verdict(removed >= 0), {"principal_delete_at": removed}
        if check == "endpoint":
            allocated, released, detail = endpoint_order(username)
            return _verdict(allocated >= 0 and allocated > released), detail
        if check == "endpoint_blocked":
            allocated, released, detail = endpoint_order(username)
            return _verdict(released >= 0 and released > allocated), detail
        raise ValueError(f"unknown check: {check}")

    return collect
