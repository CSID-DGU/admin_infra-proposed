"""trial 앞의 환경 판정. 자원 목록(inventory)을 읽기만 하고 아무것도 지우지 않는다.

잔재는 범위 안의 다른 측정 사용자 자원이다. 이 trial 의 사용자 자원은 잔재가 아니다. 회수 trial 은
같은 사용자로 생성 trial 에 이어 돌므로 그 앞에 Pod 와 Service 가 있는 것이 정상이다.

원장·NAS 홈·AD·farm keytab·공용 그룹은 아직 볼 수 없다. 확인하지 않은 것을 CLEAN 이라고 하지
않으려고, 스택 단위로 깨끗해도 UNKNOWN 과 unchecked 목록을 돌려준다. 노드 단위 확인이 붙으면
UNCHECKED 가 비고 그때 CLEAN 이 나온다.
"""
import inventory

CLEAN, DIRTY, UNKNOWN = "CLEAN", "DIRTY", "UNKNOWN"
UNCHECKED = ("ledger", "nas_home", "ad", "farm_keytab", "shared_group")


def environment_for(host, namespace, prefix, *, username):
    """trial_runner 의 environment 포트. 부를 때마다 목록을 새로 모아 (verdict, evidence) 를 돌려준다."""

    def environment():
        inv = inventory.collect_inventory(host, namespace, prefix)
        residue = [{"user": user, "kind": kind, "name": name}
                   for user, kinds in sorted(inv["by_user"].items()) if user != username
                   for kind, names in sorted(kinds.items()) for name in names]
        evidence = {"username": username, "residue": residue, "e2e_users": inv["e2e_users"],
                    "errors": inv["errors"], "unchecked": list(UNCHECKED)}
        if residue or inv["e2e_users"]:
            return DIRTY, evidence
        return UNKNOWN, evidence

    return environment
