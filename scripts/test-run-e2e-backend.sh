#!/usr/bin/env bash
set -euo pipefail

script='scripts/run-e2e-backend.sh'
grep -Fq 'api_pid=' "$script"
grep -Fq 'wait "$api_pid"' "$script"
! grep -Fq 'exec uv run --project backend uvicorn' "$script"
# E2E 与生产共用 Worker 指标生命周期，必须避免 Taskiq 默认两个子进程争抢 9101。
grep -Fq 'taskiq worker --workers 1 --ack-type when_executed' "$script"
grep -Fq 'ai_employee.cli.database_maintenance role-bootstrap' "$script"
grep -Fq 'ai_employee.cli.database_maintenance migrate' "$script"
! grep -Fq 'alembic -c backend/alembic.ini upgrade head' "$script"
! grep -Eq 'repair|stamp|downgrade|revision rewrite' "$script"

# 使用只记录参数和环境的 fake uv 运行完整脚本，证明 SQLAlchemy 测试 DSN 会转换为
# LangGraph/psycopg 可接受的同库 DSN；测试不得连接数据库、Redis 或启动真实进程。
sandbox="$(mktemp -d)"
trap 'rm -rf -- "$sandbox"' EXIT
mkdir -p "$sandbox/bin"
cat >"$sandbox/bin/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\t%s\t%s\n' "${DATABASE_URL-}" "${CHECKPOINT_DATABASE_URL-}" "$*" >>"$E2E_TEST_LOG"
EOF
chmod +x "$sandbox/bin/uv"
printf '%s\n' 'synthetic-e2e-password' >"$sandbox/password"
printf '%s\n' 'synthetic-owner-password' >"$sandbox/owner-password"
printf '%s\n' 'synthetic-app-password' >"$sandbox/app-password"
printf '%s\n' 'synthetic-retention-password' >"$sandbox/retention-password"
chmod 600 "$sandbox/password" "$sandbox/owner-password" \
  "$sandbox/app-password" "$sandbox/retention-password"

PATH="$sandbox/bin:$PATH" \
E2E_TEST_LOG="$sandbox/uv.log" \
TEST_DATABASE_URL='postgresql+asyncpg://tester:test@127.0.0.1:55432/fixture_test' \
TEST_REDIS_URL='redis://127.0.0.1:56379/15' \
TEST_DATABASE_OWNER_PASSWORD_FILE="$sandbox/owner-password" \
TEST_APP_DATABASE_PASSWORD_FILE="$sandbox/app-password" \
TEST_RETENTION_DATABASE_PASSWORD_FILE="$sandbox/retention-password" \
E2E_ADMIN_EMAIL='fixture-admin@example.test' \
E2E_ADMIN_PASSWORD_FILE="$sandbox/password" \
bash "$script"

expected_checkpoint='postgresql://tester:test@127.0.0.1:55432/fixture_test'
grep -Fq $'postgresql+psycopg://tester@127.0.0.1:55432/fixture_test\t'"${expected_checkpoint}"$'\trun --project backend python -m ai_employee.cli.database_maintenance role-bootstrap --owner-password-file '"$sandbox/owner-password" "$sandbox/uv.log"
grep -Fq $'postgresql+psycopg://tester@127.0.0.1:55432/fixture_test\t'"${expected_checkpoint}"$'\trun --project backend python -m ai_employee.cli.database_maintenance migrate --owner-password-file '"$sandbox/owner-password" "$sandbox/uv.log"
awk -F '\t' -v expected="$expected_checkpoint" '
  $1 == "postgresql+asyncpg://tester:test@127.0.0.1:55432/fixture_test" && $2 == expected {
    matched = 1
  }
  END { exit matched ? 0 : 1 }
' "$sandbox/uv.log"
