#!/usr/bin/env bash
set -euo pipefail

readonly required_test_database_url='postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test'
if [[ -v TEST_DATABASE_URL && "${TEST_DATABASE_URL}" != "${required_test_database_url}" ]]; then
  printf 'M2 release test refused: TEST_DATABASE_URL must match the fixed Task13 database\n' >&2
  exit 1
fi
export TEST_DATABASE_URL="${required_test_database_url}"

# 只有上述固定合成目标通过准入后才能启动子进程。完整 CI 与各独立审计按批准顺序
# 串行运行；任一失败立即退出，不提供跳过项，也不执行生产迁移、恢复或真实账户写入。
just ci
bash scripts/test-calendar-aad-0019-audit.sh
uv run --project backend pytest backend/tests/integration/faults/test_m2_write_recovery.py -q
uv run --project backend pytest backend/tests/unit/application/test_calendar_event_aad.py backend/tests/unit/infrastructure/db/test_database_access.py backend/tests/unit/infrastructure/db/test_database_grants.py backend/tests/integration/db/test_migrations.py backend/tests/integration/observability/test_uvicorn_oauth_query_redaction.py -q
uv run --project backend pytest backend/tests/unit/application/test_calendar_aad_digests.py backend/tests/unit/application/test_oauth_refresh_identity.py backend/tests/unit/application/test_oauth_refresh_coordinator.py backend/tests/unit/application/test_connection_capability_use_cases.py -q
uv run --project backend pytest backend/tests/integration/m2/test_connection_capability_repository.py backend/tests/integration/m2/test_credential_rotation_repository.py backend/tests/integration/m2/test_oauth_refresh_coordinator.py backend/tests/integration/api/test_connections.py backend/tests/integration/google/test_oauth_flow.py backend/tests/integration/google/test_gmail_sync.py backend/tests/integration/google/test_calendar_sync.py backend/tests/integration/microsoft/test_oauth_flow.py backend/tests/integration/microsoft/test_mail_sync.py backend/tests/integration/microsoft/test_calendar_sync.py backend/tests/integration/operations/test_calendar_aad_0019_preflight.py -q
uv run --project backend pytest backend/tests/integration/operations/test_database_maintenance_gate.py backend/tests/integration/operations/test_postgres_backup_restore.py backend/tests/integration/operations/test_calendar_aad_0019_deadline_restore.py -q
uv run --project backend pytest backend/tests/integration/retention/test_m2_action_retention.py backend/tests/integration/privacy/test_source_cache_cleanup.py backend/tests/integration/privacy/test_all_data_deletion.py backend/tests/unit/application/test_privacy.py backend/tests/integration/workers/test_outbox_dispatch.py backend/tests/integration/workers/test_retry_recovery.py backend/tests/integration/m2/test_tool_execution_claim.py backend/tests/unit/workers/test_execution_lease.py backend/tests/integration/retention/test_role_permissions.py backend/tests/unit/test_init_db_roles_script.py -q
bash scripts/test-legacy-backup-conversion.sh
bash scripts/test-deployment.sh
bash scripts/test-tooling.sh
# scanner 同时检查已暂存文件，拒绝私有配置、转储、报告、缓存及其他生成物进入提交。
python3 scripts/verify-m2-sensitive-output.py
git diff --check
