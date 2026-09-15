#!/usr/bin/env bash
# 验证 CI 到真实评估选择的接线及合成主密钥边界；不执行测试或输出密钥内容。
set -euo pipefail

uv run --project backend python - <<'PY'
import base64
import binascii
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path("backend").resolve()))
from tests.integration.integration_suite import build_regular_pytest_arguments

workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
# 评估由唯一 integration orchestrator 的 regular child 收集。只检查 recipe 文本会
# 错拒该间接调用，因此同时核对 just 的实际展开命令与正式 builder，不接受注释占位。
expanded = subprocess.run(
    ["just", "--color", "never", "--dry-run", "ci"], capture_output=True, text=True, check=True,
)
commands = [shlex.split(line, comments=True) for line in (expanded.stdout + expanded.stderr).splitlines()]
integration_command = ["uv", "run", "--project", "backend", "python", "scripts/run_integration_tests.py"]
arguments = build_regular_pytest_arguments()
evaluation = Path("backend/tests/evals")
ignored = [Path(value.removeprefix("--ignore=")) for value in arguments if value.startswith("--ignore=")]
if (integration_command not in commands or str(evaluation) not in arguments
    or any(evaluation.is_relative_to(path) or path.is_relative_to(evaluation) for path in ignored)):
    raise SystemExit("just ci must include the approved daily-brief evaluation suite")
# 新 runner 的显式准备必须经过根 recipe 到唯一 tests 薄入口；不能只留下同名注释。
prepared = subprocess.run(
    ["just", "--color", "never", "--dry-run", "prepare-ci-database"], capture_output=True, text=True, check=True,
)
preparation_commands = [shlex.split(line, comments=True) for line in (prepared.stdout + prepared.stderr).splitlines()]
if ["uv", "run", "--project", "backend", "python", "scripts/prepare_ci_database.py"] not in preparation_commands:
    raise SystemExit("CI must use the approved explicit database preparation recipe")
generated = 'openssl rand -base64 32 >"${secret_dir}/app_master_key"'
if generated in workflow:
    raise SystemExit(0)

match = re.search(
    r"printf '%s' '([^']+)' >\"\$\{secret_dir\}/app_master_key\"",
    workflow,
)
if match is None:
    raise SystemExit("CI workflow must create an app_master_key secret file")
try:
    decoded = base64.urlsafe_b64decode(match.group(1).encode("ascii"))
except (UnicodeEncodeError, binascii.Error, ValueError) as error:
    raise SystemExit("CI app_master_key must contain valid Base64URL data") from error
if len(decoded) != 32:
    raise SystemExit("CI app_master_key must decode to exactly 32 bytes")
PY
