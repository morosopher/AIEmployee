#!/usr/bin/env bash
# 初始 M2 release 契约显式运行 Task13 合成数据库上的三阶段 AAD audit；完整矩阵由 Task30 补齐。
set -euo pipefail

TEST_DATABASE_URL=postgresql+asyncpg://ai_employee_test:synthetic-password@127.0.0.1:55443/ai_employee_task13_test bash scripts/test-calendar-aad-0019-audit.sh
