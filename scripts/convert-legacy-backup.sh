#!/usr/bin/env bash
# 非生产隔离转换宿主入口；先registry，再一次性project，不进入正式恢复协议。
set -euo pipefail
exec uv run --project backend python scripts/legacy-backup-operations.py convert "$@"
