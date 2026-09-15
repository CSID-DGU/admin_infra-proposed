#!/usr/bin/env bash
# 스택 admin_be에 가입한 계정을 관리자(ADMIN)로 지정한다. 배포 서버에서 kubectl로 실행한다.
# 공개 레포의 워크플로로 돌리면 이메일이 로그에 남으므로 그렇게 하지 않는다.
#
#   make-admin.sh <baseline|noprobe|full> <가입한 이메일>
set -euo pipefail
STACK=${1:?"스택 이름(baseline|noprobe|full)"}
EMAIL=${2:?"가입한 이메일"}
[ -z "${KUBECONFIG:-}" ] && [ -r /etc/kubernetes/ci-deployer.conf ] && export KUBECONFIG=/etc/kubernetes/ci-deployer.conf
case "$STACK" in baseline|noprobe|full) ;; *) echo "알 수 없는 스택: $STACK"; exit 2 ;; esac
[[ "$EMAIL" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]] || { echo "이메일 형식이 아님: $EMAIL"; exit 2; }
kubectl -n "ailab-$STACK" exec -i mysql-0 -- sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N web_admin 2>/dev/null' <<SQL
UPDATE users SET role='ADMIN' WHERE email='$EMAIL';
SELECT CONCAT(email, ' → ', role) FROM users WHERE email='$EMAIL';
SQL
