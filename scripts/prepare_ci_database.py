#!/usr/bin/env python3
"""CI 新服务的测试基线薄入口；真实构造只委托专用 tests 模块。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from tests.integration.ci_database import main

if __name__ == "__main__":
    if sys.argv[1:]:
        raise SystemExit("CI database preparation accepts no command arguments")
    raise SystemExit(main())
