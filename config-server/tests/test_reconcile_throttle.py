"""이슈 #57 — reconcile_nodeport_allocations 의 쓰로틀 상태 참조 회귀.

thin-layout 분리 때 _last_reconcile_ts 를 자기 모듈 global 로 남겨 NameError 가 났고,
NodePort 예약이 전면 실패해 신규 Pod 생성이 막혔다. 상태는 main 모듈이 소유한다.
"""
import time

import main
from lifecycle_steps import provision


def test_reconcile_does_not_raise_name_error(monkeypatch):
    """첫 호출부터 NameError 없이 돌아야 한다. 쓰로틀 창을 크게 잡아 k8s 호출 없이 스킵 경로만 탄다."""
    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_SEC", 10_000)
    monkeypatch.setattr(main, "_last_reconcile_ts", time.time())  # 방금 실행한 것처럼
    with main.app.app_context():
        assert provision.reconcile_nodeport_allocations(namespace="ailab-full") == 0


def test_reconcile_updates_shared_state_on_main(monkeypatch):
    """쓰로틀 시각을 자기 모듈이 아니라 main 에 기록해야 다음 호출이 스킵된다."""
    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_SEC", 10_000)
    monkeypatch.setattr(main, "_last_reconcile_ts", 0.0)      # 오래전 → 이번엔 실행 경로
    calls = {"n": 0}
    def fake_load_k8s(): calls["n"] += 1; raise RuntimeError("stop before k8s")
    monkeypatch.setattr(main, "load_k8s", fake_load_k8s)
    with main.app.app_context():
        # 실행 경로 진입 → _main._last_reconcile_ts 를 갱신한 뒤 load_k8s 에서 멈춘다
        try:
            provision.reconcile_nodeport_allocations(namespace="ailab-full")
        except RuntimeError:
            pass
    assert calls["n"] == 1
    assert main._last_reconcile_ts > 0.0        # main 의 상태가 갱신됐다
    # provision 모듈에는 그 이름이 없어야 한다 (자기 모듈 global 이 아님)
    assert not hasattr(provision, "_last_reconcile_ts")
