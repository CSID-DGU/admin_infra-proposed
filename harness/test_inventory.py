"""inventory 계약 검증. system.stack_kube 와 stack_sql 을 가짜 스택으로 바꾼다."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inventory  # noqa: E402
import system  # noqa: E402

HOST, NS, PREFIX = "farm1", "ailab-full", "exp-fu-"
ME, OTHER, E2E = "exp-fu-m0007", "exp-fu-001", "exp-fu-e2e1a2b"


def _obj(name, **labels):
    return {"metadata": {"name": name, "labels": labels}}


class FakeStack:
    def __init__(self):
        self.kube_fail = set()
        self.statements = []
        self.objects = {
            "pods": [_obj(f"ailab-{ME}-0a1b2c3d", app="ailab-guest", username=ME),
                     _obj(f"ailab-{OTHER}-11111111", app="ailab-guest", username=OTHER),
                     _obj(f"ailab-{E2E}-22222222", app="ailab-guest", username=E2E)],
            "services": [_obj(f"{ME}-ssh", app="ailab-nodeport", username=ME),
                         _obj(f"{E2E}-ssh", app="ailab-nodeport", username=E2E)],
            "secrets": [_obj(f"account-{ME}", app="ailab-account", username=ME),
                        _obj(f"krb5-keytab-{ME}"),
                        _obj(f"krb5-keytab-{OTHER}"),
                        _obj("stack-db")],
        }
        self.tables = {
            "nodeport_allocations": [(ME, 32251), (OTHER, 32252), (E2E, 32253)],
            "krb5_cleanup_pending": [(ME, "farm6")],
        }

    def kube(self, host, namespace, args, *, stdin=None):
        assert (host, namespace) == (HOST, NS) and args[0] == "get"
        resource = args[1]
        if resource in self.kube_fail:
            return {"rc": 1, "stdout": "", "stderr": "connection refused"}
        items = self.objects[resource]
        if "-l" in args:
            key, value = args[args.index("-l") + 1].split("=")
            items = [i for i in items if i["metadata"]["labels"].get(key) == value]
        return {"rc": 0, "stdout": json.dumps({"items": items}), "stderr": ""}

    def sql(self, host, namespace, database, statement):
        self.statements.append(statement)
        assert database == "pod_port_db"
        table = statement.split("FROM")[1].split()[0]
        return {"rc": 0, "columns": ["username", "x"], "rows": [list(r) for r in self.tables[table]],
                "stderr": ""}


@pytest.fixture
def stack(monkeypatch):
    s = FakeStack()
    monkeypatch.setattr(system, "stack_kube", s.kube)
    monkeypatch.setattr(system, "stack_sql", s.sql)
    return s


def test_resources_grouped_by_user(stack):
    inv = inventory.collect_inventory(HOST, NS, PREFIX)
    assert inv["errors"] == {}
    assert inv["by_user"] == {ME: {
        "pod": [f"ailab-{ME}-0a1b2c3d"],
        "service": [f"{ME}-ssh"],
        "account_secret": [f"account-{ME}"],
        "keytab_secret": [f"krb5-keytab-{ME}"],
        "nodeport_allocation": ["32251"],
        "krb5_cleanup_pending": ["farm6"],
    }}


def test_out_of_scope_and_e2e_users_excluded_e2e_counted(stack):
    inv = inventory.collect_inventory(HOST, NS, PREFIX)
    assert set(inv["by_user"]) == {ME}
    assert inv["e2e_users"] == 1


def test_failed_query_is_error_not_empty_list(stack):
    stack.kube_fail.add("services")
    inv = inventory.collect_inventory(HOST, NS, PREFIX)
    assert "service" in inv["errors"]
    assert all("service" not in kinds for kinds in inv["by_user"].values())
    assert inv["by_user"][ME]["pod"]


def test_call_failure_is_error(stack, monkeypatch):
    def boom(*a, **k):
        raise system.SystemCallFailed("server-state 없음")
    monkeypatch.setattr(system, "stack_sql", boom)
    inv = inventory.collect_inventory(HOST, NS, PREFIX)
    assert set(inv["errors"]) == {"nodeport_allocation", "krb5_cleanup_pending"}


def test_requests_table_never_queried(stack):
    inventory.collect_inventory(HOST, NS, PREFIX)
    assert stack.statements
    assert not any("requests" in s for s in stack.statements)
