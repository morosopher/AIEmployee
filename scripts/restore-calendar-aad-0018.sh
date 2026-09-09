#!/usr/bin/env bash
# sealed窗口专用入口；独立validator先核对原preflight/audit/image，再复用同holder恢复。
# 不预先export owner密码或另起post-reopen verifier。
set -euo pipefail
exec /app/backend/.venv/bin/python -m ai_employee.cli.postgres_restore execute sealed_0018 "$@"
