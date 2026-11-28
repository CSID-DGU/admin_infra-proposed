# provi-master shadow monitoring

This directory defines a **separate** `master-monitoring` Helm release for the
independent `provi-master` cluster. It does not modify, import, or retire the
current FARM monitoring release.

## Fixed contract

- Namespace: `provi-monitoring`
- Release: `master-monitoring`
- Chart: `kube-prometheus-stack` `80.2.2`
- OCI digest: `sha256:552cf360469ef8275297ec9c88a2042847bcc6b9b3be8849d42eda2201bc34ae`
- Downloaded archive SHA-256: `33896f603cdbbcaad03ee618cda2da61ce6549b469582682a85d715ae4aea1d3`
- Placement: only a node labelled
  `provi.dgu.ac.kr/role=master-management`
- Public ingress/Gateway routes: disabled
- External Alertmanager receivers: none (`null` receiver only)
- Prometheus retention: `30d`, capped at `120GiB` in a retained `150Gi` PV
- Grafana: retained `10Gi` PV

The first shadow observes only the local `provi-master` Kubernetes API, node,
system components, and workloads. Credentialed FARM/LAB remote scraping is a
later, separately reviewed change. This release deliberately contains no
remote kubeconfig, bearer token, `remote_write`, `remote_read`, or external
scrape endpoint.

## Data-safety prerequisites

The local-volume plugin will not create its source directories. Before the
first Helm install, a root-owned host preparation step must verify the node
label, verify that the paths are not symlinks or mounts from another device,
and create exactly these two directories:

```bash
sudo install -d -o 1000 -g 2000 -m 0770 \
  /var/lib/provi/master-monitoring/prometheus
sudo install -d -o 472 -g 472 -m 0770 \
  /var/lib/provi/master-monitoring/grafana
```

Neither directory may be deleted or emptied by rollback. The PVs and Grafana
PVC carry `helm.sh/resource-policy: keep`, and both PVs use reclaim policy
`Retain`. A Helm rollback changes Kubernetes manifests only; it is not a data
rollback.

## Verify the immutable chart and render

```bash
CHART=/tmp/provi-monitoring-chart/kube-prometheus-stack-80.2.2.tgz
printf '%s  %s\n' \
  '33896f603cdbbcaad03ee618cda2da61ce6549b469582682a85d715ae4aea1d3' \
  "$CHART" | sha256sum --check --strict

helm template master-monitoring "$CHART" \
  --namespace provi-monitoring \
  --values values-shadow.yaml > /tmp/master-monitoring-shadow.yaml
```

The deployment workflow must use the same archive, namespace, release name,
and values file. It must not point this release at the FARM kubeconfig. The
existing FARM monitoring release stays active throughout shadow validation and
through a separately defined retention/observation window.
