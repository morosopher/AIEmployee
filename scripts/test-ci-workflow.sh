#!/usr/bin/env bash
# 验证 CI 的合成主密钥满足应用加密边界；只检查长度与编码，不输出密钥内容。
set -euo pipefail

python3 - <<'PY'
import base64
import binascii
import re
from pathlib import Path

workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
test_recipes = Path("justfiles/test.just").read_text(encoding="utf-8")
if "backend/tests/evals" not in test_recipes:
    raise SystemExit("just ci must include the approved daily-brief evaluation suite")
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
