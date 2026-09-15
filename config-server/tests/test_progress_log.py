"""진행 상황 변화는 제어기가 작업을 실행하는 동안 작업 이력에도 남는다(단계가 바뀔 때만)."""
import main


def test_progress_rows_only_inside_job_and_only_on_change(logs, monkeypatch):
    stored = []
    monkeypatch.setattr(main, "_store_pod_status", lambda *a: stored.append(a))
    main._last_progress.clear()

    main.set_pod_creation_status("9", "pulling_image", "이미지 다운로드 중")  # 작업 밖: Redis만
    assert logs == [] and len(stored) == 1

    job, user = main.current_job_id.set(77), main.current_username.set("exp-np-001")
    try:
        main.set_pod_creation_status("9", "pulling_image", "이미지 다운로드 중")
        main.set_pod_creation_status("9", "pulling_image", "이미지 다운로드 중")
        main.set_pod_creation_status("9", "starting_container", "컨테이너 시작 중")
    finally:
        main.current_username.reset(user)
        main.current_job_id.reset(job)

    assert [r["resource_type"] for r in logs] == ["pulling_image", "starting_container"]
    assert all(r["action"] == main.Action.PROGRESS and r["phase"] == main.Phase.INFO for r in logs)
    assert len(stored) == 4


def test_progress_summary_is_exposed_in_steps():
    detail = '{"stage": "pulling_image", "message": "이미지 다운로드 중"}'
    assert main._step_summary(detail) == {"stage": "pulling_image", "message": "이미지 다운로드 중"}
