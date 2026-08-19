"""验证 M2 加密草稿、日历提案、审批与工具执行的数据库不变量。"""

import asyncio
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Table,
    UniqueConstraint,
    func,
    insert,
    null,
    select,
    text,
    update,
)
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.base import Executable

from ai_employee.infrastructure.db import models as db_models
from ai_employee.infrastructure.db.alembic import set_alembic_database_url
from ai_employee.infrastructure.db.base import Base
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
    MailDraftModel,
    MailDraftVersionModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.session import (
    ManagedAsyncSessionMaker,
    build_session_factory,
)
from tests.integration.alembic_commands import run_alembic_upgrade

SQLSTATE_NOT_NULL = "23502"
SQLSTATE_FOREIGN_KEY = "23503"
SQLSTATE_UNIQUE = "23505"
SQLSTATE_CHECK = "23514"
FIXED_NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)
FIXED_RETAIN_UNTIL = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class TrustedActionFacts:
    """保存行为测试复用的两用户父对象与可信操作标识。"""

    first_user_id: UUID
    second_user_id: UUID
    first_connection_id: UUID
    second_connection_id: UUID
    draft_id: UUID
    proposal_id: UUID
    task_id: UUID
    step_id: UUID
    approval_id: UUID
    tool_execution_id: UUID
    operation_id: UUID


@dataclass(frozen=True, slots=True)
class TrustedActionDatabase:
    """组合已迁移会话工厂和当前测试独占的合成父事实。"""

    session_factory: ManagedAsyncSessionMaker
    facts: TrustedActionFacts


def _synthetic_user(*, ordinal: str) -> UserModel:
    """构造不含真实个人数据、满足身份表必填字段的用户。"""
    return UserModel(
        email=f"trusted-action-{ordinal}@example.test",
        display_name=f"Trusted Action {ordinal}",
        password_hash=None,
        timezone="UTC",
        locale="zh-CN",
        brief_time=time(8, 0),
        is_active=True,
    )


async def _seed_trusted_action_facts(
    session_factory: ManagedAsyncSessionMaker,
) -> TrustedActionFacts:
    """提交一套合法最小图，供约束反例在独立事务中修改或扩展。"""
    async with session_factory.begin() as session:
        first_user = _synthetic_user(ordinal="first")
        second_user = _synthetic_user(ordinal="second")
        session.add_all((first_user, second_user))
        await session.flush()

        first_connection = OAuthConnectionModel(
            user_id=first_user.id,
            provider="google",
            provider_account_id="trusted-action-first",
            account_email="trusted-action-first@example.test",
            scopes=[],
            status="connected",
            last_error_code=None,
        )
        second_connection = OAuthConnectionModel(
            user_id=second_user.id,
            provider="microsoft",
            provider_account_id="trusted-action-second",
            provider_tenant_id="synthetic-tenant",
            account_type="work_school",
            account_email="trusted-action-second@example.test",
            scopes=[],
            status="connected",
            last_error_code=None,
        )
        session.add_all((first_connection, second_connection))
        await session.flush()

        draft = MailDraftModel(
            user_id=first_user.id,
            connection_id=first_connection.id,
            creation_idempotency_key="draft:create",
            creation_payload_hash="a" * 64,
            source_thread_id=None,
            source_message_id=None,
            mode="new",
            current_version=1,
            status="editing",
            retain_until=FIXED_RETAIN_UNTIL,
        )
        proposal = CalendarChangeProposalModel(
            user_id=first_user.id,
            connection_id=first_connection.id,
            creation_idempotency_key="proposal:create",
            creation_payload_hash="b" * 64,
            calendar_id="primary",
            operation_kind="create",
            target_event_id=None,
            base_etag=None,
            current_version=1,
            status="editing",
            retain_until=FIXED_RETAIN_UNTIL,
        )
        task = TaskRunModel(
            user_id=first_user.id,
            kind="trusted_action",
            status="created",
            idempotency_key="trusted-action:task",
            input_payload={"synthetic": True},
        )
        session.add_all((draft, proposal, task))
        await session.flush()

        step = TaskStepModel(
            task_id=task.id,
            sequence=1,
            name="trusted-action-step",
            kind="deterministic",
            status="pending",
            input_summary={},
        )
        session.add_all(
            (
                MailDraftVersionModel(
                    user_id=first_user.id,
                    draft_id=draft.id,
                    version=1,
                    to_recipients=[],
                    cc_recipients=[],
                    bcc_recipients=[],
                    subject="",
                    body_ciphertext=None,
                    body_nonce=None,
                    body_key_version=None,
                    prompt_version=None,
                    model_name=None,
                ),
                CalendarChangeSnapshotModel(
                    user_id=first_user.id,
                    proposal_id=proposal.id,
                    version=1,
                    snapshot_kind="desired",
                    content_ciphertext=None,
                    content_nonce=None,
                    content_key_version=None,
                    canonical_hash="c" * 64,
                    retain_until=FIXED_RETAIN_UNTIL,
                ),
                step,
            )
        )
        await session.flush()

        operation_id = uuid4()
        approval = ApprovalRequestModel(
            task_id=task.id,
            step_id=step.id,
            version=1,
            action="fake.write",
            payload={"synthetic": True},
            payload_hash="d" * 64,
            preview_markdown="Synthetic trusted action preview",
            status="pending",
            expires_at=FIXED_RETAIN_UNTIL,
        )
        tool_execution = ToolExecutionModel(
            task_id=task.id,
            step_id=step.id,
            tool_name="fake.write",
            idempotency_key="trusted-action:tool",
            operation_id=operation_id,
            request_payload_hash="e" * 64,
            status="pending",
        )
        session.add_all((approval, tool_execution))
        await session.flush()

        return TrustedActionFacts(
            first_user_id=first_user.id,
            second_user_id=second_user.id,
            first_connection_id=first_connection.id,
            second_connection_id=second_connection.id,
            draft_id=draft.id,
            proposal_id=proposal.id,
            task_id=task.id,
            step_id=step.id,
            approval_id=approval.id,
            tool_execution_id=tool_execution.id,
            operation_id=operation_id,
        )


@pytest.fixture
async def trusted_action_database(
    database_url: str,
) -> AsyncIterator[TrustedActionDatabase]:
    """为每个行为测试建立合法父图，并在结束后释放连接池。"""
    session_factory = build_session_factory(database_url)
    facts = await _seed_trusted_action_facts(session_factory)
    try:
        yield TrustedActionDatabase(session_factory=session_factory, facts=facts)
    finally:
        await session_factory.dispose()


def _mail_draft_values(
    facts: TrustedActionFacts,
    *,
    creation_key: str = "draft:insert",
    user_id: UUID | None = None,
    connection_id: UUID | None = None,
) -> dict[str, object]:
    """构造可直接执行 Core INSERT 的合法草稿列集合。"""
    return {
        "id": uuid4(),
        "user_id": facts.first_user_id if user_id is None else user_id,
        "connection_id": (facts.first_connection_id if connection_id is None else connection_id),
        "creation_idempotency_key": creation_key,
        "creation_payload_hash": "f" * 64,
        "source_thread_id": None,
        "source_message_id": None,
        "mode": "new",
        "current_version": 1,
        "status": "editing",
        "retain_until": FIXED_RETAIN_UNTIL,
    }


def _mail_version_values(
    facts: TrustedActionFacts,
    *,
    version: int = 2,
    user_id: UUID | None = None,
) -> dict[str, object]:
    """构造默认使用保留清理后全空 AEAD 三元组的草稿版本。"""
    return {
        "id": uuid4(),
        "user_id": facts.first_user_id if user_id is None else user_id,
        "draft_id": facts.draft_id,
        "version": version,
        "to_recipients": [],
        "cc_recipients": [],
        "bcc_recipients": [],
        "subject": "Synthetic subject",
        "body_ciphertext": None,
        "body_nonce": None,
        "body_key_version": None,
        "prompt_version": None,
        "model_name": None,
    }


def _proposal_values(
    facts: TrustedActionFacts,
    *,
    creation_key: str = "proposal:insert",
    user_id: UUID | None = None,
    connection_id: UUID | None = None,
) -> dict[str, object]:
    """构造可直接执行 Core INSERT 的合法日历提案列集合。"""
    return {
        "id": uuid4(),
        "user_id": facts.first_user_id if user_id is None else user_id,
        "connection_id": (facts.first_connection_id if connection_id is None else connection_id),
        "creation_idempotency_key": creation_key,
        "creation_payload_hash": "1" * 64,
        "calendar_id": "primary",
        "operation_kind": "create",
        "target_event_id": None,
        "base_etag": None,
        "current_version": 1,
        "status": "editing",
        "retain_until": FIXED_RETAIN_UNTIL,
    }


def _snapshot_values(
    facts: TrustedActionFacts,
    *,
    version: int = 2,
    snapshot_kind: str = "desired",
    user_id: UUID | None = None,
) -> dict[str, object]:
    """构造默认使用全空 AEAD 三元组的合法日历 snapshot。"""
    return {
        "id": uuid4(),
        "user_id": facts.first_user_id if user_id is None else user_id,
        "proposal_id": facts.proposal_id,
        "version": version,
        "snapshot_kind": snapshot_kind,
        "content_ciphertext": None,
        "content_nonce": None,
        "content_key_version": None,
        "canonical_hash": "2" * 64,
        "retain_until": FIXED_RETAIN_UNTIL,
    }


def _tool_execution_values(
    facts: TrustedActionFacts,
    *,
    idempotency_key: str = "trusted-action:tool:insert",
    operation_id: UUID | None = None,
) -> dict[str, object]:
    """构造保留 M1 必填列并显式携带 M2 operation 的工具执行。"""
    return {
        "id": uuid4(),
        "task_id": facts.task_id,
        "step_id": facts.step_id,
        "tool_name": "fake.write",
        "idempotency_key": idempotency_key,
        "operation_id": uuid4() if operation_id is None else operation_id,
        "request_payload_hash": "3" * 64,
        "status": "pending",
        "write_attempt_count": 0,
        "reconciliation_attempt_count": 0,
    }


def _integrity_error_details(
    error: IntegrityError,
) -> tuple[str | None, str | None, str | None]:
    """提取 asyncpg SQLSTATE、命名约束和 NOT NULL 列名。"""
    if error.orig is None:
        return None, None, None
    cause = error.orig.__cause__
    sqlstate = getattr(cause, "sqlstate", None)
    constraint_name = getattr(cause, "constraint_name", None)
    column_name = getattr(cause, "column_name", None)
    return (
        sqlstate if isinstance(sqlstate, str) else None,
        constraint_name if isinstance(constraint_name, str) else None,
        column_name if isinstance(column_name, str) else None,
    )


async def _assert_constraint_violation(
    session_factory: ManagedAsyncSessionMaker,
    statement: Executable,
    *,
    sqlstate: str,
    constraint_name: str | None,
    column_name: str | None = None,
) -> None:
    """执行并提交非法语句，核对 PostgreSQL 实际拒绝来源。

    事务上下文退出时才提交，因此延迟组合外键也会在 ``pytest.raises`` 范围内触发，测试
    不会把一次成功的 INSERT 误判为组合归属约束已经执行。
    """
    with pytest.raises(IntegrityError) as raised:
        async with session_factory.begin() as session:
            await session.execute(statement)

    assert _integrity_error_details(raised.value) == (
        sqlstate,
        constraint_name,
        column_name,
    )


def _registered_table(model_name: str) -> Table:
    """取得已由模型导出模块注册的表，并给缺失模型提供明确 RED 证据。

    Args:
        model_name: ``models`` 聚合模块应导出的 ORM 类名。

    Returns:
        对应 ORM 模型绑定的 SQLAlchemy 表元数据。
    """
    model = getattr(db_models, model_name, None)
    assert model is not None, f"M2 ORM model is not registered: {model_name}"
    return cast(Table, model.__table__)


def _constraint_columns(
    table: Table,
    constraint_type: type[UniqueConstraint] | type[ForeignKeyConstraint],
) -> dict[str, tuple[str, ...]]:
    """按命名约束读取本地列顺序，防止名称存在但语义退化。"""
    return {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, constraint_type) and constraint.name is not None
    }


def _check_sql(table: Table) -> dict[str, str]:
    """读取命名检查约束 SQL，验证 ORM 元数据镜像数据库安全边界。"""
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def _database_constraint_definitions(
    database_url: URL,
    constraint_names: Iterable[str],
) -> dict[str, str]:
    """从真实 PostgreSQL 读取命名约束定义，证明检查不只存在于 ORM。

    Args:
        database_url: 集成测试 fixture 创建并迁移的测试数据库 URL。
        constraint_names: 由测试代码固定声明、不会来自环境或用户输入的约束名。

    Returns:
        约束名到 PostgreSQL 规范定义的映射。
    """
    names = tuple(constraint_names)
    placeholders = ", ".join(f":name_{index}" for index in range(len(names)))
    parameters = {f"name_{index}": name for index, name in enumerate(names)}

    async def read_definitions() -> dict[str, str]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT conname, pg_catalog.pg_get_constraintdef(oid) "
                        "FROM pg_catalog.pg_constraint "
                        f"WHERE conname IN ({placeholders})"
                    ),
                    parameters,
                )
                return {str(row[0]): str(row[1]) for row in result}
        finally:
            await engine.dispose()

    return asyncio.run(read_definitions())


def test_m2_approval_model_has_encrypted_command_columns_and_keeps_json_marker() -> None:
    """M2 加密命令列必须兼容保留 M1 的非空 JSON marker。"""
    columns = ApprovalRequestModel.__table__.columns

    assert columns["payload"].nullable is False
    assert columns["payload_ciphertext"].nullable is True
    assert columns["payload_nonce"].type.length == 12
    assert columns["payload_key_version"].nullable is True
    assert columns["schema_version"].nullable is True


def test_trusted_action_models_are_registered_with_direct_non_null_user_ownership() -> None:
    """四类用户域事实必须直接带非空 ``user_id``，不能只间接依赖父对象。"""
    table_names = {
        "mail_drafts",
        "mail_draft_versions",
        "calendar_change_proposals",
        "calendar_change_snapshots",
    }

    assert table_names.issubset(Base.metadata.tables)
    for table_name in table_names:
        assert Base.metadata.tables[table_name].columns["user_id"].nullable is False


def test_trusted_action_models_define_exact_creation_version_and_operation_uniqueness() -> None:
    """创建、不可变版本与真实写 operation 必须使用精确数据库幂等键。"""
    mail_drafts = _registered_table("MailDraftModel")
    mail_versions = _registered_table("MailDraftVersionModel")
    proposals = _registered_table("CalendarChangeProposalModel")
    snapshots = _registered_table("CalendarChangeSnapshotModel")

    assert _constraint_columns(mail_drafts, UniqueConstraint) == {
        "uq_mail_drafts_id_user_id": ("id", "user_id"),
        "uq_mail_drafts_user_creation_idempotency_key": (
            "user_id",
            "creation_idempotency_key",
        ),
    }
    assert _constraint_columns(mail_versions, UniqueConstraint) == {
        "uq_mail_draft_versions_draft_version": ("draft_id", "version"),
    }
    assert _constraint_columns(proposals, UniqueConstraint) == {
        "uq_calendar_change_proposals_id_user_id": ("id", "user_id"),
        "uq_calendar_change_proposals_user_creation_idempotency_key": (
            "user_id",
            "creation_idempotency_key",
        ),
    }
    assert _constraint_columns(snapshots, UniqueConstraint) == {
        "uq_calendar_change_snapshots_proposal_version_kind": (
            "proposal_id",
            "version",
            "snapshot_kind",
        ),
    }
    assert _constraint_columns(ToolExecutionModel.__table__, UniqueConstraint) == {
        "uq_tool_executions_idempotency_key": ("idempotency_key",),
        "uq_tool_executions_operation": ("task_id", "operation_id"),
    }


def test_trusted_action_models_bind_children_and_connections_to_the_same_user() -> None:
    """组合外键必须同时携带 ``user_id``，阻止跨用户拼接连接和版本父对象。"""
    mail_drafts = _registered_table("MailDraftModel")
    mail_versions = _registered_table("MailDraftVersionModel")
    proposals = _registered_table("CalendarChangeProposalModel")
    snapshots = _registered_table("CalendarChangeSnapshotModel")

    assert _constraint_columns(mail_drafts, ForeignKeyConstraint)[
        "fk_mail_drafts_connection_user"
    ] == ("connection_id", "user_id")
    assert _constraint_columns(mail_versions, ForeignKeyConstraint)[
        "fk_mail_draft_versions_draft_user"
    ] == ("draft_id", "user_id")
    assert _constraint_columns(proposals, ForeignKeyConstraint)[
        "fk_calendar_change_proposals_connection_user"
    ] == ("connection_id", "user_id")
    assert _constraint_columns(snapshots, ForeignKeyConstraint)[
        "fk_calendar_change_snapshots_proposal_user"
    ] == ("proposal_id", "user_id")


def test_trusted_action_models_keep_sensitive_content_out_of_json_and_track_provider_results() -> (
    None
):
    """正文和 snapshot 只能落入 AEAD bytes，工具执行保留完整供应商关联字段。"""
    mail_drafts = _registered_table("MailDraftModel")
    mail_versions = _registered_table("MailDraftVersionModel")
    proposals = _registered_table("CalendarChangeProposalModel")
    snapshots = _registered_table("CalendarChangeSnapshotModel")
    approval_columns = ApprovalRequestModel.__table__.columns
    tool_columns = ToolExecutionModel.__table__.columns

    assert {
        "connection_id",
        "source_thread_id",
        "source_message_id",
        "creation_payload_hash",
        "retain_until",
    }.issubset(mail_drafts.columns.keys())
    assert {"to_recipients", "cc_recipients", "bcc_recipients"}.issubset(
        mail_versions.columns.keys()
    )
    assert "body" not in mail_versions.columns
    assert {"body_ciphertext", "body_nonce", "body_key_version"}.issubset(
        mail_versions.columns.keys()
    )
    assert {
        "connection_id",
        "calendar_id",
        "target_event_id",
        "base_etag",
        "creation_payload_hash",
        "retain_until",
    }.issubset(proposals.columns.keys())
    assert "content" not in snapshots.columns
    assert {"content_ciphertext", "content_nonce", "content_key_version"}.issubset(
        snapshots.columns.keys()
    )
    assert {
        "schema_version",
        "risk_level",
        "payload_ciphertext",
        "payload_nonce",
        "payload_key_version",
        "proposal_kind",
        "proposal_id",
        "proposal_version",
        "approved_execution_deadline_at",
    }.issubset(approval_columns.keys())
    assert {
        "operation_id",
        "provider",
        "provider_resource_id",
        "provider_request_id",
        "correlation_id",
        "claimed_at",
        "request_started_at",
        "completed_at",
        "write_attempt_count",
        "reconciliation_attempt_count",
        "last_reconciled_at",
        "manual_resolution",
        "manual_resolved_by_user_id",
        "manual_resolved_at",
    }.issubset(tool_columns.keys())


def test_orm_metadata_mirrors_aead_nonce_counter_and_manual_resolution_checks() -> None:
    """ORM 元数据必须镜像保留清理、12-byte nonce 与人工结果完整性检查。"""
    mail_checks = _check_sql(_registered_table("MailDraftVersionModel"))
    snapshot_checks = _check_sql(_registered_table("CalendarChangeSnapshotModel"))
    approval_checks = _check_sql(ApprovalRequestModel.__table__)
    tool_checks = _check_sql(ToolExecutionModel.__table__)

    assert set(mail_checks) == {
        "ck_mail_draft_versions_body_aead_all_or_none",
        "ck_mail_draft_versions_body_nonce_length_12",
    }
    assert (
        "octet_length(body_nonce) = 12"
        in mail_checks["ck_mail_draft_versions_body_nonce_length_12"]
    )
    assert set(snapshot_checks) == {
        "ck_calendar_change_snapshots_content_aead_all_or_none",
        "ck_calendar_change_snapshots_content_nonce_length_12",
    }
    assert (
        "octet_length(content_nonce) = 12"
        in snapshot_checks["ck_calendar_change_snapshots_content_nonce_length_12"]
    )
    assert set(approval_checks) == {
        "ck_approval_requests_payload_aead_all_or_none",
        "ck_approval_requests_payload_nonce_length_12",
    }
    assert (
        "octet_length(payload_nonce) = 12"
        in approval_checks["ck_approval_requests_payload_nonce_length_12"]
    )
    assert set(tool_checks) == {
        "ck_tool_executions_manual_resolution_all_or_none",
        "ck_tool_executions_manual_resolution_value",
        "ck_tool_executions_reconciliation_attempt_count_non_negative",
        "ck_tool_executions_write_attempt_count_non_negative",
    }
    assert "confirmed_executed" in tool_checks["ck_tool_executions_manual_resolution_value"]
    assert "confirmed_not_executed" in tool_checks["ck_tool_executions_manual_resolution_value"]


def _database_contains_named_aead_nonce_counter_and_manual_resolution_checks(
    empty_migration_database: URL,
) -> None:
    """空库升级后必须真实存在所有安全检查，而非只在 Python 元数据中声明。"""
    backend_root = Path(__file__).resolve().parents[3]
    alembic_config = Config(backend_root / "alembic.ini")
    set_alembic_database_url(
        alembic_config,
        empty_migration_database.render_as_string(hide_password=False),
    )
    run_alembic_upgrade(alembic_config, "head")

    expected_names = {
        "ck_mail_draft_versions_body_aead_all_or_none",
        "ck_mail_draft_versions_body_nonce_length_12",
        "ck_calendar_change_snapshots_content_aead_all_or_none",
        "ck_calendar_change_snapshots_content_nonce_length_12",
        "ck_approval_requests_payload_aead_all_or_none",
        "ck_approval_requests_payload_nonce_length_12",
        "ck_tool_executions_manual_resolution_all_or_none",
        "ck_tool_executions_manual_resolution_value",
        "ck_tool_executions_reconciliation_attempt_count_non_negative",
        "ck_tool_executions_write_attempt_count_non_negative",
    }
    definitions = _database_constraint_definitions(empty_migration_database, expected_names)

    assert set(definitions) == expected_names
    assert (
        "octet_length(body_nonce) = 12"
        in definitions["ck_mail_draft_versions_body_nonce_length_12"]
    )
    assert (
        "octet_length(content_nonce) = 12"
        in definitions["ck_calendar_change_snapshots_content_nonce_length_12"]
    )
    assert (
        "octet_length(payload_nonce) = 12"
        in definitions["ck_approval_requests_payload_nonce_length_12"]
    )
    assert "confirmed_executed" in definitions["ck_tool_executions_manual_resolution_value"]
    assert "confirmed_not_executed" in definitions["ck_tool_executions_manual_resolution_value"]


class TestEmptyTrustedActionMigration:
    """让唯一空库契约局部遮蔽共享 migration/TRUNCATE 自动 fixture。"""

    @pytest.fixture(autouse=True, name="migrated_database")
    def _without_session_migration(self) -> Iterator[None]:
        """空库用例不得先启动或改变共享 Task 13 数据库。"""
        yield

    @pytest.fixture(autouse=True, name="isolated_database")
    async def _without_application_truncate(self) -> AsyncIterator[None]:
        """空库用例只清理由自身 fixture 创建的 UUID 目标。"""
        yield

    test_database_contains_named_aead_nonce_counter_and_manual_resolution_checks = staticmethod(
        _database_contains_named_aead_nonce_counter_and_manual_resolution_checks
    )


REQUIRED_NULL_CASES = (
    *(
        ("mail_draft", column)
        for column in (
            "user_id",
            "connection_id",
            "creation_idempotency_key",
            "creation_payload_hash",
            "mode",
            "current_version",
            "status",
            "retain_until",
        )
    ),
    *(
        ("mail_version", column)
        for column in (
            "user_id",
            "draft_id",
            "version",
            "to_recipients",
            "cc_recipients",
            "bcc_recipients",
            "subject",
        )
    ),
    *(
        ("proposal", column)
        for column in (
            "user_id",
            "connection_id",
            "creation_idempotency_key",
            "creation_payload_hash",
            "calendar_id",
            "operation_kind",
            "current_version",
            "status",
            "retain_until",
        )
    ),
    *(
        ("snapshot", column)
        for column in (
            "user_id",
            "proposal_id",
            "version",
            "snapshot_kind",
            "canonical_hash",
            "retain_until",
        )
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_kind", "column_name"),
    REQUIRED_NULL_CASES,
    ids=(f"{entity_kind}-{column_name}" for entity_kind, column_name in REQUIRED_NULL_CASES),
)
async def test_required_trusted_action_columns_reject_null_in_postgresql(
    trusted_action_database: TrustedActionDatabase,
    entity_kind: str,
    column_name: str,
) -> None:
    """四类用户域表的直接归属和关键必填列必须由 PostgreSQL 真实拒绝空值。"""
    facts = trusted_action_database.facts
    if entity_kind == "mail_draft":
        table = MailDraftModel.__table__
        values = _mail_draft_values(facts)
    elif entity_kind == "mail_version":
        table = MailDraftVersionModel.__table__
        values = _mail_version_values(facts)
    elif entity_kind == "proposal":
        table = CalendarChangeProposalModel.__table__
        values = _proposal_values(facts)
    else:
        assert entity_kind == "snapshot"
        table = CalendarChangeSnapshotModel.__table__
        values = _snapshot_values(facts)
    # JSONB 默认会把 Python None 编码成 JSON null；这三个案例必须显式发送 SQL NULL，
    # 才能验证数据库列的 NOT NULL，而不是测试 JSON 编码器行为。
    values[column_name] = (
        null() if column_name in {"to_recipients", "cc_recipients", "bcc_recipients"} else None
    )

    await _assert_constraint_violation(
        trusted_action_database.session_factory,
        insert(table).values(values),
        sqlstate=SQLSTATE_NOT_NULL,
        constraint_name=None,
        column_name=column_name,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_name", "constraint_name"),
    (
        ("draft_connection", "fk_mail_drafts_connection_user"),
        ("version_draft", "fk_mail_draft_versions_draft_user"),
        ("proposal_connection", "fk_calendar_change_proposals_connection_user"),
        ("snapshot_proposal", "fk_calendar_change_snapshots_proposal_user"),
    ),
)
async def test_cross_user_trusted_action_links_are_rejected_on_commit(
    trusted_action_database: TrustedActionDatabase,
    case_name: str,
    constraint_name: str,
) -> None:
    """四条延迟组合外键必须在 commit 时拒绝跨用户连接或父实体。"""
    facts = trusted_action_database.facts
    if case_name == "draft_connection":
        statement = insert(MailDraftModel).values(
            _mail_draft_values(facts, connection_id=facts.second_connection_id)
        )
    elif case_name == "version_draft":
        statement = insert(MailDraftVersionModel).values(
            _mail_version_values(facts, user_id=facts.second_user_id)
        )
    elif case_name == "proposal_connection":
        statement = insert(CalendarChangeProposalModel).values(
            _proposal_values(facts, connection_id=facts.second_connection_id)
        )
    else:
        assert case_name == "snapshot_proposal"
        statement = insert(CalendarChangeSnapshotModel).values(
            _snapshot_values(facts, user_id=facts.second_user_id)
        )

    await _assert_constraint_violation(
        trusted_action_database.session_factory,
        statement,
        sqlstate=SQLSTATE_FOREIGN_KEY,
        constraint_name=constraint_name,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_name", "constraint_name"),
    (
        ("draft_body_partial", "ck_mail_draft_versions_body_aead_all_or_none"),
        ("draft_body_nonce", "ck_mail_draft_versions_body_nonce_length_12"),
        (
            "snapshot_content_partial",
            "ck_calendar_change_snapshots_content_aead_all_or_none",
        ),
        (
            "snapshot_content_nonce",
            "ck_calendar_change_snapshots_content_nonce_length_12",
        ),
        ("approval_payload_partial", "ck_approval_requests_payload_aead_all_or_none"),
        ("approval_payload_nonce", "ck_approval_requests_payload_nonce_length_12"),
    ),
)
async def test_aead_triples_reject_partial_or_non_12_byte_nonce_values(
    trusted_action_database: TrustedActionDatabase,
    case_name: str,
    constraint_name: str,
) -> None:
    """三类敏感内容必须真实执行 all-or-none 与 12-byte nonce 检查。"""
    facts = trusted_action_database.facts
    if case_name.startswith("draft_body"):
        values = _mail_version_values(facts)
        values.update(
            {
                "body_ciphertext": b"ciphertext",
                "body_nonce": b"n" * 11 if case_name.endswith("nonce") else None,
                "body_key_version": 1 if case_name.endswith("nonce") else None,
            }
        )
        statement = insert(MailDraftVersionModel).values(values)
    elif case_name.startswith("snapshot_content"):
        values = _snapshot_values(facts)
        values.update(
            {
                "content_ciphertext": b"ciphertext",
                "content_nonce": b"n" * 11 if case_name.endswith("nonce") else None,
                "content_key_version": 1 if case_name.endswith("nonce") else None,
            }
        )
        statement = insert(CalendarChangeSnapshotModel).values(values)
    else:
        assert case_name.startswith("approval_payload")
        statement = (
            update(ApprovalRequestModel)
            .where(ApprovalRequestModel.id == facts.approval_id)
            .values(
                payload_ciphertext=b"ciphertext",
                payload_nonce=b"n" * 11 if case_name.endswith("nonce") else None,
                payload_key_version=1 if case_name.endswith("nonce") else None,
            )
        )

    await _assert_constraint_violation(
        trusted_action_database.session_factory,
        statement,
        sqlstate=SQLSTATE_CHECK,
        constraint_name=constraint_name,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_name", "constraint_name"),
    (
        ("draft_creation", "uq_mail_drafts_user_creation_idempotency_key"),
        ("draft_version", "uq_mail_draft_versions_draft_version"),
        (
            "proposal_creation",
            "uq_calendar_change_proposals_user_creation_idempotency_key",
        ),
        (
            "snapshot_version_kind",
            "uq_calendar_change_snapshots_proposal_version_kind",
        ),
        ("tool_operation", "uq_tool_executions_operation"),
        ("tool_legacy_idempotency", "uq_tool_executions_idempotency_key"),
    ),
)
async def test_trusted_action_unique_constraints_reject_duplicate_facts(
    trusted_action_database: TrustedActionDatabase,
    case_name: str,
    constraint_name: str,
) -> None:
    """Task 5 新旧幂等与不可变版本唯一键必须由 PostgreSQL 执行。"""
    facts = trusted_action_database.facts
    if case_name == "draft_creation":
        statement = insert(MailDraftModel).values(
            _mail_draft_values(facts, creation_key="draft:create")
        )
    elif case_name == "draft_version":
        statement = insert(MailDraftVersionModel).values(_mail_version_values(facts, version=1))
    elif case_name == "proposal_creation":
        statement = insert(CalendarChangeProposalModel).values(
            _proposal_values(facts, creation_key="proposal:create")
        )
    elif case_name == "snapshot_version_kind":
        statement = insert(CalendarChangeSnapshotModel).values(
            _snapshot_values(facts, version=1, snapshot_kind="desired")
        )
    elif case_name == "tool_operation":
        statement = insert(ToolExecutionModel).values(
            _tool_execution_values(
                facts,
                idempotency_key="trusted-action:tool:duplicate-operation",
                operation_id=facts.operation_id,
            )
        )
    else:
        assert case_name == "tool_legacy_idempotency"
        statement = insert(ToolExecutionModel).values(
            _tool_execution_values(facts, idempotency_key="trusted-action:tool")
        )

    await _assert_constraint_violation(
        trusted_action_database.session_factory,
        statement,
        sqlstate=SQLSTATE_UNIQUE,
        constraint_name=constraint_name,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column_name", "constraint_name"),
    (
        ("write_attempt_count", "ck_tool_executions_write_attempt_count_non_negative"),
        (
            "reconciliation_attempt_count",
            "ck_tool_executions_reconciliation_attempt_count_non_negative",
        ),
    ),
)
async def test_tool_execution_counters_reject_negative_updates(
    trusted_action_database: TrustedActionDatabase,
    column_name: str,
    constraint_name: str,
) -> None:
    """写入与核对计数在 UPDATE/commit 边界都不得退化为负数。"""
    facts = trusted_action_database.facts
    statement = (
        update(ToolExecutionModel)
        .where(ToolExecutionModel.id == facts.tool_execution_id)
        .values({column_name: -1})
    )

    await _assert_constraint_violation(
        trusted_action_database.session_factory,
        statement,
        sqlstate=SQLSTATE_CHECK,
        constraint_name=constraint_name,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field",
    ("manual_resolution", "manual_resolved_by_user_id", "manual_resolved_at"),
)
async def test_manual_resolution_rejects_each_incomplete_audit_tuple(
    trusted_action_database: TrustedActionDatabase,
    missing_field: str,
) -> None:
    """人工结果缺 enum、actor 或 timestamp 任一字段都必须在 UPDATE/commit 时失败。"""
    facts = trusted_action_database.facts
    values: dict[str, object | None] = {
        "manual_resolution": "confirmed_executed",
        "manual_resolved_by_user_id": facts.first_user_id,
        "manual_resolved_at": FIXED_NOW,
    }
    values[missing_field] = None
    statement = (
        update(ToolExecutionModel)
        .where(ToolExecutionModel.id == facts.tool_execution_id)
        .values(values)
    )

    await _assert_constraint_violation(
        trusted_action_database.session_factory,
        statement,
        sqlstate=SQLSTATE_CHECK,
        constraint_name="ck_tool_executions_manual_resolution_all_or_none",
    )


@pytest.mark.asyncio
async def test_manual_resolution_rejects_free_text_enum_value(
    trusted_action_database: TrustedActionDatabase,
) -> None:
    """完整三元组仍不得把自由文本伪装为人工结果枚举。"""
    facts = trusted_action_database.facts
    statement = (
        update(ToolExecutionModel)
        .where(ToolExecutionModel.id == facts.tool_execution_id)
        .values(
            manual_resolution="free_text",
            manual_resolved_by_user_id=facts.first_user_id,
            manual_resolved_at=FIXED_NOW,
        )
    )

    await _assert_constraint_violation(
        trusted_action_database.session_factory,
        statement,
        sqlstate=SQLSTATE_CHECK,
        constraint_name="ck_tool_executions_manual_resolution_value",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manual_resolution",
    ("confirmed_executed", "confirmed_not_executed"),
)
async def test_legal_trusted_action_boundary_values_commit(
    trusted_action_database: TrustedActionDatabase,
    manual_resolution: str,
) -> None:
    """合法 scoped key、AEAD nonce、零计数与两种人工枚举必须能够共同提交。"""
    facts = trusted_action_database.facts
    second_user_draft = _mail_draft_values(
        facts,
        creation_key="draft:create",
        user_id=facts.second_user_id,
        connection_id=facts.second_connection_id,
    )
    second_user_proposal = _proposal_values(
        facts,
        creation_key="proposal:create",
        user_id=facts.second_user_id,
        connection_id=facts.second_connection_id,
    )
    mail_version = _mail_version_values(facts)
    mail_version.update(
        {
            "body_ciphertext": b"ciphertext",
            "body_nonce": b"n" * 12,
            "body_key_version": 1,
        }
    )
    snapshot = _snapshot_values(facts)
    snapshot.update(
        {
            "content_ciphertext": b"ciphertext",
            "content_nonce": b"n" * 12,
            "content_key_version": 1,
        }
    )

    async with trusted_action_database.session_factory.begin() as session:
        await session.execute(insert(MailDraftModel).values(second_user_draft))
        await session.execute(insert(CalendarChangeProposalModel).values(second_user_proposal))
        await session.execute(insert(MailDraftVersionModel).values(mail_version))
        await session.execute(insert(CalendarChangeSnapshotModel).values(snapshot))
        await session.execute(
            update(ApprovalRequestModel)
            .where(ApprovalRequestModel.id == facts.approval_id)
            .values(
                payload_ciphertext=b"ciphertext",
                payload_nonce=b"n" * 12,
                payload_key_version=1,
            )
        )
        await session.execute(
            update(ToolExecutionModel)
            .where(ToolExecutionModel.id == facts.tool_execution_id)
            .values(
                write_attempt_count=0,
                reconciliation_attempt_count=0,
                manual_resolution=manual_resolution,
                manual_resolved_by_user_id=facts.first_user_id,
                manual_resolved_at=FIXED_NOW,
            )
        )

    async with trusted_action_database.session_factory() as session:
        draft_count = await session.scalar(
            select(func.count())
            .select_from(MailDraftModel)
            .where(MailDraftModel.creation_idempotency_key == "draft:create")
        )
        proposal_count = await session.scalar(
            select(func.count())
            .select_from(CalendarChangeProposalModel)
            .where(CalendarChangeProposalModel.creation_idempotency_key == "proposal:create")
        )
        retained_mail_row = (
            await session.execute(
                select(
                    MailDraftVersionModel.body_ciphertext,
                    MailDraftVersionModel.body_nonce,
                    MailDraftVersionModel.body_key_version,
                ).where(
                    MailDraftVersionModel.draft_id == facts.draft_id,
                    MailDraftVersionModel.version == 1,
                )
            )
        ).one()
        approval_row = (
            await session.execute(
                select(
                    ApprovalRequestModel.payload_ciphertext,
                    ApprovalRequestModel.payload_nonce,
                    ApprovalRequestModel.payload_key_version,
                ).where(ApprovalRequestModel.id == facts.approval_id)
            )
        ).one()
        tool_row = (
            await session.execute(
                select(
                    ToolExecutionModel.write_attempt_count,
                    ToolExecutionModel.reconciliation_attempt_count,
                    ToolExecutionModel.manual_resolution,
                    ToolExecutionModel.manual_resolved_by_user_id,
                    ToolExecutionModel.manual_resolved_at,
                ).where(ToolExecutionModel.id == facts.tool_execution_id)
            )
        ).one()

    assert draft_count == 2
    assert proposal_count == 2
    assert tuple(retained_mail_row) == (None, None, None)
    assert tuple(approval_row) == (b"ciphertext", b"n" * 12, 1)
    assert tuple(tool_row) == (
        0,
        0,
        manual_resolution,
        facts.first_user_id,
        FIXED_NOW,
    )
