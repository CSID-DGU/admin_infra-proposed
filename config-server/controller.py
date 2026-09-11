"""
v2.0 제어기

승인 API(/operations/provision, /operations/revoke)가 등록만 해 둔 작업을 찾아 단계별로 실행한다.
config-server와 같은 이미지로 별도 Deployment(replicas 1, Recreate)에서 `python controller.py`로 돈다.
작업을 찾는 근거는 operation_log의 작업 단위 행이고, 실행은 main.py의 run_job이 한다.
"""
import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor

from main import app, find_unfinished_jobs, mark_interrupted_jobs, run_job

POLL_SEC = float(os.getenv("CONTROLLER_POLL_SEC", "2"))
# API 서버(gunicorn --workers=4)와 같은 동시 처리 수. baseline과 동시성 조건을 맞춘다.
WORKERS = int(os.getenv("CONTROLLER_WORKERS", "4"))

_stop = threading.Event()


def _run(kind, request_id, username):
    with app.app_context():
        try:
            run_job(kind, request_id, username)
        except Exception:
            app.logger.exception(f"[CONTROLLER] job crashed {kind} request_id={request_id}")


def main():
    # SIGTERM(배포·Pod 삭제)을 받으면 새 작업을 더 받지 않고, 실행 중인 작업이 끝날 때까지 기다린다.
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())

    with app.app_context():
        mark_interrupted_jobs()
    app.logger.info(f"[CONTROLLER] started poll={POLL_SEC}s workers={WORKERS}")

    running = set()
    lock = threading.Lock()

    def _done(key):
        with lock:
            running.discard(key)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while not _stop.is_set():
            try:
                with app.app_context():
                    jobs = find_unfinished_jobs()
            except Exception:
                app.logger.exception("[CONTROLLER] job lookup failed")
                jobs = []
            for kind, request_id, username in jobs:
                key = (kind, request_id)
                with lock:
                    if key in running or len(running) >= WORKERS:
                        continue
                    running.add(key)
                future = pool.submit(_run, kind, request_id, username)
                future.add_done_callback(lambda _f, k=key: _done(k))
            _stop.wait(POLL_SEC)
    app.logger.info("[CONTROLLER] stopped")


if __name__ == "__main__":
    main()
