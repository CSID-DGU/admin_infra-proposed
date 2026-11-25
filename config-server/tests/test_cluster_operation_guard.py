import pytest

import main


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/operations/password", {"request_id": 1, "username": "alice", "passwd_hash": "$6$saltsalt$" + "a" * 86}),
        ("/operations/home", {"request_id": 2, "username": "alice", "expected_uid": 20001}),
        ("/operations/group", {"request_id": 3, "op": "create", "name": "researchers", "members": []}),
    ],
)
def test_account_global_operations_are_rejected_by_lab_config_server(api, monkeypatch, path, body):
    monkeypatch.setattr(main, "CLUSTER_ID", "LAB")

    response = api.post(path, json=body)

    assert response.status_code == 409
    assert response.get_json()["error"] == "CLUSTER_OPERATION_UNSUPPORTED"


def test_access_remains_cluster_local_and_is_not_blocked_on_lab(api, monkeypatch):
    monkeypatch.setattr(main, "CLUSTER_ID", "LAB")
    monkeypatch.setattr(main, "_register_job", lambda *args: ({"accepted": args[0]}, 202))

    response = api.post(
        "/operations/access",
        json={"request_id": 4, "username": "alice", "blocked": True},
    )

    assert response.status_code == 202
    assert response.get_json() == {"accepted": "access"}
