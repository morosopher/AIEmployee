#!/usr/bin/env bash
# 从仓库根目录执行registry/隔离配置/清理故障矩阵与真实app角色只读健康检查。
# 未提供目标时只使用计划固定的回环Task13合成库；统一orchestrator再次校验目标并管理两阶段fixture。
# 原生pg_restore→正常backup和实际专用资源清理另由受控镜像演练记录，不能把本门禁称为原生恢复。
set -euo pipefail
[[ "$#" -eq 0 ]] || { printf '%s\n' 'legacy_conversion_test_arguments_invalid' >&2; exit 1; }
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test}"
export PYTEST_ADDOPTS='-k "(test_postgres_backup_restore and legacy) or test_read_current_uses_one_snapshot_across_a_concurrent_committed_refresh or test_database_contains_named_aead_nonce_counter_and_manual_resolution_checks" --tb=short'
exec uv run --project backend python scripts/run_integration_tests.py
