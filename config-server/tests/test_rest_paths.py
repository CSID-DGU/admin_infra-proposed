"""경로 정리: 자원 경로와 메서드, 옛 경로 제거, 오류 본문 형식 통일."""
import pytest


@pytest.mark.parametrize("method,path", [
    ("post", "/delete-pod"), ("post", "/migrate"), ("put", "/accounts/groups"), ("put", "/accounts/users/u/groups"),
])
def test_old_paths_are_gone(api, method, path):
    assert getattr(api, method)(path, json={}).status_code in (404, 405)


def test_invalid_pod_name_on_delete_is_400_with_infra_error(api):
    r = api.delete("/pods/not-a-user-pod")
    assert r.status_code == 400 and r.get_json()["error"] == "INVALID_REQUEST"


def test_unknown_job_kind_uses_infra_error(api):
    r = api.get("/operations/nope/1")
    assert r.status_code == 404 and r.get_json()["error"] == "UNKNOWN_JOB_KIND"
