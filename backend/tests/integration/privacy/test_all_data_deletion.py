"""验证全数据删除在撤销失败时仍完成本地不可逆清理。"""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from ai_employee.application.use_cases.task_execution import LeasedTask, TaskLeaseMode
from ai_employee.domain.settings import WeeklyWorkingHours
from ai_employee.infrastructure.db.models.briefs import (
    ConversationModel,
    DailyBriefModel,
    MessageModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel
from ai_employee.infrastructure.db.models.sources import (
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.privacy_checkpoints import (
    PostgresPrivacyCheckpointCleaner,
)
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.workers.privacy import AllDataDeletionCompleted, PrivacyDeletionWorker


class FailingRevoker:
    """记录合成 token 后模拟供应商网络不可用，绝不建立真实连接。"""

    def __init__(self) -> None:
        """初始化撤销调用记录。"""
        self.tokens: list[str] = []

    async def revoke(self, token: str) -> None:
        """记录 token 并抛出可预期传输错误。"""
        self.tokens.append(token)
        raise httpx.ConnectError("synthetic provider outage")


class CrashAfterCredentialCleanupWorker(PrivacyDeletionWorker):
    """仅供测试在已提交的首个删除阶段后模拟进程崩溃。"""

    async def _after_deletion_phase(self, *, phase: str) -> None:
        """在首个连接的凭据本地提交后、任何 revoke 之前模拟崩溃。"""
        if phase == "credentials_deleted":
            raise RuntimeError("synthetic mid-batch crash")


async def _seed_owned_deletion_lease(
    session_factory, *, user_id: UUID, request_id: str
) -> LeasedTask:
    """旧隐私入口回归同样使用真实 QUEUED→RUNNING 认领，不伪造新的删除赢家。"""
    task_id = uuid4()
    async with session_factory.begin() as session:
        session.add(
            TaskRunModel(
                id=task_id,
                user_id=user_id,
                kind="privacy.delete_all_data",
                status="queued",
                idempotency_key=str(task_id),
                input_payload={"deletion_request_id": request_id},
            )
        )
    lease = await SqlAlchemyTaskExecutionStore(session_factory).acquire(
        task_id=task_id,
        lease_owner="synthetic-privacy-owner",
        now=BARRIER_NOW,
        lease_expires_at=BARRIER_NOW + timedelta(minutes=5),
    )
    assert lease is not None
    return lease


@pytest.mark.asyncio
async def test_all_data_deletion_attempts_revocation_once_without_blocking_local_cleanup(
    database_url: str,
) -> None:
    """Google 撤销失败不能阻止凭据删除和一次内容无关完成审计。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"p" * 32)
    revoker = FailingRevoker()
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email="privacy-all-data@example.test",
                display_name="Synthetic owner",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            other = UserModel(email="privacy-all-data-other@example.test", display_name="Other", password_hash=None, timezone="UTC", locale="zh-CN", brief_time=time(8, 0), is_active=True)
            session.add(other)
            await session.flush()
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="google",
                provider_account_id="all-data-cleanup",
                account_email="synthetic@example.test",
                scopes=["gmail.readonly"],
                status="connected",
                last_error_code=None,
            )
            session.add(connection)
            await session.flush()
            encrypted = cipher.encrypt(
                b"synthetic-refresh-token",
                f"{user.id}:{connection.id}:refresh_token".encode("ascii"),
            )
            session.add(
                EncryptedCredentialModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    credential_kind="refresh_token",
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    token_expires_at=datetime.now(UTC),
                )
            )
            thread = EmailThreadModel(user_id=user.id, connection_id=connection.id, provider_thread_id="all-data-email", subject="Synthetic subject", participants=[], latest_message_at=datetime.now(UTC), provider_url="https://example.test/email", provider_updated_at=None)
            session.add(thread)
            await session.flush()
            session.add(EmailMessageModel(user_id=user.id, connection_id=connection.id, thread_id=thread.id, provider_message_id="all-data-message", received_at=datetime.now(UTC), sender={}, recipients=[], subject="Synthetic subject", snippet="Synthetic body", body_ciphertext=None, body_nonce=None, body_key_version=None, labels=[], headers={}, provider_url="https://example.test/email"))
            task = TaskRunModel(user_id=user.id, kind="daily_brief", status="succeeded", idempotency_key="all-data-task", input_payload={})
            conversation = ConversationModel(user_id=user.id, title="Synthetic")
            session.add_all((task, conversation, UserSessionModel(user_id=user.id, token_hash=b"t" * 32, csrf_hash=b"c" * 32, created_at=datetime.now(UTC), expires_at=datetime(2031, 1, 1, tzinfo=UTC), last_seen_at=datetime.now(UTC))))
            await session.flush()
            session.add_all((MessageModel(user_id=user.id, conversation_id=conversation.id, role="user", content_markdown="Synthetic body", task_id=None, created_at=datetime.now(UTC)), DailyBriefModel(user_id=user.id, local_date=date(2030, 1, 1), version=1, task_id=task.id, completeness="complete", source_cutoff=datetime.now(UTC), headline="Synthetic", structured_content={}, markdown="Synthetic", warnings=[], created_at=datetime.now(UTC))))
            other_task = TaskRunModel(user_id=other.id, kind="daily_brief", status="succeeded", idempotency_key="other-task", input_payload={})
            other_conversation = ConversationModel(user_id=other.id, title="Other")
            session.add_all((other_task, other_conversation))
            await session.flush()
            other_id = other.id
            user_id: UUID = user.id

        lease = await _seed_owned_deletion_lease(
            session_factory, user_id=user_id, request_id="opaque-delete-request"
        )
        await PrivacyDeletionWorker(
            session_factory,
            credential_cipher=cipher,
            oauth_adapters={"google": revoker},
            clock=_DeletionClock(),
            checkpoint_cleaner=PostgresPrivacyCheckpointCleaner(database_url),
        ).delete_all_data(
            user_id=user_id,
            request_id="opaque-delete-request",
            batch_size=1,
            task_id=lease.task_id,
            lease_owner=lease.lease_owner,
            lease_mode=lease.lease_mode,
        )

        async with session_factory() as session:
            credentials = await session.scalar(
                select(func.count())
                .select_from(EncryptedCredentialModel)
                .where(EncryptedCredentialModel.user_id == user_id)
            )
            audit_events = (
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.user_id == user_id,
                        AuditEventModel.event_type == "privacy.deletion_completed",
                    )
                )
            ).all()
            owner_sessions = await session.scalar(select(func.count()).select_from(UserSessionModel).where(UserSessionModel.user_id == user_id))
            owner_tasks = await session.scalar(select(func.count()).select_from(TaskRunModel).where(TaskRunModel.user_id == user_id))
            owner_conversations = await session.scalar(select(func.count()).select_from(ConversationModel).where(ConversationModel.user_id == user_id))
            owner_messages = await session.scalar(select(func.count()).select_from(EmailMessageModel).where(EmailMessageModel.user_id == user_id))
            other_tasks = await session.scalar(select(func.count()).select_from(TaskRunModel).where(TaskRunModel.user_id == other_id))
            other_conversations = await session.scalar(select(func.count()).select_from(ConversationModel).where(ConversationModel.user_id == other_id))
            deleted_user = await session.get(UserModel, user_id)

        assert revoker.tokens == ["synthetic-refresh-token"]
        assert credentials == 0
        assert owner_sessions == owner_tasks == owner_conversations == owner_messages == 0
        assert other_tasks == other_conversations == 1
        assert deleted_user is not None and deleted_user.is_active is False
        assert deleted_user.email == f"deleted-{user_id}@invalid.local"
        assert len(audit_events) == 1
        assert set(audit_events[0].event_metadata) == {
            "operation",
            "request_id",
            "completed_at",
            "trace_id",
        }
    finally:
        await session_factory.dispose()


@pytest.mark.parametrize(
    ("case_name", "persisted_key_version", "persisted_nonce"),
    (
        pytest.param("key-version-mismatch", 8, None, id="key-version-mismatch"),
        pytest.param("malformed-nonce", 7, b"short", id="malformed-nonce"),
    ),
)
@pytest.mark.asyncio
async def test_all_data_deletion_ignores_typed_credential_damage_before_local_cleanup(
    database_url: str,
    case_name: str,
    persisted_key_version: int,
    persisted_nonce: bytes | None,
) -> None:
    """明确的本地密文边界失败不能阻断连接删除、匿名化和完成审计。"""
    session_factory = build_session_factory(database_url)
    cipher = AeadCipher(b"d" * 32, key_version=7)
    revoker = FailingRevoker()
    request_id = f"typed-credential-damage-{case_name}"
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email=f"{case_name}@example.test",
                display_name="Synthetic damaged credential owner",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="google",
                provider_account_id=f"damaged-{case_name}",
                account_email=f"damaged-{case_name}@example.test",
                scopes=["gmail.readonly"],
                status="connected",
                last_error_code=None,
            )
            session.add(connection)
            await session.flush()
            encrypted = cipher.encrypt(
                b"synthetic-refresh-token",
                f"{user.id}:{connection.id}:refresh_token".encode("ascii"),
            )
            session.add(
                EncryptedCredentialModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    credential_kind="refresh_token",
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce if persisted_nonce is None else persisted_nonce,
                    key_version=persisted_key_version,
                    token_expires_at=None,
                )
            )
            user_id: UUID = user.id

        lease = await _seed_owned_deletion_lease(
            session_factory, user_id=user_id, request_id=request_id
        )
        await PrivacyDeletionWorker(
            session_factory,
            credential_cipher=cipher,
            oauth_adapters={"google": revoker},
            clock=_DeletionClock(),
            checkpoint_cleaner=PostgresPrivacyCheckpointCleaner(database_url),
        ).delete_all_data(
            user_id=user_id,
            request_id=request_id,
            batch_size=1,
            task_id=lease.task_id,
            lease_owner=lease.lease_owner,
            lease_mode=lease.lease_mode,
        )

        async with session_factory() as session:
            credential_count = await session.scalar(
                select(func.count())
                .select_from(EncryptedCredentialModel)
                .where(EncryptedCredentialModel.user_id == user_id)
            )
            connection_count = await session.scalar(
                select(func.count())
                .select_from(OAuthConnectionModel)
                .where(OAuthConnectionModel.user_id == user_id)
            )
            deleted_user = await session.get(UserModel, user_id)
            completed_events = (
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.user_id == user_id,
                        AuditEventModel.event_type == "privacy.deletion_completed",
                        AuditEventModel.event_metadata["request_id"].astext == request_id,
                    )
                )
            ).all()

        assert revoker.tokens == []
        assert credential_count == connection_count == 0
        assert deleted_user is not None
        assert deleted_user.is_active is False
        assert deleted_user.email == f"deleted-{user_id}@invalid.local"
        assert deleted_user.display_name == "Deleted User"
        assert len(completed_events) == 1
        assert completed_events[0].event_metadata["operation"] == "all_data_deletion"
        assert completed_events[0].event_metadata["request_id"] == request_id
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_all_data_deletion_retries_after_committed_batch_with_one_redacted_audit(
    database_url: str,
) -> None:
    """中途崩溃后重试必须删除构造的本地数据并只保留一条脱敏完成审计。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = UserModel(email="crash-owner@example.test", display_name="Crash", password_hash=None, timezone="UTC", locale="zh-CN", brief_time=time(8, 0), is_active=True)
            session.add(user)
            await session.flush()
            connections = [
                OAuthConnectionModel(user_id=user.id, provider="google", provider_account_id=f"crash-{index}", account_email=f"crash-{index}@example.test", scopes=[], status="connected", last_error_code=None)
                for index in range(3)
            ]
            session.add_all(connections)
            await session.flush()
            session.add_all(
                EncryptedCredentialModel(user_id=user.id, connection_id=connection.id, credential_kind="access_token", ciphertext=b"synthetic-token", nonce=b"123456789012", key_version=1, token_expires_at=None)
                for connection in connections
            )
            session.add(
                UserSessionModel(
                    user_id=user.id,
                    token_hash=b"s" * 32,
                    csrf_hash=b"f" * 32,
                    created_at=datetime.now(UTC),
                    expires_at=datetime(2031, 1, 1, tzinfo=UTC),
                    last_seen_at=datetime.now(UTC),
                )
            )
            user_id = user.id

        lease = await _seed_owned_deletion_lease(
            session_factory, user_id=user_id, request_id="crash-request"
        )
        with pytest.raises(RuntimeError, match="synthetic mid-batch crash"):
            await CrashAfterCredentialCleanupWorker(
                session_factory, clock=_DeletionClock()
            ).delete_all_data(
                user_id=user_id,
                request_id="crash-request",
                batch_size=1,
                task_id=lease.task_id,
                lease_owner=lease.lease_owner,
                lease_mode=lease.lease_mode,
            )
        async with session_factory() as session:
            remaining_after_crash = await session.scalar(
                select(func.count()).select_from(EncryptedCredentialModel).where(
                    EncryptedCredentialModel.user_id == user_id
                )
            )
        assert remaining_after_crash == 2
        later = BARRIER_NOW + timedelta(minutes=10)
        recovery = await SqlAlchemyTaskExecutionStore(session_factory).acquire(
            task_id=lease.task_id,
            lease_owner="synthetic-recovered-owner",
            now=later,
            lease_expires_at=later + timedelta(minutes=5),
        )
        assert (
            recovery is not None and recovery.lease_mode is TaskLeaseMode.INACTIVE_ALL_DATA_RECOVERY
        )
        await PrivacyDeletionWorker(
            session_factory,
            clock=_DeletionClock(later),
            checkpoint_cleaner=PostgresPrivacyCheckpointCleaner(database_url),
        ).delete_all_data(
            user_id=user_id,
            request_id="crash-request",
            batch_size=1,
            task_id=recovery.task_id,
            lease_owner=recovery.lease_owner,
            lease_mode=recovery.lease_mode,
        )

        async with session_factory() as session:
            credential_count = await session.scalar(
                select(func.count())
                .select_from(EncryptedCredentialModel)
                .where(EncryptedCredentialModel.user_id == user_id)
            )
            connection_count = await session.scalar(
                select(func.count())
                .select_from(OAuthConnectionModel)
                .where(OAuthConnectionModel.user_id == user_id)
            )
            session_count = await session.scalar(
                select(func.count())
                .select_from(UserSessionModel)
                .where(UserSessionModel.user_id == user_id)
            )
            deleted_user = await session.get(UserModel, user_id)
            events = (
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.user_id == user_id,
                        AuditEventModel.event_type == "privacy.deletion_completed",
                    )
                )
            ).all()

        assert credential_count == connection_count == session_count == 0
        assert deleted_user is not None
        assert deleted_user.email == f"deleted-{user_id}@invalid.local"
        assert deleted_user.display_name == "Deleted User"
        assert deleted_user.is_active is False
        assert len(events) == 1
        metadata = events[0].event_metadata
        assert set(metadata) == {"operation", "request_id", "completed_at", "trace_id"}
        assert not any(marker in str(metadata) for marker in ("crash@example.test", "Synthetic", "token", "prompt"))
    finally:
        await session_factory.dispose()


# Task27E 使用固定身份与显式时间；与旧撤销/批次回归共享真实 PostgreSQL 隔离边界。
BARRIER_USER_ID = UUID("00000000-0000-0000-0000-000000000101")
BARRIER_TASK_ID = UUID("00000000-0000-0000-0000-000000000401")
BARRIER_OTHER_TASK_ID = UUID("00000000-0000-0000-0000-000000000405")
BARRIER_NOW = datetime(2030, 1, 1, tzinfo=UTC)
BARRIER_REQUEST_ID = "synthetic-request-1"


async def _seed_barrier_task(
    factory, *, change: str = "exact", active: bool = False, live: bool = False
) -> None:
    """构造预屏障 RUNNING 删除任务及完整事实集，变体只修改被测准入条件。"""
    from datetime import timedelta

    from ai_employee.infrastructure.db.models.tasks import TaskStepModel, ToolExecutionModel

    async with factory.begin() as session:
        session.add(
            UserModel(
                id=BARRIER_USER_ID,
                email="barrier@example.test",
                display_name="Synthetic",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=active,
            )
        )
        await session.flush()
        payload = {"deletion_request_id": BARRIER_REQUEST_ID}
        if change == "payload_extra":
            payload["extra"] = "synthetic"
        elif change == "payload_missing":
            payload = {}
        elif change == "payload_empty":
            payload["deletion_request_id"] = ""
        elif change == "payload_number":
            payload["deletion_request_id"] = 1
        status = (
            change
            if change in {"created", "queued", "retry_scheduled", "waiting_approval"}
            else "running"
        )
        task = TaskRunModel(
            id=BARRIER_TASK_ID,
            user_id=BARRIER_USER_ID,
            kind="daily_brief" if change == "ordinary" else "privacy.delete_all_data",
            status=status,
            idempotency_key="synthetic-delete-1",
            input_payload=payload,
            started_at=None if change == "never_started" else BARRIER_NOW - timedelta(days=2),
            attempt_count=0 if change == "zero_attempts" else 1,
            lease_owner="worker-before-barrier",
            lease_expires_at=None
            if change == "no_lease"
            else BARRIER_NOW + timedelta(seconds=30 if live or change == "live_lease" else -1),
            created_at=BARRIER_NOW - timedelta(days=2),
            updated_at=BARRIER_NOW - timedelta(minutes=10),
            approval_checkpoint_recovery_at=BARRIER_NOW if status == "waiting_approval" else None,
        )
        session.add(task)
        session.add(
            TaskRunModel(
                id=BARRIER_OTHER_TASK_ID,
                user_id=BARRIER_USER_ID,
                kind="privacy.delete_all_data",
                status="running",
                idempotency_key="synthetic-delete-2",
                input_payload={"deletion_request_id": "synthetic-request-2"},
                started_at=BARRIER_NOW - timedelta(minutes=2),
                attempt_count=1,
                lease_owner="loser-worker",
                lease_expires_at=BARRIER_NOW - timedelta(seconds=1),
            )
        )
        await session.flush()
        metadata = {
            "schema_version": "privacy_deletion_started.v1",
            "request_id": BARRIER_REQUEST_ID,
        }
        if change == "missing_schema":
            del metadata["schema_version"]
        elif change == "extra_metadata":
            metadata["extra"] = "synthetic"
        elif change == "wrong_schema":
            metadata["schema_version"] = "privacy_deletion_started.v2"
        elif change == "wrong_request":
            metadata["request_id"] = "synthetic-request-2"
        for _ in range(0 if change == "zero_facts" else 2 if change == "many_facts" else 1):
            session.add(
                AuditEventModel(
                    user_id=BARRIER_USER_ID,
                    task_id=None
                    if change == "null_task"
                    else BARRIER_OTHER_TASK_ID
                    if change == "other_task"
                    else BARRIER_TASK_ID,
                    event_type="privacy.deletion_started",
                    actor_type="system",
                    actor_id=None,
                    event_metadata=metadata,
                    created_at=BARRIER_NOW - timedelta(minutes=1),
                )
            )
        if change == "tool_execution":
            step = TaskStepModel(
                task_id=BARRIER_TASK_ID, sequence=1, name="synthetic", kind="synthetic"
            )
            session.add(step)
            await session.flush()
            session.add(
                ToolExecutionModel(
                    task_id=BARRIER_TASK_ID,
                    step_id=step.id,
                    tool_name="fake.write",
                    idempotency_key="synthetic-tool",
                    request_payload_hash="a" * 64,
                    status="claimed",
                )
            )


async def _barrier_task_facts(factory) -> tuple[dict[str, object], int]:
    """读取全部任务列与审计计数，使拒绝路径不能偷偷清空恢复身份或租约历史。"""
    async with factory() as session:
        task = await session.get(TaskRunModel, BARRIER_TASK_ID)
        assert task is not None
        row = {column.name: getattr(task, column.name) for column in TaskRunModel.__table__.columns}
        count = await session.scalar(select(func.count()).select_from(AuditEventModel))
        return row, count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "exact",
        "ordinary",
        "created",
        "queued",
        "retry_scheduled",
        "waiting_approval",
        "live_lease",
        "no_lease",
        "never_started",
        "zero_attempts",
        "payload_extra",
        "payload_missing",
        "payload_empty",
        "payload_number",
        "tool_execution",
        "zero_facts",
        "many_facts",
        "null_task",
        "other_task",
        "missing_schema",
        "extra_metadata",
        "wrong_schema",
        "wrong_request",
    ],
)
async def test_task27e_inactive_acquisition_admits_only_exact_expired_winner(
    database_url: str, change: str
) -> None:
    """inactive 普通任务与所有歧义删除任务零认领，唯一精确赢家保留原历史恢复。"""
    from datetime import timedelta

    from ai_employee.application.use_cases import task_execution as execution
    from ai_employee.infrastructure.db.repositories.task_execution import (
        SqlAlchemyTaskExecutionStore,
    )

    factory = build_session_factory(database_url)
    try:
        await _seed_barrier_task(factory, change=change)
        before = await _barrier_task_facts(factory)
        result = await SqlAlchemyTaskExecutionStore(factory).acquire(
            task_id=BARRIER_TASK_ID,
            lease_owner="recovery-worker",
            now=BARRIER_NOW,
            lease_expires_at=BARRIER_NOW + timedelta(seconds=60),
        )
        if change == "exact":
            assert result is not None
            assert result.lease_mode is execution.TaskLeaseMode.INACTIVE_ALL_DATA_RECOVERY
            assert result.started_at == before[0]["started_at"]
            assert result.input_payload == {"deletion_request_id": BARRIER_REQUEST_ID}
            assert result.attempt_count == 2
        else:
            assert result is None
            assert await _barrier_task_facts(factory) == before
    finally:
        await factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "exact",
        "ordinary",
        "many_facts",
        "zero_facts",
        "other_task",
        "wrong_request",
        "extra_metadata",
        "wrong_owner",
        "expired",
    ],
)
async def test_task27e_inactive_renewal_requires_live_exact_winner(
    database_url: str, change: str
) -> None:
    """屏障之后仅唯一赢家持久 owner/旧有效租约可续租，拒绝路径保持截止时间不变。"""
    from datetime import timedelta

    from ai_employee.infrastructure.db.repositories.task_execution import (
        SqlAlchemyTaskExecutionStore,
    )

    factory = build_session_factory(database_url)
    try:
        await _seed_barrier_task(factory, change=change, live=change != "expired")
        before = await _barrier_task_facts(factory)
        result = await SqlAlchemyTaskExecutionStore(factory).renew(
            task_id=BARRIER_TASK_ID,
            lease_owner="wrong" if change == "wrong_owner" else "worker-before-barrier",
            renewed_at=BARRIER_NOW,
            lease_expires_at=BARRIER_NOW + timedelta(seconds=60),
        )
        assert result is (change == "exact")
        if not result:
            assert await _barrier_task_facts(factory) == before
    finally:
        await factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "succeeded",
        "failed",
        "cancelled",
        "retry_scheduled",
        "schedule_retry",
        "fail_internal",
        "prepare_retry",
    ],
)
async def test_task27e_generic_writes_preserve_inactive_winner_running(
    database_url: str, operation: str
) -> None:
    """Runner 通用错误/成功/重试不得终结已赢得屏障的删除任务，只有最终删除事务可移除。"""
    from datetime import timedelta

    from ai_employee.domain.tasks import TaskStatus
    from ai_employee.infrastructure.db.repositories.task_execution import (
        SqlAlchemyTaskExecutionStore,
    )

    factory = build_session_factory(database_url)
    try:
        await _seed_barrier_task(factory, live=True)
        before = await _barrier_task_facts(factory)
        store = SqlAlchemyTaskExecutionStore(factory)
        if operation == "prepare_retry":
            await store.prepare_retry(task_id=BARRIER_TASK_ID, now=BARRIER_NOW)
        elif operation == "schedule_retry":
            assert not await store.schedule_retry(
                task_id=BARRIER_TASK_ID,
                lease_owner="worker-before-barrier",
                scheduled_at=BARRIER_NOW,
                retry_available_at=BARRIER_NOW + timedelta(seconds=5),
                error_code="synthetic",
                attempt_count=1,
            )
        elif operation == "fail_internal":
            assert not await store.fail_internal(
                task_id=BARRIER_TASK_ID,
                lease_owner="worker-before-barrier",
                failed_at=BARRIER_NOW,
                error_code="internal_error",
            )
        else:
            assert not await store.finish(
                task_id=BARRIER_TASK_ID,
                lease_owner="worker-before-barrier",
                status=TaskStatus(operation),
                finished_at=BARRIER_NOW,
                error_code=None,
            )
        assert await _barrier_task_facts(factory) == before
    finally:
        await factory.dispose()


class _DeletionClock:
    """删除步骤只使用显式 UTC 合成时刻。"""

    def __init__(self, now: datetime = BARRIER_NOW) -> None:
        """默认冻结初始尝试，故障恢复测试可以显式推进时间。"""
        self._now = now

    def now(self) -> datetime:
        """返回冻结时刻，避免租约测试依赖运行机器时间。"""
        return self._now


class _PhaseCrashWorker(PrivacyDeletionWorker):
    """仅在已提交阶段或最终事务内的明确观察点注入进程故障。"""

    def __init__(self, factory, phase: str) -> None:
        """使用旧公开构造边界，RED 必须因缺少行为而非新参数 TypeError 失败。"""
        super().__init__(
            factory,
            checkpoint_cleaner=PostgresPrivacyCheckpointCleaner(
                factory.engine.url.render_as_string(hide_password=False),
            ),
        )
        self._clock = _DeletionClock()
        self.phase = phase
        self.visited: list[str] = []

    async def _after_deletion_phase(self, *, phase: str) -> None:
        """阶段命中后只抛合成异常，不修改恢复 authority。"""
        self.visited.append(phase)
        if phase == self.phase:
            raise RuntimeError("synthetic deletion phase crash")


def _barrier_lease(*, recovery: bool = False, other: bool = False) -> LeasedTask:
    """重用真实 TaskRun 的精确绑定；测试不能以新 request 冒充旧赢家。"""
    return LeasedTask(
        task_id=BARRIER_OTHER_TASK_ID if other else BARRIER_TASK_ID,
        kind="privacy.delete_all_data",
        input_payload={
            "deletion_request_id": "synthetic-request-2" if other else BARRIER_REQUEST_ID
        },
        started_at=BARRIER_NOW - timedelta(days=2),
        user_id=BARRIER_USER_ID,
        lease_owner="loser-worker" if other else "worker-before-barrier",
        lease_mode=TaskLeaseMode.INACTIVE_ALL_DATA_RECOVERY if recovery else TaskLeaseMode.NORMAL,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    [
        "barrier",
        "unclaimed_actions",
        "reconciliation",
        "credentials_deleted",
        "revoke_attempted",
        "local_rows_deleted",
        "before_final_commit",
    ],
)
async def test_task27e_each_deletion_phase_crash_keeps_same_winner_recoverable(
    database_url: str,
    phase: str,
) -> None:
    """每个阶段崩溃后原 RUNNING/authority 可重获租约；最终事务中途失败整体回滚。"""
    factory = build_session_factory(database_url)
    try:
        await _seed_barrier_task(factory, change="zero_facts", active=True, live=True)
        async with factory.begin() as session:
            connection = OAuthConnectionModel(
                user_id=BARRIER_USER_ID,
                provider="google",
                provider_account_id="phase-fixture",
                account_email="phase@example.test",
                scopes=[],
                status="connected",
            )
            session.add(connection)
            await session.flush()
            session.add(
                EncryptedCredentialModel(
                    user_id=BARRIER_USER_ID,
                    connection_id=connection.id,
                    credential_kind="refresh_token",
                    ciphertext=b"synthetic",
                    nonce=b"123456789012",
                    key_version=1,
                )
            )
        worker = _PhaseCrashWorker(factory, phase)
        try:
            await worker.execute(_barrier_lease())
        except AllDataDeletionCompleted:
            pass
        except RuntimeError as error:
            assert str(error) == "synthetic deletion phase crash"
        assert phase in worker.visited
        async with factory.begin() as session:
            user = await session.get(UserModel, BARRIER_USER_ID)
            task = await session.get(TaskRunModel, BARRIER_TASK_ID)
            assert user is not None and user.is_active is False
            assert user.email == "barrier@example.test"
            assert task is not None and task.status == "running"
            assert task.input_payload == {"deletion_request_id": BARRIER_REQUEST_ID}
            started_rows = (
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.user_id == BARRIER_USER_ID,
                        AuditEventModel.event_type == "privacy.deletion_started",
                    )
                )
            ).all()
            assert len(started_rows) == 1
            authority = started_rows[0]
            assert (
                authority.task_id,
                authority.user_id,
                authority.actor_type,
                authority.actor_id,
            ) == (
                BARRIER_TASK_ID,
                BARRIER_USER_ID,
                "system",
                None,
            )
            assert authority.event_metadata == {
                "schema_version": "privacy_deletion_started.v1",
                "request_id": BARRIER_REQUEST_ID,
            }
            task.lease_expires_at = BARRIER_NOW - timedelta(seconds=1)
        recovered = await SqlAlchemyTaskExecutionStore(factory).acquire(
            task_id=BARRIER_TASK_ID,
            lease_owner="recovery-worker",
            now=BARRIER_NOW,
            lease_expires_at=BARRIER_NOW + timedelta(minutes=5),
        )
        assert recovered is not None
        assert recovered.lease_mode is TaskLeaseMode.INACTIVE_ALL_DATA_RECOVERY
        completing = PrivacyDeletionWorker(
            factory, checkpoint_cleaner=PostgresPrivacyCheckpointCleaner(database_url)
        )
        completing._clock = _DeletionClock()
        with pytest.raises(AllDataDeletionCompleted):
            await completing.execute(recovered)
        async with factory() as session:
            assert await session.get(TaskRunModel, BARRIER_TASK_ID) is None
            assert await session.get(TaskRunModel, BARRIER_OTHER_TASK_ID) is None
            rows = (
                await session.scalars(
                    select(AuditEventModel).where(AuditEventModel.user_id == BARRIER_USER_ID)
                )
            ).all()
            assert len(rows) == 1 and rows[0].event_type == "privacy.deletion_completed"
            assert rows[0].task_id is None
            assert rows[0].event_metadata == {
                "operation": "all_data_deletion",
                "request_id": BARRIER_REQUEST_ID,
                "completed_at": BARRIER_NOW.isoformat(),
                "trace_id": None,
            }
        assert (
            await SqlAlchemyTaskExecutionStore(factory).acquire(
                task_id=BARRIER_TASK_ID,
                lease_owner="ack-replay",
                now=BARRIER_NOW,
                lease_expires_at=BARRIER_NOW + timedelta(minutes=5),
            )
            is None
        )
    finally:
        await factory.dispose()


@pytest.mark.asyncio
async def test_task27e_barrier_loser_performs_no_cleanup_and_cannot_create_second_authority(
    database_url: str,
) -> None:
    """两个预先 RUNNING 删除任务只允许一个 true→false CAS 赢家，失败者零副作用。"""
    factory = build_session_factory(database_url)
    try:
        await _seed_barrier_task(factory, change="zero_facts", active=True, live=True)
        async with factory.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == BARRIER_OTHER_TASK_ID)
                .values(
                    lease_expires_at=BARRIER_NOW + timedelta(minutes=5),
                )
            )
        winner = _PhaseCrashWorker(factory, "barrier")
        try:
            await winner.execute(_barrier_lease())
        except (RuntimeError, AllDataDeletionCompleted):
            pass
        assert winner.visited == ["barrier"]
        before = await _barrier_task_facts(factory)
        loser = _PhaseCrashWorker(factory, "barrier")
        # 失败者的显式拒绝不能误入成功哨兵；它的普通 Runner 后续可由赢家统一移除。
        from ai_employee.domain.errors import StateConflictError

        with pytest.raises(StateConflictError):
            await loser.execute(_barrier_lease(other=True))
        assert loser.visited == []
        assert await _barrier_task_facts(factory) == before
    finally:
        await factory.dispose()


@pytest.mark.asyncio
async def test_task27e_all_data_deletes_m2_and_resets_every_default_with_bound_accounts(
    database_url: str,
) -> None:
    """默认连接外键、全部非默认设置及M2子图都必须由显式删除和同一最终匿名化覆盖。"""
    from sqlalchemy import text

    from ai_employee.infrastructure.db.base import Base
    from tests.integration.retention.test_m2_action_retention import seed_lifecycle_action

    factory = build_session_factory(database_url)
    try:
        await _seed_barrier_task(factory, change="zero_facts", active=True, live=True)
        mail = await seed_lifecycle_action(factory, user_id=BARRIER_USER_ID, binding="thread")
        calendar = await seed_lifecycle_action(
            factory,
            user_id=BARRIER_USER_ID,
            kind="calendar",
            binding="event",
            execution_status="reconciling",
            event_ends_at=BARRIER_NOW,
        )
        async with factory.begin() as session:
            user = await session.get(UserModel, BARRIER_USER_ID)
            assert user is not None
            user.default_mail_connection_id = mail.connection_id
            user.default_calendar_connection_id = calendar.connection_id
            user.default_calendar_id = "primary"
            user.password_hash = "synthetic-password-hash"
            user.timezone, user.locale, user.brief_time = "Asia/Shanghai", "en-US", time(17, 30)
            (
                user.email_body_retention_days,
                user.source_metadata_retention_days,
                user.workspace_history_retention_days,
            ) = 7, 90, 730
            user.working_hours = {
                **WeeklyWorkingHours.default().to_mapping(),
                "monday": [["10:00", "15:00"]],
            }
            user.meeting_buffer_minutes = 45
        with pytest.raises(AllDataDeletionCompleted):
            await _PhaseCrashWorker(factory, "never").execute(_barrier_lease())
        async with factory() as session:
            user = await session.get(UserModel, BARRIER_USER_ID)
            assert user is not None and not user.is_active
            assert (
                user.password_hash,
                user.default_mail_connection_id,
                user.default_calendar_connection_id,
                user.default_calendar_id,
            ) == (None,) * 4
            assert (user.timezone, user.locale, user.brief_time) == ("UTC", "zh-CN", time(8))
            assert (
                user.email_body_retention_days,
                user.source_metadata_retention_days,
                user.workspace_history_retention_days,
            ) == (30, 180, 365)
            assert user.working_hours == WeeklyWorkingHours.default().to_mapping()
            assert user.meeting_buffer_minutes == 10 and user.updated_at == BARRIER_NOW
            for table in Base.metadata.sorted_tables:
                if "user_id" in table.c and table.name != "audit_events":
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(table)
                            .where(table.c.user_id == BARRIER_USER_ID)
                        )
                        == 0
                    )
            assert await session.scalar(text("SELECT count(*) FROM users")) == 1
            assert (await session.scalars(select(AuditEventModel.event_type))).all() == [
                "privacy.deletion_completed"
            ]
    finally:
        await factory.dispose()
