import importlib.util
import json
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cluster_profile = _load("cluster_profile")


def test_legacy_farm_profile_is_converted_to_cluster_profile():
    values = {
        "farm": {
            "ssh": {"user": "legacy-node", "keyPath": "/old/node", "nodes": [{"name": "farm1"}]},
            "adSsh": {"user": "legacy-ad", "keyPath": "/old/ad", "nodes": [{"name": "ad1"}]},
        },
        "krb5": {"realm": "FARM.EXAMPLE", "kdcHost": "192.0.2.10"},
    }

    converted = cluster_profile.cluster_profile(values)

    assert converted["cluster"] == {
        "id": "farm",
        "homeMountRootTemplate": "",
        "ssh": {"user": "legacy-node", "keyPath": "/old/node", "nodes": [{"name": "farm1"}]},
        "adSsh": {"user": "legacy-ad", "keyPath": "/old/ad", "nodes": [{"name": "ad1"}]},
    }
    assert converted["krb5"]["kdcHosts"] == ["192.0.2.10"]
    assert converted["krb5"]["adminServer"] == "192.0.2.10"


def test_current_cluster_profile_wins_over_legacy_farm_values():
    values = {
        "cluster": {
            "homeMountRootTemplate": "/home/{node}",
            "ssh": {"user": "current", "keyPath": "/new/key", "nodes": [{"name": "lab1"}]},
            "adSsh": {"user": "current-ad", "keyPath": "/new/ad", "nodes": [{"name": "lab-ad1"}]},
        },
        "farm": {
            "ssh": {"user": "legacy", "keyPath": "/old/key", "nodes": [{"name": "farm1"}]},
            "adSsh": {"user": "legacy-ad", "keyPath": "/old/ad", "nodes": [{"name": "farm-ad1"}]},
        },
    }

    converted = cluster_profile.cluster_profile(values, cluster_id="lab")

    assert converted["cluster"]["id"] == "lab"
    assert converted["cluster"]["homeMountRootTemplate"] == "/home/{node}"
    assert converted["cluster"]["ssh"]["user"] == "current"
    assert converted["cluster"]["ssh"]["nodes"] == [{"name": "lab1"}]
    assert converted["cluster"]["adSsh"]["user"] == "current-ad"


def test_management_placement_overrides_legacy_master_node_selector():
    converted = cluster_profile.cluster_profile({
        "nodeSelector": {"kubernetes.io/hostname": "csid-dgu-desktop"},
        "tolerations": [],
    })

    assert converted["nodeSelector"] == {
        "provi.dgu.ac.kr/role": "farm-management",
    }
    assert converted["tolerations"] == [{
        "key": "node-role.kubernetes.io/control-plane",
        "operator": "Exists",
        "effect": "NoSchedule",
    }]


def test_lab_management_profile_uses_lab_role():
    converted = cluster_profile.cluster_profile({}, cluster_id="lab")

    assert converted["nodeSelector"] == {
        "provi.dgu.ac.kr/role": "lab-management",
    }


def test_management_overlay_wins_in_actual_helm_render():
    deploy_values = cluster_profile.deployment_values({
        "cluster": {"id": "farm"},
        "image": {"tag": "test"},
        "nodeSelector": {"kubernetes.io/hostname": "csid-dgu-desktop"},
        "controller": {"enabled": True},
    })

    rendered = subprocess.run(
        [
            "helm", "template", "config-server-operation",
            str(ROOT.parents[1] / "config-server" / "Chart"),
            "-f", "-",
        ],
        input=json.dumps(deploy_values),
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "kubernetes.io/hostname: csid-dgu-desktop" not in rendered
    assert rendered.count("provi.dgu.ac.kr/role: farm-management") >= 3


def test_deployment_values_preserve_dependencies_but_replace_placement_map():
    converted = cluster_profile.deployment_values({
        "nfs": {"server": "nas.example", "kubeSharePath": "/share"},
        "nodeSelector": {"kubernetes.io/hostname": "csid-dgu-desktop"},
        "tolerations": [],
    })

    assert converted["nfs"] == {
        "server": "nas.example",
        "kubeSharePath": "/share",
    }
    assert converted["nodeSelector"] == {
        "provi.dgu.ac.kr/role": "farm-management",
    }
    assert converted["tolerations"][0]["key"] == "node-role.kubernetes.io/control-plane"


def test_current_kdc_hosts_win_and_legacy_kdc_host_is_only_a_fallback():
    current = cluster_profile.cluster_profile({
        "krb5": {"kdcHosts": ["198.51.100.10", "198.51.100.11"], "kdcHost": "192.0.2.10"}
    })
    legacy = cluster_profile.cluster_profile({"krb5": {"kdcHost": "192.0.2.10"}})

    assert current["krb5"]["kdcHosts"] == ["198.51.100.10", "198.51.100.11"]
    assert current["krb5"]["adminServer"] == "198.51.100.10"
    assert legacy["krb5"]["kdcHosts"] == ["192.0.2.10"]


def test_stack_up_uses_cluster_registry_contract_and_release_service_name():
    script = (ROOT / "stack-up.sh").read_text()

    assert '"provi":{"clusters":{"FARM"' in script
    assert '"in-cluster":true' in script
    assert '"base-url":"http://$CONFIG_SERVICE.$NS.svc.cluster.local"' in script
    assert '"config":{"base-url"' not in script
    assert '"kubernetes":{"pod-namespace"' not in script
    assert 'API_SELECTOR_MODE=$(selector_mode_of "$RELEASE" containerssh-config-server api)' in script
    assert 'SELECTOR_MODE=${API_SELECTOR_MODE:-${CONTROLLER_SELECTOR_MODE:-release}}' in script
    assert '--set compatibility.deploymentSelectorMode="$SELECTOR_MODE"' in script
    assert '[ "$SELECTOR_MODE" = legacy ] && CONFIG_SERVICE=containerssh-config-service' in script
    assert script.count('api.headers["X-Cluster-Id"]') == 2


def test_ledger_helper_uses_farm_management_role_instead_of_old_master():
    script = (ROOT / "stack-up.sh").read_text()
    helper = script.split("start_ledger_helper()", 1)[1].split("wait_ledger_helper()", 1)[0]

    assert "nodeName: csid-dgu-desktop" not in script
    assert "provi.dgu.ac.kr/role: farm-management" in helper
    assert "key: node-role.kubernetes.io/control-plane" in helper
    assert "operator: Exists" in helper
    assert "effect: NoSchedule" in helper


def test_stack_down_sends_canonical_cluster_identity():
    script = (ROOT / "stack-down.sh").read_text()

    assert 'api.headers["X-Cluster-Id"] = os.environ.get("CLUSTER_ID", "").strip().upper()' in script


def test_generic_stack_down_explicitly_rejects_operation():
    result = subprocess.run(
        ["bash", str(ROOT / "stack-down.sh"), "operation"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "빈 계정 접두어가 모든 운영 계정을 대상으로" in result.stderr
