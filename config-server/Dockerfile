FROM python:3.10-slim

WORKDIR /app
COPY . .

RUN apt-get update && apt-get install -y --no-install-recommends \
    krb5-user \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt

# 노드 로컬 이미지 도우미 Pod(node_image_cli.py)가 이 이미지로 떠서 노드의 containerd에 명령을 내린다.
# 버전과 체크섬을 고정한다 — 노드의 containerd 소켓에 닿는 도구라 받는 파일이 바뀌면 안 된다.
ARG NERDCTL_VERSION=2.1.6
ARG NERDCTL_SHA256=22857b373edea479a4534ba62cae1c77f2af38f0aac4c91c1c68cf09e29d6f9e
RUN python - <<'PY'
import hashlib, io, os, tarfile, urllib.request
version, expected = os.environ["NERDCTL_VERSION"], os.environ["NERDCTL_SHA256"]
url = f"https://github.com/containerd/nerdctl/releases/download/v{version}/nerdctl-{version}-linux-amd64.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
actual = hashlib.sha256(data).hexdigest()
if actual != expected:
    raise SystemExit(f"nerdctl checksum mismatch: {actual}")
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
    member = archive.getmember("nerdctl")
    with open("/usr/local/bin/nerdctl", "wb") as out:
        out.write(archive.extractfile(member).read())
os.chmod("/usr/local/bin/nerdctl", 0o755)
PY

# --graceful-timeout: SIGTERM(롤링 업데이트 등) 시 진행 중인 요청을 마칠 때까지 기다리는
# 시간. 기본값 30초는 /create-pod가 pod ready를 최대 500초까지 기다리는 것보다 훨씬 짧아서,
# 배포할 때마다 진행 중인 Pod 생성이 중간에 끊겼다. deployment.yaml의
# terminationGracePeriodSeconds(630)보다는 작게 잡아 k8s가 SIGKILL하기 전에 gunicorn이
# 먼저 정상 종료할 기회를 준다.
CMD ["gunicorn", "--workers=4", "--bind=0.0.0.0:8000", "--timeout", "700", "--graceful-timeout", "600", "main:app"]

