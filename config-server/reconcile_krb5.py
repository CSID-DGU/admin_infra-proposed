import os
import tempfile
import threading
import time

from main import (app, _get_farm_node_info, _remove_krb5_from_farm, _farm_ssh,
                  _record_krb5_cleanup_pending, SHARED_GID_MIN, SHARED_GID_MAX)
from utils import (get_db_connection, read_group_lines, parse_group_line,
                   read_passwd_lines, parse_passwd_line,
                   nas_shared_gids_for_users, nas_flush_gss_cache)
from adapters.nas_gss_flush_lock import try_acquire_flush_lock, release_flush_lock


def reconcile_krb5_cleanup_pending() -> None:
    """krb5_cleanup_pending 테이블의 레코드를 순회하며 재정리를 시도한다.

    레이스 컨디션 주의: 이 함수가 예약 목록을 읽은 뒤부터 실제로 _remove_krb5_from_farm을
    실행하기까지 사이에, 마침 그 (username, node_name)에 대한 배포가 성공해서
    _clear_krb5_cleanup_pending이 같은 행을 지웠을 수 있다. 단순히 다 읽고 나서 끝에 가서
    지우면 이미 살아난 keytab을 뒤늦게 지워버리게 된다. 그래서 각 행마다 먼저 그 행을
    원자적으로 DELETE(선점)해보고, 실제로 지워진 경우(=아직 아무도 안 지운 예약)에만
    실제 정리를 실행한다. 이미 지워져 있었다면(배포 성공으로 먼저 취소됨) 조용히
    건너뛴다."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT username, node_name FROM krb5_cleanup_pending")
        rows = cur.fetchall()
    conn.close()

    for username, node_name in rows:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM krb5_cleanup_pending WHERE username=%s AND node_name=%s",
                    (username, node_name),
                )
                claimed = cur.rowcount > 0
            conn.commit()
        finally:
            conn.close()

        if not claimed:
            app.logger.info(f"[KRB5 RECONCILE] 예약이 이미 취소됨(그 사이 배포 성공): {username} ← {node_name}")
            continue

        try:
            _remove_krb5_from_farm(username, node_name)
            app.logger.info(f"[KRB5 RECONCILE] pending 정리 성공: {username} ← {node_name}")
        except Exception as e:
            app.logger.warning(f"[KRB5 RECONCILE] pending 정리 재시도 실패(다음 주기에 재시도): {username} ← {node_name} — {e}")
            _record_krb5_cleanup_pending(username, node_name)


def _get_expected_krb5_usernames_for_node(node_name: str) -> set:
    """지금 이 노드에 떠 있어야 하는(=NodePort가 살아있는) username 집합."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT username FROM nodeport_allocations WHERE node_name=%s",
                (node_name,),
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def reconcile_krb5_orphans() -> None:
    """각 farm 노드의 keytab 목록과 '지금 이 노드에 떠 있어야 하는 username'(nodeport_allocations
    기준) 목록을 대조해 delete_pod/delete_user 흐름을 아예 타지 않은 고아(수동 조작, 코드 버그 등)를
    찾아낸다.

    주의 — 지금은 자동 삭제하지 않고 후보만 로그로 남긴다. 레거시(마이그레이션 전) Docker
    컨테이너 유저는 애초에 nodeport_allocations에 등록될 방법이 없어서, 이 대조만으로는
    "아직 신시스템으로 안 옮긴, 지금도 살아서 쓰이는 레거시 계정"과 "진짜 고아"를 구분할 수
    없다. 자동 삭제로 뒀다가 farm6의 레거시 계정 여러 개(2026-09-03 dry-run으로 확인, dm20020204/
    donghyun2/jy/ohchanju3)가 한꺼번에 지워질 뻔한 적이 있다. 레거시 계정을 구분할 방법(예:
    별도 allowlist/테이블)이 생기기 전까진 사람이 로그를 보고 직접 정리하는 게 안전하다."""
    for node in app.config["FARM_NODES"]:
        try:
            node_info = _get_farm_node_info(node["name"])
            result = _farm_ssh(node_info["host"], node_info["port"], "list")
            deployed_usernames = {u for u in result.splitlines() if u}
        except Exception as e:
            app.logger.warning(f"[KRB5 RECONCILE] {node['name']} keytab 목록 조회 실패: {e}")
            continue

        expected_usernames = _get_expected_krb5_usernames_for_node(node["name"])
        orphans = deployed_usernames - expected_usernames

        for username in orphans:
            app.logger.warning(
                f"[KRB5 RECONCILE] 고아 후보(자동 삭제 안 함, 확인 필요): {username} @ {node['name']} "
                "— nodeport_allocations엔 없지만 레거시 계정일 수 있음, 직접 확인 후 정리할 것"
            )


def _ledger_shared_gids() -> set:
    """대장이 아는 공용 그룹 gid 전체. 대조는 이 안에서만 한다.

    AD 에는 config-server 가 만들지 않은 공용 그룹도 있다 — 과거 공용 그룹은 전부 수동
    samba-tool 로 만들었고 지금도 남아 있다. 그런 그룹에 속한 사용자가 한 명이라도 있으면
    "완전 일치"를 요구하는 대조가 영원히 어긋나 flush 가 **영구히 막힌다**(2026-09-22 실운영
    배포에서 실제로 걸렸다). 대장이 모르는 그룹은 우리 관심사가 아니므로 대조에서 뺀다."""
    gids = set()
    for line in read_group_lines():
        g = parse_group_line(line)
        if not g or g["gid"] < SHARED_GID_MIN:
            continue
        if SHARED_GID_MAX is not None and g["gid"] > SHARED_GID_MAX:
            continue
        gids.add(g["gid"])
    return gids


def _ledger_shared_gids_by_user() -> dict:
    """계정 대장이 말하는 {사용자: 공용 대역 gid 집합}. 이게 "이래야 하는" 값이다.

    그룹 멤버만이 아니라 **대장의 모든 계정**을 빈 집합으로 깔아 둔다. 그래야 그룹에서 뺀
    사용자도 대조 대상에 남는다 — 멤버만 보면 회수가 NAS 에 반영되기 전에 비워 버려서
    그 사용자가 하루 더 접근한다."""
    wanted = {p["name"]: set() for l in read_passwd_lines() if (p := parse_passwd_line(l))}
    for line in read_group_lines():
        g = parse_group_line(line)
        if not g or g["gid"] < SHARED_GID_MIN:
            continue
        if SHARED_GID_MAX is not None and g["gid"] > SHARED_GID_MAX:
            continue
        for member in g["members"]:
            wanted.setdefault(member, set()).add(g["gid"])
    return wanted


def _gss_state_path() -> str:
    group_path = app.config["GROUP_PATH"]
    return os.path.join(os.path.dirname(group_path), ".gss_flush_state")


def _write_gss_state(state_path: str, group_mtime: float) -> None:
    """임시 파일에 쓰고 os.replace 로 교체한다 — 같은 파일에 두 프로세스(30분 크론과 온디맨드
    재시도 루프)가 겹쳐 쓸 수 있는데, open(path,"w")는 원자적이지 않아 극히 드물게 값이 깨질
    여지가 있었다. os.replace 는 POSIX에서 원자적이라 항상 둘 중 하나의 완전한 값만 남는다.

    임시 파일 이름은 pid만으로 만들지 않는다 — pid는 같은 프로세스 안 스레드끼리는 겹쳐서,
    같은 워커의 온디맨드 재시도 스레드 두 개가 같은 임시 파일을 동시에 열면 서로 truncate
    할 수 있다. tempfile.mkstemp 로 매 호출마다 유일한 이름을 받는다."""
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(state_path) or ".",
        prefix=os.path.basename(state_path) + ".tmp.",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"{group_mtime}\n")
        os.replace(tmp_path, state_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def reconcile_nas_gss_cache() -> bool:
    """공용 그룹이 바뀌었으면 NAS 의 GSS 컨텍스트 캐시를 비운다(#153).

    NAS 는 컨텍스트를 맺을 때 그룹 목록을 한 번 풀어 고정하고 컨텍스트 수명(= 티켓 수명 24h)
    동안 다시 보지 않는다. 비우지 않으면 AD 를 고쳐도 이미 떠 있는 Pod 에 최대 하루 반영되지
    않는다. 클라이언트에서 티켓을 다시 받는 것으로는 풀리지 않는다 — 2026-09-22 실측.

    **NAS 가 아직 모르는 상태에서 비우면 안 된다.** NAS winbind 가 새 멤버십을 알기까지 몇 분
    걸리는데, 그 전에 비우면 다음 컨텍스트가 **옛 목록으로 다시 굳어** 하루가 또 간다. 그래서
    고정된 대기 시간을 두지 않고 NAS 에 직접 물어 일치할 때만 비운다. 어긋나면 이번 주기는
    건너뛰고 대장 mtime 을 남기지 않으므로 다음 주기가 다시 시도한다.

    30분 크론(`__main__`)과 온디맨드 재시도 루프(`trigger_nas_gss_flush_ondemand`, #161) 둘 다
    이 함수를 그대로 부른다 — mtime 게이트가 이미 중복 flush를 막아주므로 별도 락 없이 안전하다.

    반환값: 이번 호출에서 실제로 flush했으면 True(호출부가 재시도를 멈추는 신호로 씀),
    아니면(반영할 변경이 없었거나 아직 NAS가 못 따라잡았거나 실패) False."""
    state_path = _gss_state_path()
    try:
        group_mtime = os.path.getmtime(app.config["GROUP_PATH"])
    except OSError:
        return False                # 아직 계정이 하나도 없는 스택
    try:
        with open(state_path) as f:
            flushed_for = float(f.read().strip() or 0)
    except (OSError, ValueError):
        flushed_for = 0.0
    if flushed_for >= group_mtime:
        return False

    wanted = _ledger_shared_gids_by_user()
    known = _ledger_shared_gids()
    try:
        actual = nas_shared_gids_for_users(wanted, SHARED_GID_MIN, SHARED_GID_MAX)
    except Exception as e:
        app.logger.warning(f"[NAS GSS] NAS 그룹 조회 실패(다음 주기 재시도): {e}")
        return False
    actual = {u: gids & known for u, gids in actual.items()}

    # 더한 그룹뿐 아니라 뺀 그룹도 본다. 같아야만 비운다 — 한쪽만 보면 회수가 반영되지 않는다.
    stale = {}
    for username, gids in wanted.items():
        got = actual.get(username)
        if got is None:
            # NAS 가 이 이름을 모른다(AD 에 없는 레거시 계정 등). 공용 그룹이 걸려 있지 않으면
            # 판정 대상이 아니므로 넘어가고, 걸려 있다면 아직 반영 안 된 것으로 본다.
            if gids:
                stale[username] = (gids, None)
            continue
        if got != gids:
            stale[username] = (gids, got)
    if stale:
        app.logger.info(f"[NAS GSS] NAS 가 아직 대장을 따라오지 못함(다음 주기 재시도): {stale}")
        return False

    try:
        nas_flush_gss_cache()
    except Exception as e:
        app.logger.warning(f"[NAS GSS] 캐시 비우기 실패(다음 주기 재시도): {e}")
        return False

    _write_gss_state(state_path, group_mtime)
    app.logger.info(f"[NAS GSS] 공용 그룹 변경 반영 완료 — 사용자 {len(wanted)}명, mtime={group_mtime}")
    return True


NAS_GSS_ONDEMAND_POLL_SEC = 20
NAS_GSS_ONDEMAND_TIMEOUT_SEC = 600  # 10분 — 그 안에 안 되면 포기하고 30분 크론에 맡긴다.
NAS_GSS_ONDEMAND_LOCK_TTL_SEC = NAS_GSS_ONDEMAND_TIMEOUT_SEC + 60  # 루프보다 넉넉히 길게.


def _nas_gss_flush_retry_loop(lock_token: str) -> None:
    """그룹 변경 승인 직후부터 짧은 간격으로 재시도한다(#161).

    30분 크론과 이 함수는 서로의 존재를 모른 채 같은 reconcile_nas_gss_cache()를 부른다 —
    그 함수 안의 mtime 게이트가 중복 실행을 막아주므로 조율이 필요 없다. 이 함수가 막는 건
    "온디맨드 루프끼리" 겹치는 것뿐이다(같은 시점에 승인이 여러 건이면 gunicorn 워커마다
    스레드가 하나씩 뜰 수 있어서) — 그건 Redis 락으로 막는다.

    타임아웃 안에 못 끝나면 그냥 포기한다 — 30분 크론이 결국 잡아준다(안전망, 안 없앰).
    별도 스레드에서 돈다는 게 핵심: 이 함수를 부른 HTTP 요청(admin_be가 승인 직후 던진 호출)은
    바로 돌아가고, 이 재시도는 그 요청과 무관하게 백그라운드에서 계속된다."""
    with app.app_context():
        deadline = time.monotonic() + NAS_GSS_ONDEMAND_TIMEOUT_SEC
        attempts = 0
        try:
            while time.monotonic() < deadline:
                attempts += 1
                try:
                    if reconcile_nas_gss_cache():
                        app.logger.info(f"[NAS GSS 온디맨드] {attempts}번째 시도에 flush 완료")
                        return
                except Exception:
                    app.logger.exception("[NAS GSS 온디맨드] 재시도 중 예외 — 다음 시도 계속")
                time.sleep(NAS_GSS_ONDEMAND_POLL_SEC)
            app.logger.info(
                f"[NAS GSS 온디맨드] {NAS_GSS_ONDEMAND_TIMEOUT_SEC}초 안에 못 끝남({attempts}번 시도) — "
                "30분 크론에 맡기고 포기"
            )
        finally:
            release_flush_lock(lock_token)


def trigger_nas_gss_flush_ondemand() -> bool:
    """승인 직후 admin_be가 부르는 진입점. 락을 얻으면 백그라운드 스레드로 재시도 루프를 띄우고
    즉시 반환한다(요청을 안 막음) — 락을 못 얻으면(이미 도는 루프가 있으면) 아무것도 안 하고
    반환한다. 이미 도는 루프가 매번 최신 대장 상태를 다시 읽으므로, 그 루프가 이번에 새로 생긴
    변경분까지 같이 처리해준다 — 겹쳐 부를 필요가 없다.

    반환값은 "루프를 새로 띄웠는지"일 뿐, flush 성공 여부가 아니다 — 이건 fire-and-forget이다."""
    lock_token = try_acquire_flush_lock(NAS_GSS_ONDEMAND_LOCK_TTL_SEC)
    if lock_token is None:
        app.logger.info("[NAS GSS 온디맨드] 이미 재시도 루프가 돌고 있어 새로 안 띄움")
        return False
    try:
        threading.Thread(target=_nas_gss_flush_retry_loop, args=(lock_token,), daemon=True).start()
    except Exception:
        # 스레드 시작 자체가 실패하면 루프가 절대 못 돌아 release가 안 불린다 — 여기서 직접 풀어준다.
        release_flush_lock(lock_token)
        raise
    return True


if __name__ == "__main__":
    with app.app_context():
        reconcile_krb5_cleanup_pending()
        reconcile_krb5_orphans()
        reconcile_nas_gss_cache()
