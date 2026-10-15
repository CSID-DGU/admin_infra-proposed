"""노드 로컬 이미지 도우미 — 일회용 Pod 안에서 그 노드의 containerd에 명령을 내린다.

config-server는 노드에 직접 들어갈 수 없으므로(adapters/node_image.py) 대상 노드에 이 스크립트를 실행하는
Pod를 띄우고, 마지막 줄의 `RESULT {...}` 한 줄만 읽는다. nerdctl의 경고·진행 출력은 형식이 버전마다
달라 결과로 쓰지 않는다. 표준 라이브러리만 쓴다 — 이 Pod에는 config-server 설정이 없다.
"""
import argparse
import json
import subprocess
import sys
import time

# 구운 층에 남기는 표식. 이미지 이력의 comment로 남아, 기본 이미지 층과 사용자가 쌓은 층을 구분한다.
COMMIT_MARKER = "ailab-commit"
RESULT_PREFIX = "RESULT "
# 실행 중인 컨테이너가 다 멈추기를 기다리는 시간. runc는 10초만 기다리고(고정값) 실패를 돌려준다.
PAUSE_WAIT_SEC = 60
_PAUSE_POLL_SEC = 2
# 풀었는데도 이 상태면 멈춘 채로 본다. 상태를 읽지 못한 것(None)도 멈추는 중이라 읽지 못한 것이다.
_NOT_RUNNING_AGAIN = (None, "paused", "pausing")


def _nerdctl(socket, *args):
    return subprocess.run(["nerdctl", "--address", socket, "--namespace", "k8s.io", *args],
                          capture_output=True, text=True)


def added_bytes(inspected):
    """이미지 맨 위에 연속으로 쌓인 구운 층의 크기 합(압축 기준). 재생성을 반복하면 층이 하나씩 늘어난다."""
    layers = inspected["Manifest"]["layers"]
    history = [h for h in (inspected["ImageConfig"].get("history") or []) if not h.get("empty_layer")]
    total = 0
    for layer, entry in zip(reversed(layers), reversed(history)):
        if entry.get("comment") != COMMIT_MARKER:
            break
        total += int(layer["size"])
    return total


def _inspect(socket, ref):
    done = _nerdctl(socket, "image", "inspect", "--mode", "native", "--format", "{{json .}}", ref)
    if done.returncode != 0:
        raise RuntimeError(f"inspect failed: {done.stderr.strip()[-500:]}")
    # 플랫폼별로 한 줄씩 나온다 — 노드에 있는 것은 하나라 첫 줄을 쓴다.
    return json.loads(done.stdout.strip().splitlines()[0])


def commit(socket, container, ref, running, pause_wait_sec=PAUSE_WAIT_SEC):
    if running:
        # 멈추기는 직접 하고(nerdctl commit의 기본 --pause는 runc의 10초를 넘기면 바로 실패한다) 굽기는 멈춘 상태로 한다.
        _pause(socket, container, pause_wait_sec)
        try:
            result = _commit(socket, container, ref)
        finally:
            _nerdctl(socket, "unpause", container)
        if _status(socket, container) in _NOT_RUNNING_AGAIN:
            raise RuntimeError("committed but the container is still paused")
        return result
    return _commit(socket, container, ref)


def _commit(socket, container, ref):
    done = _nerdctl(socket, "commit", "--pause=false", "--message", COMMIT_MARKER, container, ref)
    if done.returncode != 0:
        raise RuntimeError(f"commit failed: {done.stderr.strip()[-500:]}")
    return {"added_bytes": added_bytes(_inspect(socket, ref))}


def _pause(socket, container, wait_sec):
    """runc는 cgroup이 다 멈추기를 10초만 기다린 뒤 실패를 돌려주지만 멈춤 요청(cgroup.freeze=1)은 거두지 않아
    커널은 계속 멈추는 중이다. 그래서 실패해도 wait_sec까지 상태를 다시 읽어 다 멈췄으면 이어 간다. 끝내 다 멈추지
    않으면 실패로 끝낸다 — 풀기와 그 확인은 호출하는 쪽(adapters/node_image.py)이 맡는다."""
    deadline = time.monotonic() + wait_sec
    done = _nerdctl(socket, "pause", container)
    if done.returncode == 0:
        return
    while time.monotonic() < deadline:
        if _status(socket, container) == "paused":
            return
        time.sleep(_PAUSE_POLL_SEC)
    raise RuntimeError(f"pause failed within {wait_sec}s: {done.stderr.strip()[-500:]}")


def _status(socket, container):
    """컨테이너 상태 문자열. 멈추는 중에는 runc도 상태를 읽지 못해(10초 대기 후 실패) None이다."""
    done = _nerdctl(socket, "inspect", "--format", "{{.State.Status}}", container)
    return done.stdout.strip() if done.returncode == 0 else None


def unpause(socket, container):
    """굽기가 실패하면 컨테이너가 멈춘 채 남을 수 있다. 풀어 본 뒤 실제 상태를 다시 읽어 돌려준다 — unpause의
    종료 코드만으로는 '멈춰 있지 않았음'과 '풀지 못함'을 구분할 수 없다. 상태를 읽지 못하면 멈춘 것으로 본다."""
    _nerdctl(socket, "unpause", container)
    return {"paused": _status(socket, container) in _NOT_RUNNING_AGAIN}


def remove(socket, ref):
    done = _nerdctl(socket, "rmi", ref)
    if done.returncode != 0 and "must be forced" in done.stderr:
        # Pod를 지운 직후에는 멈춘 컨테이너가 노드에 잠깐 남아 이미지를 붙잡는다. 멈춘 컨테이너뿐이면 강제로
        # 지운다 — 실행 중인 컨테이너가 쓰는 이미지는 강제로도 지워지지 않는다("cannot be forced").
        done = _nerdctl(socket, "rmi", "--force", ref)
    if done.returncode == 0:
        return {"removed": True}
    if "no such image" in done.stderr:
        return {"removed": False}
    raise RuntimeError(f"rmi failed: {done.stderr.strip()[-500:]}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commit_args = commands.add_parser("commit")
    commit_args.add_argument("--container", required=True)
    commit_args.add_argument("--ref", required=True)
    commit_args.add_argument("--running", action="store_true")
    remove_args = commands.add_parser("remove")
    remove_args.add_argument("--ref", required=True)
    unpause_args = commands.add_parser("unpause")
    unpause_args.add_argument("--container", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "commit":
            result = commit(args.socket, args.container, args.ref, args.running)
        elif args.command == "unpause":
            result = unpause(args.socket, args.container)
        else:
            result = remove(args.socket, args.ref)
    except Exception as e:
        print(RESULT_PREFIX + json.dumps({"error": str(e)}))
        return 1
    print(RESULT_PREFIX + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
