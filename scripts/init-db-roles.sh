#!/usr/bin/env bash
# 初始化 AI Employee 的最小 PostgreSQL 应用/保留角色；不得把密码写入命令行或仓库。
set -euo pipefail

: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${APP_DATABASE_PASSWORD_FILE:?APP_DATABASE_PASSWORD_FILE is required}"
: "${RETENTION_DATABASE_PASSWORD_FILE:?RETENTION_DATABASE_PASSWORD_FILE is required}"

if [[ ! -r "$APP_DATABASE_PASSWORD_FILE" || ! -r "$RETENTION_DATABASE_PASSWORD_FILE" ]]; then
  echo "database role password file is unreadable" >&2
  exit 1
fi

# 密码只经 psql 标准输入传递：base64 令 psql 变量保持安全 token，再由数据库以 format(%L)
# 构造字面量。这样密码不会进入 argv、环境变量或 shell 诊断输出。
# Secret 挂载常不带末尾换行；read 已写入该行时会返回非零，不能让 set -e 误判失败。
read -r app_password < "$APP_DATABASE_PASSWORD_FILE" || [[ -n "$app_password" ]]
read -r retention_password < "$RETENTION_DATABASE_PASSWORD_FILE" || [[ -n "$retention_password" ]]
app_password_b64="$(printf '%s' "$app_password" | base64 | tr -d '\n')"
retention_password_b64="$(printf '%s' "$retention_password" | base64 | tr -d '\n')"
unset app_password retention_password

{
  printf '\\set app_password_b64 %s\n' "$app_password_b64"
  printf '\\set retention_password_b64 %s\n' "$retention_password_b64"
  cat <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_employee_app') THEN
    CREATE ROLE ai_employee_app LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_employee_retention') THEN
    CREATE ROLE ai_employee_retention LOGIN;
  END IF;
END
$$;
SELECT format('ALTER ROLE ai_employee_app PASSWORD %L', convert_from(decode(:'app_password_b64', 'base64'), 'UTF8')) \gexec
SELECT format('ALTER ROLE ai_employee_retention PASSWORD %L', convert_from(decode(:'retention_password_b64', 'base64'), 'UTF8')) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO ai_employee_app, ai_employee_retention', current_database()) \gexec
GRANT USAGE ON SCHEMA public TO ai_employee_app, ai_employee_retention;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ai_employee_app;
REVOKE UPDATE, DELETE ON audit_events FROM ai_employee_app;
-- retention 角色仅获得保留 Worker 实际需要的表权限。全量隐私删除可删除旧审计后追加
-- 唯一完成事实，但绝不允许修改既有审计行。
GRANT SELECT ON users, oauth_connections, sync_cursors, task_runs TO ai_employee_retention;
-- 先撤销历史版本留下的整表 UPDATE，再按 Worker 实际赋值列回授，保证脚本可重复收紧。
REVOKE UPDATE ON email_messages, users FROM ai_employee_retention;
GRANT SELECT, DELETE ON email_messages TO ai_employee_retention;
GRANT UPDATE (body_ciphertext, body_nonce, body_key_version) ON email_messages TO ai_employee_retention;
GRANT SELECT, DELETE ON email_analyses, email_threads, calendar_events,
  encrypted_credentials TO ai_employee_retention;
REVOKE UPDATE ON sync_cursors FROM ai_employee_retention;
GRANT UPDATE (cursor, last_success_at, last_attempt_at, last_error_code) ON sync_cursors TO ai_employee_retention;
-- 保留与隐私 Worker 以 "SELECT 主键/谓词 -> 有界 DELETE" 执行，DELETE 谓词也读取表列，
-- 因此这些表必须同时授予 SELECT；不额外授予 INSERT 或 UPDATE。
GRANT SELECT, DELETE ON messages, conversations, daily_brief_items, daily_briefs, llm_invocations,
  approval_requests, tool_executions, task_steps, outbox_events, task_runs, user_sessions,
  oauth_connections TO ai_employee_retention;
GRANT UPDATE (email, display_name, password_hash, is_active, email_body_retention_days,
  source_metadata_retention_days, workspace_history_retention_days) ON users TO ai_employee_retention;
GRANT SELECT, INSERT, DELETE ON audit_events TO ai_employee_retention;
-- 两个运行角色只通过 Identity 写入审计；USAGE 足以生成值，不允许读取序列状态。
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ai_employee_app, ai_employee_retention;
SQL
} | psql --set=ON_ERROR_STOP=1 --dbname="$POSTGRES_DB"
unset app_password_b64 retention_password_b64
