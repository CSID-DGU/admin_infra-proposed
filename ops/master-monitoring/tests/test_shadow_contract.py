from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
VALUES = yaml.safe_load((ROOT / "values-shadow.yaml").read_text())
SOURCE_LOCK = yaml.safe_load((ROOT / "chart-source.lock.yaml").read_text())
MASTER_SELECTOR = {"provi.dgu.ac.kr/role": "master-management"}


class MasterMonitoringShadowContractTests(unittest.TestCase):
    def test_chart_source_is_immutable(self):
        self.assertEqual(SOURCE_LOCK["spec"]["version"], "80.2.2")
        self.assertEqual(
            SOURCE_LOCK["spec"]["repository"],
            "oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack",
        )
        self.assertEqual(
            SOURCE_LOCK["spec"]["ociDigest"],
            "sha256:552cf360469ef8275297ec9c88a2042847bcc6b9b3be8849d42eda2201bc34ae",
        )
        self.assertEqual(
            SOURCE_LOCK["spec"]["archiveSha256"],
            "33896f603cdbbcaad03ee618cda2da61ce6549b469582682a85d715ae4aea1d3",
        )

    def test_namespace_release_mode_and_all_schedulers_are_master_only(self):
        self.assertEqual(VALUES["namespaceOverride"], "provi-monitoring")
        self.assertEqual(
            VALUES["commonLabels"]["provi.dgu.ac.kr/cluster"], "provi-master"
        )
        selectors = (
            VALUES["alertmanager"]["alertmanagerSpec"]["nodeSelector"],
            VALUES["grafana"]["nodeSelector"],
            VALUES["kube-state-metrics"]["nodeSelector"],
            VALUES["prometheus-node-exporter"]["nodeSelector"],
            VALUES["prometheusOperator"]["nodeSelector"],
            VALUES["prometheusOperator"]["admissionWebhooks"]["deployment"][
                "nodeSelector"
            ],
            VALUES["prometheusOperator"]["admissionWebhooks"]["patch"][
                "nodeSelector"
            ],
            VALUES["prometheus"]["prometheusSpec"]["nodeSelector"],
        )
        self.assertTrue(all(selector == MASTER_SELECTOR for selector in selectors))

    def test_no_public_route_or_external_notification_receiver_exists(self):
        for component in ("alertmanager", "grafana", "prometheus"):
            self.assertFalse(VALUES[component]["ingress"]["enabled"])
            self.assertFalse(VALUES[component]["route"]["main"]["enabled"])
            self.assertEqual(VALUES[component]["service"]["type"], "ClusterIP")

        config = VALUES["alertmanager"]["config"]
        self.assertEqual(config["route"]["receiver"], "null")
        self.assertEqual(config["receivers"], [{"name": "null"}])
        forbidden_receiver_keys = {"slack_configs", "email_configs", "webhook_configs"}
        receiver_keys = {key for receiver in config["receivers"] for key in receiver}
        self.assertTrue(forbidden_receiver_keys.isdisjoint(receiver_keys))

    def test_shadow_has_no_remote_cluster_data_path(self):
        spec = VALUES["prometheus"]["prometheusSpec"]
        for key in (
            "remoteRead",
            "remoteWrite",
            "additionalRemoteRead",
            "additionalRemoteWrite",
            "additionalScrapeConfigs",
        ):
            self.assertEqual(spec[key], [])
        self.assertFalse(spec["enableRemoteWriteReceiver"])
        self.assertEqual(spec["externalLabels"], {"cluster": "provi-master"})
        serialized = yaml.safe_dump(VALUES).lower()
        self.assertNotIn("kubeconfig", serialized)
        self.assertNotIn("bearertoken", serialized)
        self.assertIsNone(re.search(r"\b(farm|lab)\b", serialized))
        self.assertNotIn("192.168.", serialized)
        self.assertNotIn("100.100.", serialized)

    def test_prometheus_and_grafana_storage_are_static_and_retained(self):
        spec = VALUES["prometheus"]["prometheusSpec"]
        self.assertEqual(spec["retention"], "30d")
        self.assertEqual(spec["retentionSize"], "120GiB")
        self.assertEqual(
            spec["persistentVolumeClaimRetentionPolicy"],
            {"whenDeleted": "Retain", "whenScaled": "Retain"},
        )
        self.assertEqual(
            spec["storageSpec"]["volumeClaimTemplate"]["spec"]["resources"][
                "requests"
            ]["storage"],
            "150Gi",
        )
        self.assertTrue(VALUES["grafana"]["persistence"]["enabled"])
        self.assertEqual(
            VALUES["grafana"]["persistence"]["existingClaim"],
            "master-monitoring-grafana",
        )

        manifests = VALUES["extraManifests"]
        pvs = {item["metadata"]["name"]: item for item in manifests if item["kind"] == "PersistentVolume"}
        self.assertEqual(
            set(pvs),
            {
                "provi-master-monitoring-prometheus",
                "provi-master-monitoring-grafana",
            },
        )
        expected = {
            "provi-master-monitoring-prometheus": (
                "/var/lib/provi/master-monitoring/prometheus",
                "150Gi",
            ),
            "provi-master-monitoring-grafana": (
                "/var/lib/provi/master-monitoring/grafana",
                "10Gi",
            ),
        }
        for name, (path, size) in expected.items():
            pv = pvs[name]
            self.assertEqual(pv["spec"]["local"]["path"], path)
            self.assertEqual(pv["spec"]["capacity"]["storage"], size)
            self.assertEqual(pv["spec"]["persistentVolumeReclaimPolicy"], "Retain")
            self.assertEqual(
                pv["metadata"]["annotations"]["helm.sh/resource-policy"], "keep"
            )
            expression = pv["spec"]["nodeAffinity"]["required"][
                "nodeSelectorTerms"
            ][0]["matchExpressions"][0]
            self.assertEqual(expression["key"], "provi.dgu.ac.kr/role")
            self.assertEqual(expression["values"], ["master-management"])

        grafana_claim = next(
            item for item in manifests if item["kind"] == "PersistentVolumeClaim"
        )
        self.assertEqual(grafana_claim["metadata"]["namespace"], "provi-monitoring")
        self.assertEqual(
            grafana_claim["metadata"]["annotations"]["helm.sh/resource-policy"],
            "keep",
        )


if __name__ == "__main__":
    unittest.main()
