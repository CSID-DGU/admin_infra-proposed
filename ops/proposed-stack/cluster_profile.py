"""운영 Config Server Helm 값을 새 cluster.* 프로필로 바꿔 표준 출력에 JSON으로 쓴다.

운영 릴리스가 아직 구 차트면 SSH 값이 farm.* 아래에 있고 Kerberos KDC도 kdcHost 한 개로
저장되어 있다. proposed-stack은 운영 값을 읽어 실험 릴리스에 재사용하므로, 차트 전환 기간에는
두 형식을 모두 받아 새 형식만 담은 작은 overlay를 만든다.
"""

import json
import sys


def _nonempty(primary, fallback):
    return primary if primary not in (None, "", [], {}) else fallback


def cluster_profile(values, cluster_id="farm"):
    current = values.get("cluster") or {}
    legacy = values.get("farm") or {}

    current_ssh = current.get("ssh") or {}
    legacy_ssh = legacy.get("ssh") or {}
    current_ad = current.get("adSsh") or {}
    legacy_ad = legacy.get("adSsh") or {}

    krb5 = values.get("krb5") or {}
    kdc_hosts = krb5.get("kdcHosts") or []
    if not kdc_hosts and krb5.get("kdcHost"):
        kdc_hosts = [krb5["kdcHost"]]

    return {
        "cluster": {
            "id": cluster_id,
            "homeMountRootTemplate": current.get("homeMountRootTemplate") or "",
            "ssh": {
                "user": _nonempty(current_ssh.get("user"), legacy_ssh.get("user", "")),
                "keyPath": _nonempty(current_ssh.get("keyPath"), legacy_ssh.get("keyPath", "")),
                "nodes": _nonempty(current_ssh.get("nodes"), legacy_ssh.get("nodes", [])),
            },
            "adSsh": {
                "user": _nonempty(current_ad.get("user"), legacy_ad.get("user", "")),
                "keyPath": _nonempty(current_ad.get("keyPath"), legacy_ad.get("keyPath", "")),
                "nodes": _nonempty(current_ad.get("nodes"), legacy_ad.get("nodes", [])),
            },
        },
        "krb5": {
            "realm": krb5.get("realm", ""),
            "kdcHosts": kdc_hosts,
            "adminServer": krb5.get("adminServer") or (kdc_hosts[0] if kdc_hosts else ""),
            "domain": krb5.get("domain") or ("ailab.dgu" if krb5.get("realm") else ""),
        },
    }


if __name__ == "__main__":
    json.dump(cluster_profile(json.load(sys.stdin) or {}), sys.stdout)
    sys.stdout.write("\n")
