#!/usr/bin/env bash
# 将数据库角色初始化收敛到唯一 typed lifecycle CLI；脚本从不读取 Secret 内容。
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${POSTGRES_BOOTSTRAP_PASSWORD_FILE:?POSTGRES_BOOTSTRAP_PASSWORD_FILE is required}"
: "${APP_DATABASE_PASSWORD_FILE:?APP_DATABASE_PASSWORD_FILE is required}"
: "${RETENTION_DATABASE_PASSWORD_FILE:?RETENTION_DATABASE_PASSWORD_FILE is required}"

for secret_path in \
  "$POSTGRES_BOOTSTRAP_PASSWORD_FILE" \
  "$APP_DATABASE_PASSWORD_FILE" \
  "$RETENTION_DATABASE_PASSWORD_FILE"; do
  if [[ ! -r "$secret_path" ]]; then
    printf 'database role Secret file is unreadable\n' >&2
    exit 1
  fi
done

# 同一脚本既从仓库根运行，也从镜像的 /app/backend 工作目录运行；这里只解析项目路径，
# 不把数据库 URL、密码内容或其他 authority 放入 argv、诊断输出或临时文件。
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
backend_project="${script_dir}/../backend"
if [[ ! -f "${backend_project}/pyproject.toml" ]]; then
  printf 'database role lifecycle project is unavailable\n' >&2
  exit 1
fi

exec uv run --project "$backend_project" --no-sync \
  python -m ai_employee.cli.database_maintenance role-bootstrap \
  --owner-password-file "$POSTGRES_BOOTSTRAP_PASSWORD_FILE" \
  --app-password-file "$APP_DATABASE_PASSWORD_FILE" \
  --retention-password-file "$RETENTION_DATABASE_PASSWORD_FILE"
