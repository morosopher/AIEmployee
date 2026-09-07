#!/usr/bin/env bash
set -euo pipefail
# Playwright 从 frontend/ 调起本脚本；先锚定仓库根，避免相对的 uv 项目与 Alembic
# 配置意外指向调用方目录。
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir/.."
: "${TEST_DATABASE_URL:?TEST_DATABASE_URL is required}"
: "${TEST_REDIS_URL:?TEST_REDIS_URL is required}"
: "${TEST_DATABASE_OWNER_PASSWORD_FILE:?TEST_DATABASE_OWNER_PASSWORD_FILE is required}"
: "${TEST_APP_DATABASE_PASSWORD_FILE:?TEST_APP_DATABASE_PASSWORD_FILE is required}"
: "${TEST_RETENTION_DATABASE_PASSWORD_FILE:?TEST_RETENTION_DATABASE_PASSWORD_FILE is required}"
: "${E2E_ADMIN_EMAIL:?E2E_ADMIN_EMAIL is required}"
: "${E2E_ADMIN_PASSWORD_FILE:?E2E_ADMIN_PASSWORD_FILE is required}"
database_name="$(python3 -c 'from urllib.parse import urlsplit; import os; print(urlsplit(os.environ["TEST_DATABASE_URL"]).path.lstrip("/"))')"
[[ "$database_name" == *_test ]] || { echo 'E2E database must end in _test' >&2; exit 1; }
database_lifecycle_url="$(python3 - <<'PY'
import os
import re
from urllib.parse import urlsplit

value = os.environ["TEST_DATABASE_URL"]
if value != value.strip() or any(character in value for character in "\t\n\r?#"):
    raise SystemExit("E2E database URL is invalid")
parsed = urlsplit(value)
username = parsed.username
hostname = parsed.hostname
port = parsed.port
database_name = parsed.path.removeprefix("/")
if (
    parsed.scheme != "postgresql+asyncpg"
    or username is None
    or hostname is None
    or port is None
    or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", username) is None
    or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*_test", database_name) is None
):
    raise SystemExit("E2E database URL is invalid")
normalized_host = f"[{hostname}]" if ":" in hostname else hostname
print(f"postgresql+psycopg://{username}@{normalized_host}:{port}/{database_name}")
PY
)"
redis_db="$(python3 -c 'from urllib.parse import urlsplit; import os; print(urlsplit(os.environ["TEST_REDIS_URL"]).path.lstrip("/") or "0")')"
[[ "$redis_db" == 15 ]] || { echo 'E2E Redis database must be 15' >&2; exit 1; }
# LangGraph 使用 psycopg 而业务 ORM 使用 asyncpg；二者必须指向同一隔离测试库，避免
# E2E 意外回退到开发默认端口或另一数据库而把 Graph 失败收敛成 internal_worker_error。
checkpoint_database_url="${TEST_DATABASE_URL/postgresql+asyncpg:\/\//postgresql:\/\/}"
export DATABASE_URL="$TEST_DATABASE_URL" CHECKPOINT_DATABASE_URL="$checkpoint_database_url"
export REDIS_URL="$TEST_REDIS_URL" APP_ENV=test APP_TEST_MODE=true
DATABASE_URL="$database_lifecycle_url" uv run --project backend \
  python -m ai_employee.cli.database_maintenance role-bootstrap \
  --owner-password-file "$TEST_DATABASE_OWNER_PASSWORD_FILE" \
  --app-password-file "$TEST_APP_DATABASE_PASSWORD_FILE" \
  --retention-password-file "$TEST_RETENTION_DATABASE_PASSWORD_FILE"
DATABASE_URL="$database_lifecycle_url" uv run --project backend \
  python -m ai_employee.cli.database_maintenance migrate \
  --owner-password-file "$TEST_DATABASE_OWNER_PASSWORD_FILE"
uv run --project backend python -m ai_employee.cli.create_admin --email "$E2E_ADMIN_EMAIL" --password-file "$E2E_ADMIN_PASSWORD_FILE" --if-absent
worker_pid=''
api_pid=''
cleanup() {
  [[ -z "$api_pid" ]] || kill "$api_pid" 2>/dev/null || true
  [[ -z "$worker_pid" ]] || kill "$worker_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
# E2E 保持与生产相同的单子进程模型，避免默认两个子进程争抢内部指标端口。
uv run --project backend taskiq worker --workers 1 --ack-type when_executed ai_employee.infrastructure.queue.broker:broker & worker_pid=$!
# E2E 与生产共用 OAuth 日志边界，不把浏览器 callback query 写入原始 access log。
uv run --project backend uvicorn ai_employee.main:app --host 127.0.0.1 --port 8000 --no-access-log & api_pid=$!
set +e
wait "$api_pid"
api_status=$?
set -e
exit "$api_status"
