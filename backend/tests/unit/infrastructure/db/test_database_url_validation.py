"""验证集成测试数据库 URL 在任何连接或迁移之前被拒绝危险目标。"""

import importlib.util
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from ai_employee.infrastructure.db.database_url import InvalidTestDatabaseUrl

VALID_TEST_DATABASE_URL = (
    "postgresql+asyncpg://synthetic_user:synthetic_password@127.0.0.1:15432/synthetic_test"
)


@lru_cache
def _load_integration_conftest() -> ModuleType:
    """加载集成测试 guard 模块而不触发该目录的 autouse 数据库 fixture。"""
    backend_root = Path(__file__).resolve().parents[4]
    conftest_path = backend_root / "tests" / "integration" / "conftest.py"
    spec = importlib.util.spec_from_file_location("task3_integration_conftest", conftest_path)
    if spec is None or spec.loader is None:
        raise AssertionError("integration conftest module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_fixture_url(monkeypatch: pytest.MonkeyPatch, value: str) -> str:
    """直接调用会话级 fixture 的实现，隔离环境变量边界而不启动 pytest fixture。"""
    monkeypatch.setenv("TEST_DATABASE_URL", value)
    return cast(str, _load_integration_conftest().database_url.__wrapped__())


@pytest.mark.parametrize(
    "query",
    ("host=remote.invalid", "database=other_test", "user=other_user"),
)
def test_rejects_query_target_overrides_before_connection_creation(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    """asyncpg 可能把 query 映射为连接参数，测试 guard 必须在构造引擎前拒绝它。"""
    integration_conftest = _load_integration_conftest()
    called = False

    def fail_if_constructed(database_url: str) -> object:
        nonlocal called
        called = True
        raise AssertionError("engine construction must not run for an invalid URL")

    monkeypatch.setattr(integration_conftest, "build_engine", fail_if_constructed)
    monkeypatch.setenv("TEST_DATABASE_URL", f"{VALID_TEST_DATABASE_URL}?{query}")

    with pytest.raises(pytest.UsageError):
        integration_conftest.database_url.__wrapped__()

    assert called is False


@pytest.mark.parametrize(
    "value",
    (
        "postgresql+asyncpg://synthetic_user:synthetic_password@203.0.113.10:15432/synthetic_test",
        "postgresql+asyncpg://synthetic_user:synthetic_password@127.0.0.1/synthetic_test",
        "postgresql+asyncpg://synthetic_user:synthetic_password@127.0.0.1:15432/production_db",
        "postgresql://synthetic_user:synthetic_password@127.0.0.1:15432/synthetic_test",
        "postgresql+asyncpg://:synthetic_password@127.0.0.1:15432/synthetic_test",
        "postgresql+asyncpg://synthetic_user:synthetic_password@127.0.0.1:15432",
        "postgresql+asyncpg://synthetic_user:synthetic_password@127.0.0.1:15432/synthetic_test#fragment",
        "postgresql+asyncpg://synthetic_user:synthetic_password@127.0.0.1:15432/synthetic_test?",
        "postgresql+asyncpg://synthetic_user:synthetic_password@127.0.0.1:15432/synthetic_test\n",
        "postgresql+asyncpg://synthetic_user:synthetic_password@127.0.0.1:15432/synthetic_test\t",
        "postgresql+asyncpg://synthetic_user:synthetic_password@127.0.0.1:15432/synthetic_test\r",
        "postgresql+asyncpg://synthetic_user:synthetic_password@127.0.0.1:not-a-port/synthetic_test",
        "not-a-database-url",
    ),
)
def test_rejects_unsafe_test_database_urls(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """缺少关键 authority、错误 driver、远端目标或控制字符都必须 fail closed。"""
    with pytest.raises(pytest.UsageError) as error:
        _load_fixture_url(monkeypatch, value)

    assert "synthetic_password" not in str(error.value)


def test_accepts_explicit_local_test_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """评审指定的本地测试端点保持可用。"""
    assert _load_fixture_url(monkeypatch, VALID_TEST_DATABASE_URL) == VALID_TEST_DATABASE_URL


def test_rejects_remote_target_before_alembic_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """远端目标在创建 Alembic Config 或引擎前就应被拒绝。"""
    integration_conftest = _load_integration_conftest()
    constructed = False

    def fail_if_constructed(*args: object, **kwargs: object) -> object:
        nonlocal constructed
        constructed = True
        raise AssertionError("invalid URL reached an infrastructure constructor")

    monkeypatch.setattr(integration_conftest, "Config", fail_if_constructed)
    monkeypatch.setattr(integration_conftest, "build_engine", fail_if_constructed)
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://synthetic_user:synthetic_password@203.0.113.10:15432/synthetic_test",
    )

    with pytest.raises(InvalidTestDatabaseUrl):
        next(
            integration_conftest.migrated_database.__wrapped__(
                integration_conftest.TestDatabaseUrl(
                    "postgresql+asyncpg://synthetic_user:synthetic_password@203.0.113.10:15432/synthetic_test"
                )
            )
        )

    assert constructed is False
