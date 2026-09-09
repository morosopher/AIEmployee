#!/usr/bin/env bash
# 从仓库根目录执行；官方两阶段fixture独占本地合成目标，audit不连接真实供应商或生产账号。
# TEST_DATABASE_URL由调用者显式提供并由统一orchestrator校验，禁止在此另建迁移/角色清理流程。
# nonzero用例同时覆盖六类当前资格缺口、发布补偿，以及sealed同holder的RR/RO比较和真实25006。
set -euo pipefail
[[ "$#" -eq 0 ]] || { printf '%s\n' 'calendar_aad_audit_test_arguments_invalid' >&2; exit 1; }
[[ -n "${TEST_DATABASE_URL:-}" ]] || { printf '%s\n' 'calendar_aad_audit_test_database_required' >&2; exit 1; }
export PYTEST_ADDOPTS='-k "test_calendar_owner_facts_isolated_from_app_identity or test_formal_calendar_oneoffs_keep_app_business_and_owner_facts or test_calendar_audit_nonzero_phases_with_real_app_oneoffs or test_audit_version_rejects_coercion or test_audit_and_sealed_callbacks_are_separate_from_generic_restore or test_read_current_uses_one_snapshot_across_a_concurrent_committed_refresh or test_database_contains_named_aead_nonce_counter_and_manual_resolution_checks" --tb=short'
exec uv run --project backend python scripts/run_integration_tests.py
