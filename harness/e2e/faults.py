"""장애 주입. 모든 장애는 이 실행의 표지(e2e-run=<runid>)를 달고, heal_all이 표지로 전부 되돌린다.

- kill: 제어기/config-server Pod를 지운다(Deployment가 다시 띄운다).
- down: config-server API를 0개로 줄인다(admin_be가 부르는 쪽이 끊긴다).
- block: config-server·제어기 Pod의 나가는 연결 중 NAS 또는 AD(=KDC, 같은 DC가 맡는다) 주소만 막는다.
  클러스터는 Calico라 NetworkPolicy가 실제로 적용된다. kubelet이 거는 NFS 마운트는 Pod 네트워크를
  타지 않으므로 홈 마운트는 끊지 않고, config-server가 여는 SSH만 막힌다.
막을 주소는 config-server Pod 안에서 그 Pod가 쓰는 설정값으로 찾고, 화면에 출력하지 않는다.
"""
import json

from .ports import safe

LABELS = {"controller": "containerssh-config-controller", "config-server": "containerssh-config-server"}

_RESOLVE = {
    "nas": 'hosts = [os.environ["NAS_SSH_HOST"]]',
    "ad": 'hosts = [n["host"] for n in json.loads(os.environ.get("FARM_AD_DC_NODES_JSON", "[]"))]',
}


class Faults:
    def __init__(self, cluster, run_id):
        self.cluster = cluster
        self.run_id = safe(run_id)
        self._downed = False

    def clear_leftovers(self):
        """이전 실행이 중간에 죽어 남긴 장애를 걷어 낸다. 다른 실행의 표지라도 지운다 — 장애가 남은 스택에서는
        어떤 시험도 믿을 수 없다."""
        self.cluster.sh('kubectl -n "$NS" delete networkpolicy -l e2e-fault=true --ignore-not-found')
        self._scale_config_server(1)

    def kill(self, target):
        label = LABELS[target]
        self.cluster.sh(f'kubectl -n "$NS" delete pod -l app={label} --wait=false')

    def down(self, target):
        if target != "config-server":
            raise ValueError(f"down은 config-server만 지원: {target}")
        self._downed = True
        self._scale_config_server(0)
        self.cluster.sh(f'kubectl -n "$NS" wait --for=delete pod -l app={LABELS[target]} --timeout=180s || true')

    def block(self, target):
        code = ("import json, os, socket\n" + _RESOLVE[target] + "\n"
                "print(json.dumps(sorted({socket.gethostbyname(h) for h in hosts})))")
        ips = json.loads(self.cluster.config_server_python(code).strip().splitlines()[-1])
        if not ips:
            raise RuntimeError(f"막을 {target} 주소를 찾지 못함")
        policy = {
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": f"e2e-block-{target}-{self.run_id}".lower(),
                         "labels": {"e2e-fault": "true", "e2e-run": self.run_id}},
            "spec": {
                "podSelector": {"matchExpressions": [
                    {"key": "app", "operator": "In", "values": sorted(LABELS.values())}]},
                "policyTypes": ["Egress"],
                "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [f"{ip}/32" for ip in ips]}}]}],
            },
        }
        self.cluster.sh('kubectl -n "$NS" apply -f - >/dev/null <<"YAMLEOF"\n' + json.dumps(policy) + "\nYAMLEOF\n")

    def heal_all(self):
        self.cluster.sh(f'kubectl -n "$NS" delete networkpolicy -l e2e-run={self.run_id} --ignore-not-found')
        if self._downed:
            self._scale_config_server(1)
            self._downed = False
        # Pod 표지로 기다리면 막 지운(종료 중인) Pod가 걸린다. Deployment 롤아웃 완료로 본다.
        for deploy in (f"config-server-{self.cluster.stack}", f"config-server-{self.cluster.stack}-controller"):
            self.cluster.sh(f'kubectl -n "$NS" rollout status deploy/{deploy} --timeout=300s >/dev/null')

    def _scale_config_server(self, replicas):
        self.cluster.sh(f'kubectl -n "$NS" scale deploy/config-server-{self.cluster.stack} --replicas={int(replicas)}')
