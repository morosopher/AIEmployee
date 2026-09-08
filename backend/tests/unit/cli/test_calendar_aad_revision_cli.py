"""验证固定 revision screening CLI 的 Secret authority、无参数和单连接组合边界。"""

import importlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from sqlalchemy.pool import NullPool

from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError
from ai_employee.cli.database_maintenance import _load_database_endpoint


@pytest.mark.parametrize("revision", ["20260809_0018", "20260809_0019"])
def test_calendar_aad_revision_cli_uses_only_fixed_owner_secret(monkeypatch, capsys, revision):
    """PGPASSWORD 不能替代安全 Secret reader；只创建一个 bounded NullPool 目标 engine。"""
    module = importlib.import_module("ai_employee.cli.calendar_aad_revision_0019")
    endpoint = _load_database_endpoint(
        {"DATABASE_URL": "postgresql+psycopg://synthetic_owner@postgres:5432/synthetic_test"}
    )
    monkeypatch.setenv("PGPASSWORD", "synthetic-forbidden-environment-secret")
    monkeypatch.setattr(module, "_load_database_endpoint", lambda: endpoint)
    secret_reads = []

    def secret_reader(path):
        """只在 Secret I/O 边界提供合成内容，记录固定 owner 路径是否被消费。"""
        secret_reads.append(path)
        return SecretStr("synthetic-fixed-secret")

    monkeypatch.setattr(module, "_read_secret_file", secret_reader)
    engine = Mock()
    create = Mock(return_value=engine)
    monkeypatch.setattr(module, "create_engine", create)
    read = Mock(return_value=revision)
    monkeypatch.setattr(module, "read_rollout_revision", read)
    assert module.main([]) == 0
    assert secret_reads == [Path("/run/secrets/postgres_bootstrap_password")]
    create.assert_called_once()
    url = create.call_args.args[0]
    assert url == endpoint.owner_url(
        SecretStr("synthetic-fixed-secret"), database_name="synthetic_test"
    )
    assert create.call_args.kwargs == {
        "poolclass": NullPool,
        "hide_parameters": True,
        "connect_args": {"connect_timeout": 10, "options": "-c statement_timeout=10000"},
    }
    read.assert_called_once()
    assert read.call_args.args == (engine,)
    assert read.call_args.kwargs["authority"].head_revision == "20260809_0019"
    engine.dispose.assert_called_once()
    assert capsys.readouterr() == (revision + "\n", "")


def test_calendar_aad_revision_cli_rejects_arguments_before_secret(monkeypatch, capsys):
    """内部 screen 也不接受 revision、override 或 scope 参数，且不回显原始输入。"""
    module = importlib.import_module("ai_employee.cli.calendar_aad_revision_0019")
    secret_reader = Mock(side_effect=AssertionError("Secret access is forbidden"))
    monkeypatch.setattr(module, "_read_secret_file", secret_reader)
    assert module.main(["--revision", "synthetic-private-value"]) == 1
    secret_reader.assert_not_called()
    assert capsys.readouterr() == ("", "calendar_aad_arguments_invalid\n")


def test_calendar_aad_revision_cli_disposes_engine_and_redacts_read_failure(monkeypatch, capsys):
    """未知数据库异常只返回稳定码；失败后释放唯一 engine，不创建维护/repair 路径。"""
    module = importlib.import_module("ai_employee.cli.calendar_aad_revision_0019")
    endpoint = _load_database_endpoint(
        {"DATABASE_URL": "postgresql+psycopg://synthetic_owner@postgres:5432/synthetic_test"}
    )
    monkeypatch.setattr(module, "_load_database_endpoint", lambda: endpoint)
    monkeypatch.setattr(
        module, "_read_secret_file", lambda path: SecretStr("synthetic-fixed-secret")
    )
    engine = Mock()
    monkeypatch.setattr(module, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(
        module,
        "read_rollout_revision",
        Mock(side_effect=RuntimeError("synthetic-private-diagnostic")),
    )
    assert module.main([]) == 1
    engine.dispose.assert_called_once()
    assert capsys.readouterr() == ("", "calendar_aad_revision_screen_failed\n")


def test_calendar_aad_revision_reader_rejects_other_published_revisions(monkeypatch):
    """published 链上的其它合法版本也不能被 screen 当作可接受的 0018/0019。"""
    module = importlib.import_module("ai_employee.cli.calendar_aad_revision_0019")
    from alembic.config import Config

    authority = module.load_published_alembic_authority(Config(module._CONFIG_PATH))
    engine = Mock()
    connection = Mock()
    connection.execution_options.return_value = connection
    engine.connect.return_value.__enter__ = Mock(return_value=connection)
    engine.connect.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(module, "read_current_alembic_revision", lambda *args, **kwargs: "base")
    with pytest.raises(CalendarAadRolloutError) as failure:
        module.read_rollout_revision(engine, authority=authority)
    assert failure.value.error_code == "calendar_aad_revision_mismatch"
    connection.execution_options.assert_called_once_with(
        isolation_level="REPEATABLE READ", postgresql_readonly=True
    )
