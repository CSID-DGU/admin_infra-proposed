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

from main import app, find_unfinished_jobs, run_job
import job_control

POLL_SEC = float(os.getenv("CONTROLLER_POLL_SEC", "2"))
# API 서버(gunicorn --workers=4)와 같은 동시 처리 수. baseline과 동시성 조건을 맞춘다.
WORKERS = int(os.getenv("CONTROLLER_WORKERS", "4"))

_stop = threading.Event()


def _run(kind, request_id, username, job_id):
    with app.app_context():
        try:
            run_job(kind, request_id, username, job_id)
        except Exception:
            app.logger.exception(f"[CONTROLLER] job crashed {kind} request_id={request_id}")


def main():
    # SIGTERM(배포·Pod 삭제)을 받으면 새 작업을 더 받지 않고, 실행 중인 작업이 끝날 때까지 기다린다.
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())

    # 이전 제어기가 실행하던 작업은 별도 처리 없이, lease가 만료되면 run_job이 인수해
    # 저널에 남은 단계부터 이어간다(v2.1).
    app.logger.info(f"[CONTROLLER] started poll={POLL_SEC}s workers={WORKERS} owner={job_control.OWNER}")

    running = {}  # (kind, request_id) -> job_id
    lock = threading.Lock()

    def _done(key):
        with lock:
            running.pop(key, None)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while not _stop.is_set():
            try:
                with app.app_context():
                    jobs = find_unfinished_jobs()
            except Exception:
                app.logger.exception("[CONTROLLER] job lookup failed")
                jobs = []
            for kind, request_id, username, job_id in jobs:
                key = (kind, request_id)
                with lock:
                    if key in running or len(running) >= WORKERS:
                        continue
                    running[key] = job_id
                future = pool.submit(_run, kind, request_id, username, job_id)
                future.add_done_callback(lambda _f, k=key: _done(k))
            # 실행 중 작업의 lease 갱신. 갱신이 끊기면(프로세스 사망) 다른 제어기가 인수한다.
            with lock:
                held = [j for j in running.values() if j is not None]
            if held:
                try:
                    with app.app_context():
                        job_control.renew(held)
                except Exception:
                    app.logger.exception("[CONTROLLER] lease renew failed")
            _stop.wait(POLL_SEC)
    app.logger.info("[CONTROLLER] stopped")


if __name__ == "__main__":
    main()
