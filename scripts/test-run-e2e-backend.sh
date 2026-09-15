#!/usr/bin/env bash
set -euo pipefail

# Shell 只委托唯一 Python 生命周期入口，不再直接 bootstrap/migrate 输入 anchor。
# argv、同库 checkpoint、子进程故障清理由无网络单测证明；数据库 provenance 与
# roles-absent 完整快照由实际 E2E 后紧接 audit 的发布门禁证明。
script='scripts/run-e2e-backend.sh'
! grep -Eq 'alembic|role-bootstrap|database_maintenance|dropdb|createdb|repair|stamp|downgrade' "$script"
sandbox="$(mktemp -d)"
trap 'rm -rf -- "$sandbox"' EXIT
mkdir -p "$sandbox/bin"
cat >"$sandbox/bin/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\t%s\n' "$PWD" "$*" >>"$E2E_TEST_LOG"
EOF
chmod +x "$sandbox/bin/uv"

PATH="$sandbox/bin:$PATH" E2E_TEST_LOG="$sandbox/uv.log" bash "$script"
expected="$(pwd)"$'\t''run --project backend python scripts/run_e2e_backend.py'
[[ "$(cat "$sandbox/uv.log")" == "$expected" ]]
uv run --project backend pytest backend/tests/unit/test_run_e2e_backend.py -q
