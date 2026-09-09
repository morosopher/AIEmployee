#!/usr/bin/env bash
# 固定phase和artifact前缀；只读审计在同一owner holder内完成并发布0600证据。
set -euo pipefail
exec /app/backend/.venv/bin/python -m ai_employee.cli.calendar_aad_audit_0019 "$@"
