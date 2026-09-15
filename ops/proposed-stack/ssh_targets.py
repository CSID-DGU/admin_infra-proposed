"""운영 config-server Helm 값(JSON, 표준 입력)에서 config-server가 SSH로 접속하는 호스트(host port)를 한 줄씩 출력한다.

stack-up.sh가 호스트 키를 모을 때 쓴다. 주소는 공개 로그에 찍지 않도록 호출 쪽에서 출력하지 않는다.
"""
import json
import sys


def targets(values):
    out = set()
    nas = (values.get("nas") or {}).get("ssh") or {}
    if nas.get("host"):
        out.add((str(nas["host"]), str(nas.get("port") or 22)))
    farm = values.get("farm") or {}
    for group in ("ssh", "adSsh"):
        for node in (farm.get(group) or {}).get("nodes") or []:
            if node.get("host"):
                out.add((str(node["host"]), str(node.get("port") or 22)))
    return sorted(out)


if __name__ == "__main__":
    # helm get values -o json 출력을 표준 입력으로 받는다(배포 서버에 PyYAML이 없을 수 있음).
    for host, port in targets(json.load(sys.stdin) or {}):
        print(host, port)
