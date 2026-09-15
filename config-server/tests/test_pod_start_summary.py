"""컨테이너 준비 대기 요약: 이미지를 새로 받았는지·걸린 시간·재시도 횟수만 남기고 원문은 버린다."""
import types

import main
import utils


def _event(reason, message, count=1):
    return types.SimpleNamespace(reason=reason, message=message, count=count)


class _V1:
    def __init__(self, events=None, fail=False):
        self.events, self.fail = events or [], fail

    def list_namespaced_event(self, namespace, field_selector):
        if self.fail:
            raise RuntimeError("api down")
        return types.SimpleNamespace(items=self.events)


def test_go_duration():
    assert utils._go_duration_seconds("3m34.801s") == 214.8
    assert utils._go_duration_seconds("4.2s") == 4.2
    assert utils._go_duration_seconds("1500ms") == 1.5
    assert utils._go_duration_seconds("1h2m3s") == 3723.0
    assert utils._go_duration_seconds("") is None


def test_pulled_image_with_time_and_size():
    v1 = _V1([
        _event("Pulling", 'Pulling image "dguailab/decs:cuda12.8-tf2.20-ubuntu22.04-260915"'),
        _event("Pulled", 'Successfully pulled image "dguailab/decs:x" in 3m34.801s (3m34.801s including waiting). '
                         "Image size: 7354398010 bytes."),
        _event("FailedMount", "MountVolume.SetUp failed for volume \"home\" : mount failed 10.0.0.1:/x", count=2),
    ])
    with main.app.app_context():
        assert utils.summarize_pod_start_events(v1, "ns", "p") == {
            "image_source": "pulled", "image_pull_seconds": 214.8, "image_size_mb": 7014, "mount_retries": 2}


def test_cached_image_and_restarts():
    v1 = _V1([_event("Pulled", 'Container image "dguailab/decs:x" already present on machine'),
              _event("BackOff", "Back-off restarting failed container", count=3)])
    with main.app.app_context():
        assert utils.summarize_pod_start_events(v1, "ns", "p") == {"image_source": "cached", "restarts": 3}


def test_event_failure_gives_empty_summary():
    with main.app.app_context():
        assert utils.summarize_pod_start_events(_V1(fail=True), "ns", "p") == {}


def test_step_summary_exposes_image_keys_only():
    detail = '{"image_source": "pulled", "image_pull_seconds": 12.5, "image_size_mb": 700, "message": "10.0.0.1:/x"}'
    assert main._step_summary(detail) == {"image_source": "pulled", "image_pull_seconds": 12.5, "image_size_mb": 700}
