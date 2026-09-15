#!/usr/bin/env bash
set -euo pipefail
# Playwright 从 frontend/ 调用；这里只转交受管测试生命周期，不迁移输入 anchor。
# Python 通过当前 uv 环境启动无 access log 的 Uvicorn 与单 Worker，SIGTERM 后先回收
# 亲自创建的进程，再按既有 provenance/lease/完整快照规则清理临时库和角色。
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir/.."
exec uv run --project backend python scripts/run_e2e_backend.py
