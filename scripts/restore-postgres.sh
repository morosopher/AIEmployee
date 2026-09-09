#!/usr/bin/env bash
# 普通manifest灾备入口；Secret/解密/进程只由镜像内controller在完整输入guard后管理。
# ordinal由固定宿主逐字段传入，不source SQL、不提前授予CONNECT。
set -euo pipefail
exec /app/backend/.venv/bin/python -m ai_employee.cli.postgres_restore execute generic "$@"
