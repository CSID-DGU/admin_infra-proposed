"""노드 로컬 이미지 도우미 — 일회용 Pod 안에서 그 노드의 containerd에 명령을 내린다.

config-server는 노드에 직접 들어갈 수 없으므로(adapters/node_image.py) 대상 노드에 이 스크립트를 실행하는
Pod를 띄우고, 마지막 줄의 `RESULT {...}` 한 줄만 읽는다. nerdctl의 경고·진행 출력은 형식이 버전마다
달라 결과로 쓰지 않는다. 표준 라이브러리만 쓴다 — 이 Pod에는 config-server 설정이 없다.
"""
import argparse
import json
import subprocess
import sys

# 구운 층에 남기는 표식. 이미지 이력의 comment로 남아, 기본 이미지 층과 사용자가 쌓은 층을 구분한다.
COMMIT_MARKER = "ailab-commit"
RESULT_PREFIX = "RESULT "


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


def commit(socket, container, ref, running):
    # 실행 중이 아닌 컨테이너는 멈출 작업이 없어 기본값(--pause)으로는 실패한다.
    done = _nerdctl(socket, "commit", f"--pause={'true' if running else 'false'}",
                    "--message", COMMIT_MARKER, container, ref)
    if done.returncode != 0:
        raise RuntimeError(f"commit failed: {done.stderr.strip()[-500:]}")
    return {"added_bytes": added_bytes(_inspect(socket, ref))}


def unpause(socket, container):
    """굽는 도중 도우미가 죽으면 컨테이너가 멈춘 채 남는다. 멈춰 있지 않으면 아무 일도 하지 않는다."""
    done = _nerdctl(socket, "unpause", container)
    return {"unpaused": done.returncode == 0}


def remove(socket, ref):
    done = _nerdctl(socket, "rmi", ref)
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
