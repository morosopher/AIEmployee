"""用真实 PostgreSQL 冻结 M2 内容保留、审计例外和清理事务边界。

所有字段均为合成数据；断言只比较状态、空值、哈希和标识，不解密任何正文。共享 seed
也供 source-cache 与真实 retention 登录测试使用，避免各入口拥有不同清理事实。
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, update

from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
    MailDraftModel,
    MailDraftVersionModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.privacy_checkpoints import (
    PostgresPrivacyCheckpointCleaner,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.workers.retention import RetentionCleanupWorker
from tests.unit.application.test_oauth_refresh_coordinator import (
    recovery_started,
    result_event,
    started,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
OLD = NOW - timedelta(days=40)


@pytest.mark.asyncio
async def test_task27e_disconnected_cleanup_cannot_delete_a_concurrent_reconnection(
    database_url: str,
) -> None:
    """retention 旧快照看到 disconnected，另连接重连提交后不能继续按旧谓词删除新凭据。"""
    sessions = build_session_factory(database_url)
    retention = build_session_factory(database_url)
    delete_started = asyncio.Event()
    job: asyncio.Task[None] | None = None

    def observe_delete(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        """仅以 SQL 动作调度旧实现的阻塞点，不记录参数或替换实际数据库执行。"""
        del connection, cursor, parameters, context, executemany
        if statement.startswith("DELETE FROM encrypted_credentials"):
            delete_started.set()

    event.listen(retention.engine.sync_engine, "before_cursor_execute", observe_delete)
    try:
        seed = await seed_lifecycle_action(sessions, retained=True)
        credential_id = uuid4()
        async with sessions.begin() as session:
            await session.execute(
                update(OAuthConnectionModel)
                .where(OAuthConnectionModel.id == seed.connection_id)
                .values(status="disconnected")
            )
            session.add(
                EncryptedCredentialModel(
                    id=credential_id,
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    credential_kind="access_token",
                    ciphertext=b"synthetic-old",
                    nonce=b"123456789012",
                    key_version=1,
                    token_expires_at=NOW + timedelta(hours=1),
                )
            )
        async with asyncio.timeout(15):
            async with sessions.begin() as writer:
                connection = await writer.scalar(
                    select(OAuthConnectionModel)
                    .where(
                        OAuthConnectionModel.user_id == seed.user_id,
                        OAuthConnectionModel.id == seed.connection_id,
                    )
                    .with_for_update()
                )
                credential = await writer.scalar(
                    select(EncryptedCredentialModel)
                    .where(
                        EncryptedCredentialModel.user_id == seed.user_id,
                        EncryptedCredentialModel.id == credential_id,
                    )
                    .with_for_update()
                )
                assert connection is not None and credential is not None
                connection.status = "connected"
                credential.ciphertext = b"synthetic-new"
                await writer.flush()
                job = asyncio.create_task(
                    RetentionCleanupWorker(retention)._clean_disconnected_credentials(
                        seed.user_id,
                        batch_size=1,
                    )
                )
                observed = asyncio.create_task(delete_started.wait())
                await asyncio.wait((job, observed), return_when=asyncio.FIRST_COMPLETED)
                observed.cancel()
                await asyncio.gather(observed, return_exceptions=True)
            await job
        async with sessions() as session:
            credential = await session.get(EncryptedCredentialModel, credential_id)
            assert credential is not None
            assert credential.ciphertext == b"synthetic-new"
    finally:
        if job is not None:
            await asyncio.gather(job, return_exceptions=True)
        event.remove(retention.engine.sync_engine, "before_cursor_execute", observe_delete)
        await retention.dispose()
        await sessions.dispose()


@dataclass(frozen=True, slots=True)
class LifecycleSeed:
    """只向调用方返回主键，敏感合成内容不离开 fixture 写入边界。"""

    user_id: UUID
    connection_id: UUID
    action_id: UUID
    task_id: UUID
    approval_id: UUID
    content_id: UUID
    execution_id: UUID
    event_id: UUID


async def seed_lifecycle_action(
    sessions: ManagedAsyncSessionMaker,
    *,
    kind: Literal["mail", "calendar"] = "mail",
    execution_status: str | None = None,
    binding: Literal["none", "thread", "message", "event"] = "none",
    event_ends_at: datetime | None = None,
    metadata_days: int = 365,
    retained: bool = False,
    user_id: UUID | None = None,
) -> LifecycleSeed:
    """创建完整用户→连接→动作→任务/审批/执行图，显式 flush 每个父层。

    Args:
        sessions: 当前隔离数据库的会话工厂。
        kind: 两种固定 M2 内容聚合，不支持其他动作。
        execution_status: None 表示尚未认领；否则创建最小可信执行事实。
        binding: 按冻结字段区分独立动作与来源绑定动作。
        event_ends_at: 精确来源事件结束时刻；None 表示无可用事件。
        metadata_days: 拉长来源元数据保留，以单独观察四列内容清理。
        retained: 另一用户的仍有效内容，用于隔离断言。
    """
    seed = LifecycleSeed(user_id or uuid4(), *(uuid4() for _ in range(7)))
    operation_id = uuid4()
    async with sessions.begin() as session:
        if user_id is None:
            session.add(
                UserModel(
                    id=seed.user_id,
                    email=f"{seed.user_id}@example.test",
                    display_name="Synthetic",
                    password_hash=None,
                    timezone="UTC",
                    locale="zh-CN",
                    brief_time=time(8),
                    is_active=True,
                    source_metadata_retention_days=metadata_days,
                )
            )
        await session.flush()
        session.add(
            OAuthConnectionModel(
                id=seed.connection_id,
                user_id=seed.user_id,
                provider="google",
                provider_account_id=str(seed.connection_id),
                account_email="synthetic@example.test",
                scopes=[],
                status="connected",
            )
        )
        await session.flush()
        deadline = NOW + timedelta(days=1) if retained else NOW - timedelta(seconds=1)
        action_status = "executing" if execution_status else "awaiting_approval"
        if execution_status == "succeeded":
            action_status = "sent" if kind == "mail" else "applied"
        common = {
            "id": seed.action_id,
            "user_id": seed.user_id,
            "connection_id": seed.connection_id,
            "creation_idempotency_key": str(seed.action_id),
            "creation_payload_hash": "a" * 64,
            "current_version": 1,
            "status": action_status,
            "retain_until": deadline,
            "created_at": OLD,
            "updated_at": OLD,
        }
        if kind == "mail":
            session.add(
                MailDraftModel(
                    **common,
                    mode="new" if binding == "none" else "reply",
                    source_thread_id="thread-synthetic" if binding == "thread" else None,
                    source_message_id="message-synthetic" if binding == "message" else None,
                )
            )
        else:
            session.add(
                CalendarChangeProposalModel(
                    **common,
                    calendar_id="primary",
                    operation_kind="create" if binding == "none" else "update",
                    target_event_id="event-synthetic" if binding == "event" else None,
                    base_etag=None,
                )
            )
        session.add(
            TaskRunModel(
                id=seed.task_id,
                user_id=seed.user_id,
                kind="trusted_action",
                status="succeeded" if execution_status == "succeeded" else "running",
                idempotency_key=str(seed.task_id),
                input_payload={
                    "approval_id": str(seed.approval_id),
                    "operation_id": str(operation_id),
                },
                started_at=OLD,
                finished_at=NOW if execution_status == "succeeded" else None,
                attempt_count=1,
                lease_owner="synthetic-worker",
                lease_expires_at=NOW + timedelta(minutes=5),
                scheduled_for=NOW,
                retry_recovery_at=NOW,
                approval_checkpoint_recovery_at=NOW,
            )
        )
        await session.flush()
        if kind == "mail":
            session.add(
                MailDraftVersionModel(
                    id=seed.content_id,
                    user_id=seed.user_id,
                    draft_id=seed.action_id,
                    version=1,
                    to_recipients=[{"email": "recipient@example.test"}],
                    cc_recipients=[],
                    bcc_recipients=[],
                    subject="Synthetic",
                    body_ciphertext=b"synthetic-body",
                    body_nonce=b"123456789012",
                    body_key_version=1,
                    created_at=OLD,
                )
            )
        else:
            session.add(
                CalendarChangeSnapshotModel(
                    id=seed.content_id,
                    user_id=seed.user_id,
                    proposal_id=seed.action_id,
                    version=1,
                    snapshot_kind="desired",
                    content_ciphertext=b"synthetic-calendar",
                    content_nonce=b"123456789012",
                    content_key_version=1,
                    canonical_hash="c" * 64,
                    retain_until=deadline,
                    created_at=OLD,
                )
            )
            if event_ends_at is not None:
                session.add(
                    CalendarEventModel(
                        id=seed.event_id,
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        calendar_id="primary",
                        provider_event_id="event-synthetic",
                        title="Synthetic",
                        description_ciphertext=b"synthetic-description",
                        description_nonce=b"123456789012",
                        description_key_version=1,
                        description_aad_version=2,
                        location_ciphertext=b"synthetic-location",
                        location_nonce=b"123456789012",
                        location_key_version=1,
                        location_aad_version=2,
                        starts_at=event_ends_at - timedelta(hours=1),
                        ends_at=event_ends_at,
                        all_day=False,
                        transparency="opaque",
                        status="confirmed",
                        timezone="UTC",
                        provider_url="https://example.test/event",
                    )
                )
        step_id = uuid4()
        session.add(
            TaskStepModel(
                id=step_id,
                task_id=seed.task_id,
                sequence=1,
                name="approve",
                kind="trusted_action",
                status="running",
                input_summary={},
            )
        )
        await session.flush()
        action = (
            "mail.send"
            if kind == "mail"
            else "calendar.create"
            if binding == "none"
            else "calendar.update"
        )
        session.add(
            ApprovalRequestModel(
                id=seed.approval_id,
                task_id=seed.task_id,
                step_id=step_id,
                version=1,
                action=action,
                payload={"storage": "encrypted"},
                payload_ciphertext=b"synthetic-command",
                payload_nonce=b"123456789012",
                payload_key_version=1,
                payload_hash="d" * 64,
                proposal_kind="mail_draft" if kind == "mail" else "calendar_proposal",
                schema_version=f"{action.replace('.', '_')}.v1",
                proposal_id=seed.action_id,
                proposal_version=1,
                preview_markdown="",
                status="approved",
                expires_at=NOW + timedelta(minutes=10),
            )
        )
        if execution_status is not None:
            session.add(
                ToolExecutionModel(
                    id=seed.execution_id,
                    task_id=seed.task_id,
                    step_id=step_id,
                    tool_name=action,
                    idempotency_key=f"{action}:{seed.task_id}:{seed.approval_id}:1:{operation_id}",
                    operation_id=operation_id,
                    request_payload_hash="d" * 64,
                    provider="google",
                    provider_resource_id="synthetic-resource",
                    provider_request_id="synthetic-request",
                    correlation_id="synthetic-correlation",
                    status=execution_status,
                    error_code=None,
                    result_summary={"check_url": "https://example.test/check"},
                    request_started_at=OLD,
                    claimed_at=OLD,
                    write_attempt_count=1,
                    reconciliation_attempt_count=3,
                    last_reconciled_at=OLD,
                )
            )
    return seed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mail", "calendar"])
@pytest.mark.parametrize("execution_status", [None, "claimed", "reconciling", "succeeded"])
async def test_task27e_retention_atomically_redacts_action_and_approval(
    database_url: str,
    kind: Literal["mail", "calendar"],
    execution_status: str | None,
) -> None:
    """三种认领结论各自收敛，哈希/外部结果不变，另一用户有效正文完整保留。"""
    sessions = build_session_factory(database_url)
    try:
        seed = await seed_lifecycle_action(sessions, kind=kind, execution_status=execution_status)
        other = await seed_lifecycle_action(sessions, kind=kind, retained=True)
        await RetentionCleanupWorker(sessions).execute(now=NOW, batch_size=1)
        async with sessions() as session:
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            task = await session.get(TaskRunModel, seed.task_id)
            assert approval is not None and task is not None
            assert (
                approval.payload_ciphertext,
                approval.payload_nonce,
                approval.payload_key_version,
            ) == (None, None, None)
            assert approval.payload_hash == "d" * 64
            model = MailDraftModel if kind == "mail" else CalendarChangeProposalModel
            action = await session.get(model, seed.action_id)
            assert action is not None
            if kind == "mail":
                content = await session.get(MailDraftVersionModel, seed.content_id)
                assert content is not None
                assert (content.body_ciphertext, content.body_nonce, content.body_key_version) == (
                    None,
                    None,
                    None,
                )
            else:
                snapshot = await session.get(CalendarChangeSnapshotModel, seed.content_id)
                assert snapshot is not None
                assert (
                    snapshot.content_ciphertext,
                    snapshot.content_nonce,
                    snapshot.content_key_version,
                ) == (None, None, None)
                assert snapshot.canonical_hash == "c" * 64
            if execution_status is None:
                assert approval.status == "invalidated"
                assert action.status == task.status == "cancelled"
                assert task.error_code == "action_content_expired"
                assert (
                    task.scheduled_for,
                    task.retry_recovery_at,
                    task.approval_checkpoint_recovery_at,
                    task.lease_owner,
                    task.lease_expires_at,
                ) == (None,) * 5
            else:
                execution = await session.get(ToolExecutionModel, seed.execution_id)
                assert execution is not None
                if execution_status == "succeeded":
                    assert execution.status == task.status == "succeeded"
                    assert execution.error_code is None
                    assert action.status == ("sent" if kind == "mail" else "applied")
                else:
                    assert execution.status == action.status == task.status == "needs_attention"
                    assert execution.error_code == task.error_code == "action_content_expired"
                assert (
                    execution.provider_resource_id,
                    execution.provider_request_id,
                    execution.correlation_id,
                ) == (
                    "synthetic-resource",
                    "synthetic-request",
                    "synthetic-correlation",
                )
                assert execution.result_summary == {"check_url": "https://example.test/check"}
                assert execution.reconciliation_attempt_count == 3
                assert execution.write_attempt_count == 1
            other_approval = await session.get(ApprovalRequestModel, other.approval_id)
            assert other_approval is not None and other_approval.payload_ciphertext is not None
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("age_days", [179, 181])
@pytest.mark.parametrize("execution_status", [None, "reconciling", "succeeded"])
async def test_task27e_mail_metadata_is_removed_after_last_edit_retention(
    database_url: str,
    age_days: int,
    execution_status: str | None,
) -> None:
    """180天后删除不可更新的地址/主题版本；人工待核对执行和精确审批哈希仍保留。"""
    sessions = build_session_factory(database_url)
    try:
        seed = await seed_lifecycle_action(
            sessions, execution_status=execution_status, metadata_days=180
        )
        async with sessions.begin() as session:
            await session.execute(
                update(MailDraftVersionModel)
                .where(MailDraftVersionModel.id == seed.content_id)
                .values(
                    created_at=NOW - timedelta(days=age_days),
                )
            )
        await RetentionCleanupWorker(sessions).execute(now=NOW, batch_size=1)
        async with sessions() as session:
            content = await session.get(MailDraftVersionModel, seed.content_id)
            assert (content is None) is (age_days > 180)
            assert await session.get(MailDraftModel, seed.action_id) is not None
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            assert approval is not None and approval.payload_hash == "d" * 64
            if execution_status is not None:
                execution = await session.get(ToolExecutionModel, seed.execution_id)
                assert (
                    execution is not None and execution.provider_resource_id == "synthetic-resource"
                )
                assert execution.result_summary == {"check_url": "https://example.test/check"}
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mail", "calendar"])
async def test_task27e_expired_terminal_history_deletes_action_children_before_parent(
    database_url: str,
    kind: Literal["mail", "calendar"],
) -> None:
    """365天终态历史清理不能遗留无审批引用的聚合和快照/版本。"""
    sessions = build_session_factory(database_url)
    try:
        seed = await seed_lifecycle_action(sessions, kind=kind, execution_status="succeeded")
        async with sessions.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == seed.task_id)
                .values(finished_at=NOW - timedelta(days=400))
            )
        await RetentionCleanupWorker(
            sessions,
            checkpoint_cleaner=PostgresPrivacyCheckpointCleaner(database_url),
        ).execute(now=NOW, batch_size=1)
        async with sessions() as session:
            for model, identity in (
                (TaskRunModel, seed.task_id),
                (ApprovalRequestModel, seed.approval_id),
                (ToolExecutionModel, seed.execution_id),
                (MailDraftModel if kind == "mail" else CalendarChangeProposalModel, seed.action_id),
                (
                    MailDraftVersionModel if kind == "mail" else CalendarChangeSnapshotModel,
                    seed.content_id,
                ),
            ):
                assert await session.get(model, identity) is None
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_task27e_calendar_event_end_controls_snapshot_and_four_column_content(
    database_url: str,
    expired: bool,
) -> None:
    """事件结束+180天优先于提案时间；到期四列同时清空而非留下 AAD version。"""
    sessions = build_session_factory(database_url)
    try:
        seed = await seed_lifecycle_action(
            sessions,
            kind="calendar",
            binding="event",
            event_ends_at=NOW - timedelta(days=181 if expired else 179),
        )
        await RetentionCleanupWorker(sessions).execute(now=NOW, batch_size=1)
        async with sessions() as session:
            event = await session.get(CalendarEventModel, seed.event_id)
            snapshot = await session.get(CalendarChangeSnapshotModel, seed.content_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            assert event is not None and snapshot is not None and approval is not None
            for field in ("description", "location"):
                values = tuple(
                    getattr(event, f"{field}_{suffix}")
                    for suffix in (
                        "ciphertext",
                        "nonce",
                        "key_version",
                        "aad_version",
                    )
                )
                assert all(value is None for value in values) is expired
                assert all(value is not None for value in values) is not expired
            assert (snapshot.content_ciphertext is None) is expired
            assert (approval.payload_ciphertext is None) is expired
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_task27e_ordinary_audits_expire_but_all_started_and_oauth_facts_are_protected(
    database_url: str,
) -> None:
    """损坏/冲突删除 authority 也保留，普通 restore completion 不获得 catalog 豁免。"""
    sessions = build_session_factory(database_url)
    try:
        seed = await seed_lifecycle_action(sessions, retained=True)
        protected = (
            "oauth.refresh_started",
            "oauth.refresh_confirmed",
            "oauth.refresh_recovery_authorization_started",
            "oauth.refresh_recovery_unsatisfied",
            "oauth.refresh_credential_replaced",
            "privacy.deletion_started",
            "privacy.deletion_started",
        )
        async with sessions.begin() as session:
            for event_type in (*protected, "ordinary.old", "database.restore.completed"):
                session.add(
                    AuditEventModel(
                        user_id=seed.user_id,
                        task_id=None,
                        event_type=event_type,
                        actor_type="system",
                        actor_id=None,
                        event_metadata={"malformed": True},
                        created_at=NOW - timedelta(days=366),
                    )
                )
        await RetentionCleanupWorker(sessions).execute(now=NOW, batch_size=2)
        async with sessions() as session:
            events = (
                await session.scalars(
                    select(AuditEventModel.event_type).where(
                        AuditEventModel.user_id == seed.user_id,
                        AuditEventModel.created_at < NOW,
                    )
                )
            ).all()
            assert sorted(events) == sorted(protected)
    finally:
        await sessions.dispose()


async def seed_refresh_group(
    sessions: ManagedAsyncSessionMaker,
    seed: LifecycleSeed,
    *,
    kind: str,
    newer_result: bool = False,
    malformed: bool = False,
) -> tuple[int, ...]:
    """复用严格 parser 的固定合成历史，按真实生成的 audit ID 绑定恢复结果。"""
    from ai_employee.application.calendar_aad_digests import connection_digest_v1

    records = [started()]
    if kind != "confirmed":
        records.append(recovery_started())
    if kind != "unresolved":
        records.append(result_event(kind))
    ids: list[int] = []
    async with sessions.begin() as session:
        for index, record in enumerate(records):
            metadata = dict(record.metadata)
            metadata["connection_digest"] = connection_digest_v1(seed.connection_id)
            metadata["refresh_token_identity_key_version"] = 1
            if "new_refresh_token_identity_key_version" in metadata:
                metadata["new_refresh_token_identity_key_version"] = 1
            if "recovery_authorization_started_event_id" in metadata:
                metadata["recovery_authorization_started_event_id"] = str(ids[1])
            is_result = index == len(records) - 1 and kind != "unresolved"
            if malformed and is_result:
                metadata["unexpected"] = True
            row = AuditEventModel(
                user_id=seed.user_id,
                task_id=None,
                event_type=record.event_type,
                actor_type="system",
                actor_id=None,
                event_metadata=metadata,
                created_at=(
                    NOW - timedelta(days=364)
                    if newer_result and is_result
                    else NOW - timedelta(days=366) + timedelta(seconds=index)
                ),
            )
            session.add(row)
            await session.flush()
            ids.append(row.id)
    return tuple(ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["confirmed", "unsatisfied", "replacement"])
@pytest.mark.parametrize("case", ["closed", "newer_result", "malformed"])
async def test_task27e_oauth_closed_groups_use_strict_history_and_each_member_cutoff(
    database_url: str,
    kind: str,
    case: str,
) -> None:
    """只有完整旧组被删除；闭合不读取当前凭据/代际/能力，未决原 fence 保留。"""
    sessions = build_session_factory(database_url)
    try:
        seed = await seed_lifecycle_action(sessions, retained=True)
        ids = await seed_refresh_group(
            sessions,
            seed,
            kind=kind,
            newer_result=case == "newer_result",
            malformed=case == "malformed",
        )
        queries: list[str] = []

        def record_sql(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            """仅观察 SQL 列名，不保存参数、token、ciphertext 或 lineage 值。"""
            del connection, cursor, parameters, context, executemany
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)

        event.listen(sessions.engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            await RetentionCleanupWorker(sessions).execute(now=NOW, batch_size=1)
        finally:
            event.remove(sessions.engine.sync_engine, "before_cursor_execute", record_sql)
        async with sessions() as session:
            remaining = tuple(
                (
                    await session.scalars(
                        select(AuditEventModel.id)
                        .where(
                            AuditEventModel.user_id == seed.user_id,
                            AuditEventModel.id.in_(ids),
                        )
                        .order_by(AuditEventModel.id)
                    )
                ).all()
            )
            expected = ids if case != "closed" else ((ids[0],) if kind == "unsatisfied" else ())
            assert remaining == expected
        forbidden = (
            "encrypted_credentials.ciphertext",
            "encrypted_credentials.nonce",
            "encrypted_credentials.key_version",
            "encrypted_credentials.token_expires_at",
            "oauth_connections.authorization_generation",
            "oauth_connections.scopes",
        )
        assert not any(column in query for query in queries for column in forbidden)
    finally:
        await sessions.dispose()
