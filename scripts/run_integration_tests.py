#!/usr/bin/env python3
"""从仓库根目录启动 test-side integration suite orchestrator。"""

from __future__ import annotations

import sys
from pathlib import Path

# ``uv run --project backend`` 安装 application package，但不会把 ``backend/tests`` 放入
# module path。此 root-level 薄入口只加入已知仓库 backend 目录，再把所有编排交给测试模块。
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_ROOT = _REPOSITORY_ROOT / "backend"
sys.path.insert(0, str(_BACKEND_ROOT))

from tests.integration.integration_suite import (
    LIFECYCLE_FILE_TARGETS,
    LIFECYCLE_NODE_TARGETS,
    REGULAR_TEST_ROOTS,
    IntegrationSuiteInvariantError,
    IntegrationSuiteLease,
    PytestInvocation,
    build_lifecycle_pytest_arguments,
    build_regular_pytest_arguments,
    main,
    orchestrate_integration_tests,
    run_pytest_child,
)

__all__ = (
    "LIFECYCLE_FILE_TARGETS",
    "LIFECYCLE_NODE_TARGETS",
    "REGULAR_TEST_ROOTS",
    "IntegrationSuiteInvariantError",
    "IntegrationSuiteLease",
    "PytestInvocation",
    "build_lifecycle_pytest_arguments",
    "build_regular_pytest_arguments",
    "main",
    "orchestrate_integration_tests",
    "run_pytest_child",
)

if __name__ == "__main__":
    raise SystemExit(main())
