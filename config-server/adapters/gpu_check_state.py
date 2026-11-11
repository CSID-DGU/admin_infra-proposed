"""GPU 점검 CronJob이 실행 사이에 남기는 값 — 같은 Pod 재알림 시각, 노드별 마지막 boot id, 진단 자료.

Redis가 없는 환경이라 점검 네임스페이스의 ConfigMap에 둔다(RBAC: configmaps get/create/update).
읽거나 쓰지 못해도 점검은 계속한다 — 그때는 재부팅 감지가 동작하지 않고, 오류 로그와 관리자 알림(한 시간에 한 번)을 낸다.
"""
import json
import time

from kubernetes import client

STATE_NAME = "gpu-check-state"
DIAG_PREFIX = "gpu-diag-"
# ConfigMap 한도(1MiB) 안에 넣기 위해 전체 바이트를 이 값으로 묶고 항목마다 똑같이 나눠 자른다.
DIAG_MAX_BYTES = 900_000
TRUNCATED_MARK = "[...truncated, tail kept...]\n"


def _upsert(v1, namespace, body):
    name = body.metadata.name
    try:
        v1.replace_namespaced_config_map(name, namespace, body)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        v1.create_namespaced_config_map(namespace, body)


class CheckState:
    """alerts: {"<pod>:<문제 종류>": 마지막 알림 epoch초}, boot_ids: {node: boot id}.

    상태를 읽지 못하면(RBAC 누락·ConfigMap 접근 실패 등) 조용히 넘어가지 않는다 — 로그를 오류로 남기고,
    저장도 하지 않으며(비어 있는 값으로 덮어써 기록을 잃지 않게), 재알림 억제는 저장 없이도 되는 시각 창으로
    대신하고(CRON_PERIOD 한 번만 알림이 나가게), report_degraded 가 같은 창에 한 번 관리자에게 알린다."""

    def __init__(self, v1, namespace, logger, now, period_sec=600):
        self._v1, self._ns, self._log, self._period = v1, namespace, logger, period_sec
        self.alerts, self.boot_ids = {}, {}
        self.problem = None
        self._dirty = False
        try:
            data = v1.read_namespaced_config_map(STATE_NAME, namespace).data or {}
            self.alerts = json.loads(data.get("alerts", "{}"))
            self.boot_ids = json.loads(data.get("boot_ids", "{}"))
        except client.exceptions.ApiException as e:
            if e.status != 404:
                self._fail(f"읽기 실패(HTTP {e.status}: RBAC에 configmaps get 이 있는지 확인)")
        except ValueError as e:
            self._fail(f"형식 오류({e})")
        except Exception as e:
            self._fail(f"읽기 실패({e})")

    def _fail(self, why):
        self.problem = self.problem or why
        self._log.error(f"[GPU CHECK] 상태 저장소 사용 불가 — {why}. 재알림 억제는 시각 창으로 대신하고 재부팅 감지는 하지 않는다")

    def should_alert(self, key, now, interval_sec):
        if self.problem:
            return now % interval_sec < self._period
        return now - self.alerts.get(key, 0) >= interval_sec

    def mark_alerted(self, key, now):
        self.alerts[key] = now
        self._dirty = True

    def prune_alerts(self, live_pods, now, interval_sec):
        """사라진 Pod와 억제 시간이 지난 기록을 지운다."""
        keep = {k: t for k, t in self.alerts.items() if k.split(":", 1)[0] in live_pods and now - t < interval_sec}
        if keep != self.alerts:
            self.alerts, self._dirty = keep, True

    def set_boot_id(self, node, boot_id):
        if self.boot_ids.get(node) != boot_id:
            self.boot_ids[node] = boot_id
            self._dirty = True

    def save(self):
        if not self._dirty or self.problem:
            return
        body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=STATE_NAME, namespace=self._ns),
            data={"alerts": json.dumps(self.alerts), "boot_ids": json.dumps(self.boot_ids)})
        try:
            _upsert(self._v1, self._ns, body)
            self._dirty = False
        except client.exceptions.ApiException as e:
            self._fail(f"저장 실패(HTTP {e.status}: RBAC에 configmaps create/update 가 있는지 확인)")
        except Exception as e:
            self._fail(f"저장 실패({e})")

    def report_degraded(self, now, notify):
        """상태 저장소를 못 쓰면 관리자에게 알린다. 저장이 안 되니 시각 창(주기 하나)으로 한 번만 낸다."""
        if self.problem and now % 3600 < self._period:
            notify(f":warning: *GPU 점검 상태 저장소 사용 불가*\n▶ {self.problem}\n"
                   "▶ 재알림 억제는 시각 창으로 대신하고 노드 재부팅 감지는 동작하지 않습니다.",
                   "gpu-check-state")


def _tail_bytes(text: str, limit: int) -> str:
    raw = (text or "").encode("utf-8")
    if len(raw) <= limit:
        return text or ""
    mark = TRUNCATED_MARK.encode()
    return mark.decode() + raw[-(limit - len(mark)):].decode("utf-8", errors="ignore")


def _fit(sections: dict) -> dict:
    """항목마다 바이트 예산(전체 / 항목 수)을 주고 넘으면 끝부분(최근 로그)만 남긴다. 잘렸음을 앞에 표시한다."""
    if not sections:
        return {}
    each = DIAG_MAX_BYTES // len(sections)
    return {k: _tail_bytes(v, each) for k, v in sections.items()}


def save_diagnostics(v1, namespace, pod, sections: dict) -> str:
    """GPU 유실 시점의 자료를 Pod에 딸린 ConfigMap으로 저장한다(Pod가 사라지면 함께 지워진다). 이름을 돌려준다.
    GPU 유실이 이어지면 1시간마다 새 ConfigMap 이 생기지만 Pod 와 함께 정리된다."""
    # 이름마다 새로 만든다(create 만 쓴다) — RBAC 가 update 를 상태 ConfigMap 하나로 묶어 두므로 덮어쓸 수 없다.
    name = f"{DIAG_PREFIX}{pod.metadata.name}-{int(time.time())}"
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=name, namespace=namespace,
            owner_references=[client.V1OwnerReference(
                api_version="v1", kind="Pod", name=pod.metadata.name, uid=pod.metadata.uid)]),
        data=_fit(sections))
    v1.create_namespaced_config_map(namespace, body)
    return name
