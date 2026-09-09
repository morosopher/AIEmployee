#!/usr/bin/env bash
# 固定grace，仅回收registry/labels/锁共同证明已停止的自有隔离project。
set -euo pipefail
[[ "$#" -eq 0 ]] || exit 2
exec uv run --project backend python scripts/legacy-backup-operations.py scavenge
