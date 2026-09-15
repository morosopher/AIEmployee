#!/usr/bin/env python3
"""根目录 E2E 薄入口；数据库生命周期仅委托既有 tests 侧管理器。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

if sys.argv[1:] == ["--sensitive-output"]:
    # scanner 需要等待所有 stdout/stderr 生产者；仍复用相同 E2E 数据库与组退出边界。
    from tests.integration.sensitive_output_runner import main
elif not sys.argv[1:]:
    from tests.integration.e2e_backend import main
else:
    raise SystemExit("unsupported E2E lifecycle arguments")

if __name__ == "__main__":
    raise SystemExit(main())
