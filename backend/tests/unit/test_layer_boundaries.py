"""验证后台执行代码遵守 Worker → Application → Domain 的固定依赖方向。"""

import ast
from pathlib import Path

import pytest

BACKEND_SOURCE = Path(__file__).resolve().parents[2] / "src" / "ai_employee"


def _imported_modules(path: Path) -> set[str]:
    """解析模块的直接 import，不执行会初始化 broker 或数据库配置的代码。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


@pytest.mark.parametrize("module_name", ["execute_task.py", "outbox.py", "schedules.py"])
def test_worker_modules_do_not_own_sqlalchemy_queries(module_name: str) -> None:
    """Worker composition root 可选适配器，但不得直接依赖 SQLAlchemy 或 ORM model。"""
    imports = _imported_modules(BACKEND_SOURCE / "workers" / module_name)
    forbidden = {
        name
        for name in imports
        if name == "sqlalchemy"
        or name.startswith(("sqlalchemy.", "ai_employee.infrastructure.db.models"))
    }

    assert forbidden == set()


def test_queue_enqueuer_does_not_import_worker_entrypoint() -> None:
    """Queue adapter 必须通过窄 sender 注入，不能反向硬依赖 Worker composition root。"""
    imports = _imported_modules(BACKEND_SOURCE / "infrastructure" / "queue" / "enqueue.py")

    assert not any(name.startswith("ai_employee.workers") for name in imports)


def test_background_orchestration_and_sql_stores_have_explicit_layer_modules() -> None:
    """应用编排与 SQLAlchemy stores 必须分别位于 Application 和 Infrastructure。"""
    expected = (
        BACKEND_SOURCE / "application" / "use_cases" / "task_execution.py",
        BACKEND_SOURCE / "application" / "use_cases" / "outbox.py",
        BACKEND_SOURCE / "infrastructure" / "db" / "repositories" / "task_execution.py",
        BACKEND_SOURCE / "infrastructure" / "db" / "repositories" / "outbox.py",
    )

    assert all(path.is_file() for path in expected)


def test_application_layer_does_not_import_sqlalchemy_taskiq_or_redis() -> None:
    """Application 用例与端口必须保持供应商无关，不能依赖持久化或队列 SDK。"""
    forbidden_prefixes = ("sqlalchemy", "taskiq", "taskiq_redis", "redis")
    violations: dict[str, list[str]] = {}
    for path in (BACKEND_SOURCE / "application").rglob("*.py"):
        imports = sorted(
            name for name in _imported_modules(path) if name.startswith(forbidden_prefixes)
        )
        if imports:
            violations[str(path.relative_to(BACKEND_SOURCE))] = imports

    assert violations == {}


def test_application_and_domain_do_not_add_provider_adapters_or_sdks() -> None:
    """应用/领域层不得导入任何供应商 adapter 或 SDK。"""
    forbidden_prefixes = (
        "ai_employee.integrations.google",
        "ai_employee.integrations.microsoft",
        "google",
        "googleapiclient",
        "msal",
        "azure",
        "msgraph",
    )
    violations: dict[str, list[str]] = {}
    for layer_name in ("application", "domain"):
        for path in (BACKEND_SOURCE / layer_name).rglob("*.py"):
            imports = sorted(
                name for name in _imported_modules(path) if name.startswith(forbidden_prefixes)
            )
            if imports:
                violations[str(path.relative_to(BACKEND_SOURCE))] = imports

    assert violations == {}
