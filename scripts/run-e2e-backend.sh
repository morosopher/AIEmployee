#!/usr/bin/env bash
set -euo pipefail
# Playwright 从 frontend/ 调起本脚本；先锚定仓库根，避免相对的 uv 项目与 Alembic
# 配置意外指向调用方目录。
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir/.."
: "${TEST_DATABASE_URL:?TEST_DATABASE_URL is required}"
: "${TEST_REDIS_URL:?TEST_REDIS_URL is required}"
: "${E2E_ADMIN_EMAIL:?E2E_ADMIN_EMAIL is required}"
: "${E2E_ADMIN_PASSWORD_FILE:?E2E_ADMIN_PASSWORD_FILE is required}"
database_name="$(python3 -c 'from urllib.parse import urlsplit; import os; print(urlsplit(os.environ["TEST_DATABASE_URL"]).path.lstrip("/"))')"
[[ "$database_name" == *_test ]] || { echo 'E2E database must end in _test' >&2; exit 1; }
redis_db="$(python3 -c 'from urllib.parse import urlsplit; import os; print(urlsplit(os.environ["TEST_REDIS_URL"]).path.lstrip("/") or "0")')"
[[ "$redis_db" == 15 ]] || { echo 'E2E Redis database must be 15' >&2; exit 1; }
export DATABASE_URL="$TEST_DATABASE_URL" REDIS_URL="$TEST_REDIS_URL" APP_ENV=test APP_TEST_MODE=true
uv run --project backend alembic -c backend/alembic.ini upgrade head
uv run --project backend python -m ai_employee.cli.create_admin --email "$E2E_ADMIN_EMAIL" --password-file "$E2E_ADMIN_PASSWORD_FILE" --if-absent
worker_pid=''
api_pid=''
cleanup() {
  [[ -z "$api_pid" ]] || kill "$api_pid" 2>/dev/null || true
  [[ -z "$worker_pid" ]] || kill "$worker_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
uv run --project backend taskiq worker --ack-type when_executed ai_employee.infrastructure.queue.broker:broker & worker_pid=$!
uv run --project backend uvicorn ai_employee.main:app --host 127.0.0.1 --port 8000 & api_pid=$!
set +e
wait "$api_pid"
api_status=$?
set -e
exit "$api_status"
