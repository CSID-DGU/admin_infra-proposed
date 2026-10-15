"""노드 로컬 이미지 도우미 — 일회용 Pod 안에서 그 노드의 containerd와 cgroup에 명령을 내린다.

config-server는 노드에 직접 들어갈 수 없으므로(adapters/node_image.py) 대상 노드에 이 스크립트를 실행하는
Pod를 띄우고, 마지막 줄의 `RESULT {...}` 한 줄만 읽는다. nerdctl의 경고·진행 출력은 형식이 버전마다
달라 결과로 쓰지 않는다. 표준 라이브러리만 쓴다 — 이 Pod에는 config-server 설정이 없다.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

# 구운 층에 남기는 표식. 이미지 이력의 comment로 남아, 기본 이미지 층과 사용자가 쌓은 층을 구분한다.
COMMIT_MARKER = "ailab-commit"
RESULT_PREFIX = "RESULT "
# 노드의 /sys/fs/cgroup을 마운트하는 자리(adapters/node_image.py가 같은 값을 쓴다).
CGROUP_ROOT = "/host-cgroup"
# 실행 중인 컨테이너가 다 멈추기를 기다리는 기본 시간.
FREEZE_WAIT_SEC = 60
_FREEZE_POLL_SEC = 0.5
_THAW_WAIT_SEC = 5
# 이 결과 코드는 config-server가 그대로 작업 오류 코드로 쓴다.
LEFT_FROZEN_CODE = "IMAGE_LEFT_PAUSED"


class LeftFrozen(RuntimeError):
    """풀었는데도 컨테이너가 멈춰 있다. 사용자가 접속할 수 없다."""


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


def commit(socket, container, ref, running, freeze_wait_sec=FREEZE_WAIT_SEC, cgroup_root=CGROUP_ROOT):
    if not running:
        return _commit(socket, container, ref)
    # 멈춤·풀기는 containerd(nerdctl pause)를 거치지 않고 cgroup에 직접 한다. runc는 다 멈추기를 10초만 기다리고
    # 실패하면서 멈춤 요청은 남겨, containerd는 running·cgroup은 멈춤으로 어긋난다. 그러면 nerdctl unpause가
    # "멈춰 있지 않다"며 거부해 풀 길이 없다. cgroup에서 직접 하면 containerd는 내내 running이라 어긋날 일이 없다.
    directory = cgroup_dir(cgroup_root, container)
    frozen = False
    try:
        frozen = _freeze(directory, freeze_wait_sec)
        if frozen:
            result = _commit(socket, container, ref)
    finally:
        if not _thaw(directory):
            raise LeftFrozen("container still frozen after thaw")
    if not frozen:
        raise RuntimeError(f"container did not freeze within {freeze_wait_sec}s; thawed without committing")
    return result


def _commit(socket, container, ref):
    # 실행 중이었다면 이미 cgroup에서 멈춰 두었다. nerdctl이 다시 멈추려 하면(기본 --pause) runc 10초 제한에 걸린다.
    done = _nerdctl(socket, "commit", "--pause=false", "--message", COMMIT_MARKER, container, ref)
    if done.returncode != 0:
        raise RuntimeError(f"commit failed: {done.stderr.strip()[-500:]}")
    return {"added_bytes": added_bytes(_inspect(socket, ref))}


def cgroup_dir(root, container):
    """컨테이너의 cgroup v2 디렉터리. kubelet의 cgroup 드라이버와 Pod의 QoS 등급에 따라 경로가 다르다."""
    patterns = [
        f"{root}/kubepods/*/pod*/{container}",                              # cgroupfs, burstable·besteffort
        f"{root}/kubepods/pod*/{container}",                                # cgroupfs, guaranteed
        f"{root}/kubepods.slice/*/*/cri-containerd-{container}.scope",     # systemd, burstable·besteffort
        f"{root}/kubepods.slice/*/cri-containerd-{container}.scope",       # systemd, guaranteed
    ]
    found = sorted({path for pattern in patterns for path in glob.glob(pattern)})
    if len(found) != 1:
        raise RuntimeError(f"cgroup of container not found exactly once: {len(found)} matches")
    if not os.path.exists(os.path.join(found[0], "cgroup.freeze")):
        raise RuntimeError("cgroup v2 freezer not available")
    return found[0]


def _freeze(directory, wait_sec):
    """멈춤을 요청하고 다 멈추기를 wait_sec까지 기다린다. 다 멈췄으면 True."""
    _write(directory, "cgroup.freeze", "1")
    return _wait_frozen(directory, True, wait_sec)


def _thaw(directory):
    """멈춤을 거둔다. 풀기는 기다릴 것이 없어 바로 반영된다. 풀렸으면 True."""
    _write(directory, "cgroup.freeze", "0")
    return _wait_frozen(directory, False, _THAW_WAIT_SEC)


def _wait_frozen(directory, want, wait_sec):
    deadline = time.monotonic() + wait_sec
    while True:
        if _is_frozen(directory) == want:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_FREEZE_POLL_SEC)


def _is_frozen(directory):
    with open(os.path.join(directory, "cgroup.events")) as events:
        for line in events:
            key, _, value = line.partition(" ")
            if key == "frozen":
                return value.strip() == "1"
    raise RuntimeError("cgroup.events has no frozen line")


def _write(directory, name, value):
    with open(os.path.join(directory, name), "w") as f:
        f.write(value)


def thaw(container, cgroup_root=CGROUP_ROOT):
    """굽던 도우미가 끝을 맺지 못했을 때(시간 초과·강제 종료) 컨테이너가 멈춘 채 남지 않게 따로 푼다.
    멈춰 있지 않으면 아무 일도 하지 않는다."""
    return {"frozen": not _thaw(cgroup_dir(cgroup_root, container))}


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
    commit_args.add_argument("--freeze-wait-sec", type=int, default=FREEZE_WAIT_SEC)
    thaw_args = commands.add_parser("thaw")
    thaw_args.add_argument("--container", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "commit":
            result = commit(args.socket, args.container, args.ref, args.running, args.freeze_wait_sec, CGROUP_ROOT)
        elif args.command == "thaw":
            result = thaw(args.container, CGROUP_ROOT)
        else:
            result = remove(args.socket, args.ref)
    except LeftFrozen as e:
        print(RESULT_PREFIX + json.dumps({"error": str(e), "code": LEFT_FROZEN_CODE}))
        return 1
    except Exception as e:
        print(RESULT_PREFIX + json.dumps({"error": str(e)}))
        return 1
    print(RESULT_PREFIX + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
