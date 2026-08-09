"""验证 Task 14 用例与现有不可变草稿 Repository 的组合行为。"""

import asyncio
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from ai_employee.application.use_cases.mail_drafts import (
    CreateMailDraftInput,
    MailDraftUseCase,
    UpdateMailDraftInput,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.actions import MailDraftStatus
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.actions import MailDraftModel, MailDraftVersionModel
from ai_employee.infrastructure.db.models.briefs import LLMInvocationModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EmailMessageModel,
    EmailThreadModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyMailSyncRepository
from ai_employee.infrastructure.db.repositories.mail_drafts import SqlAlchemyMailDraftRepository
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.llm.fake import FakeModelGateway
from ai_employee.workers.generate_mail_draft import GenerateMailDraftTaskStep


class _TimeoutModelGateway(FakeModelGateway):
    """仅用于证明 provider-neutral 原生超时会进入稳定失败回退。"""

    async def complete(self, **_kwargs: object) -> object:
        """模拟适配器在返回结构化响应前抛出原生超时。"""
        raise TimeoutError("synthetic model timeout")


class _BlockingMailDraftGateway(FakeModelGateway):
    """在模型边界阻塞，允许测试并发修改持久租约后再释放旧 Worker。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, **kwargs: object) -> object:
        """通知测试模型调用已开始，并在显式释放后返回严格合成正文。"""
        self.started.set()
        await self.release.wait()
        return await super().complete(**kwargs)


@pytest.mark.asyncio
async def test_generation_rejects_missing_real_lease_owner(database_url: str) -> None:
    """真实生成步骤缺少租约 owner 时必须在模型或数据库业务读取前 fail closed。"""
    session_factory = build_session_factory(database_url)
    gateway = FakeModelGateway()
    try:
        step = GenerateMailDraftTaskStep(
            session_factory=session_factory,
            action_cipher=ActionPayloadCipher.from_key(b"o" * 32),
            source_cipher=AeadCipher(b"s" * 32),
            model_gateway=gateway,
            model_name="fake-mail-model",
        )

        with pytest.raises(ValueError, match="lease_owner"):
            await step.execute(
                LeasedTask(
                    task_id=uuid4(),
                    user_id=uuid4(),
                    kind="mail_draft.generate",
                    input_payload={
                        "draft_id": str(uuid4()),
                        "instruction": "Synthetic instruction",
                    },
                    started_at=datetime(2026, 8, 9, tzinfo=UTC),
                )
            )

        assert gateway.calls == []
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutated_status", "mutated_owner"),
    (
        (TaskStatus.CANCELLED, None),
        (TaskStatus.RUNNING, "worker-replacement"),
    ),
    ids=("cancelled", "owner-replaced"),
)
async def test_generation_drops_result_when_lease_changes_during_model_call(
    database_url: str,
    mutated_status: TaskStatus,
    mutated_owner: str | None,
) -> None:
    """模型返回前任务被取消或换 owner 时，旧 Worker 不得提交任何生成副作用。"""
    session_factory = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    lease_owner = "worker-original"
    action_cipher = ActionPayloadCipher.from_key(b"l" * 32)
    gateway = _BlockingMailDraftGateway()
    try:
        async with session_factory.begin() as session:
            session.add_all(
                (
                    UserModel(
                        id=user_id,
                        email=f"lease-{user_id}@example.test",
                        display_name="Synthetic Lease User",
                        password_hash=None,
                        timezone="UTC",
                        locale="en-US",
                        brief_time=time(8, 0),
                    ),
                    OAuthConnectionModel(
                        id=connection_id,
                        user_id=user_id,
                        provider="google",
                        provider_account_id=f"lease-{connection_id}",
                        account_email="owner@example.test",
                        scopes=[],
                        status="connected",
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability=ConnectionCapability.MAIL_READ.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=[],
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability=ConnectionCapability.MAIL_SEND.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=[],
                    ),
                )
            )
        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, action_cipher)
            draft = await MailDraftUseCase(
                drafts=repository,
                connections=repository,
                clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
            ).create_new(
                user_id=user_id,
                connection_id=connection_id,
                idempotency_key=f"lease-draft-{mutated_status.value}-{mutated_owner}",
            )
            task_row = TaskRunModel(
                user_id=user_id,
                kind="mail_draft.generate",
                status=TaskStatus.RUNNING.value,
                lease_owner=lease_owner,
                idempotency_key=f"lease-task-{mutated_status.value}-{mutated_owner}",
                input_payload={
                    "draft_id": str(draft.draft_id),
                    "expected_version": 1,
                    "instruction": "Write a synthetic body",
                },
            )
            session.add(task_row)
            await session.flush()
            task_id = task_row.id

        step = GenerateMailDraftTaskStep(
            session_factory=session_factory,
            action_cipher=action_cipher,
            source_cipher=AeadCipher(b"s" * 32),
            model_gateway=gateway,
            model_name="fake-mail-model",
            clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
        )
        execution = asyncio.create_task(
            step.execute(
                LeasedTask(
                    task_id=task_id,
                    user_id=user_id,
                    kind="mail_draft.generate",
                    input_payload={
                        "draft_id": str(draft.draft_id),
                        "expected_version": 1,
                        "instruction": "Write a synthetic body",
                    },
                    started_at=datetime(2026, 8, 9, tzinfo=UTC),
                    lease_owner=lease_owner,
                )
            )
        )
        await asyncio.wait_for(gateway.started.wait(), timeout=2)
        async with session_factory.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
                .values(
                    status=mutated_status.value,
                    lease_owner=mutated_owner,
                )
            )
        gateway.release.set()
        await execution

        async with session_factory() as session:
            persisted_task = await session.get(TaskRunModel, task_id)
            version_count = await session.scalar(
                select(func.count()).select_from(MailDraftVersionModel).where(
                    MailDraftVersionModel.draft_id == draft.draft_id
                )
            )
            invocation_count = await session.scalar(
                select(func.count()).select_from(LLMInvocationModel).where(
                    LLMInvocationModel.task_id == task_id
                )
            )
            audit_count = await session.scalar(
                select(func.count()).select_from(AuditEventModel).where(
                    AuditEventModel.task_id == task_id
                )
            )
        assert persisted_task is not None
        assert persisted_task.status == mutated_status.value
        assert persisted_task.lease_owner == mutated_owner
        assert persisted_task.result_payload is None
        assert version_count == 1
        assert invocation_count == 0
        assert audit_count == 0
    finally:
        gateway.release.set()
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_mail_draft_patch_creates_one_monotonic_immutable_version(database_url: str) -> None:
    """PATCH 通过 CAS 写入版本 2，旧版本仍保留且正文密文不泄漏。"""
    session_factory = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    cipher = ActionPayloadCipher.from_key(b"m" * 32)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                id=user_id,
                email=f"draft-{user_id}@example.test",
                display_name="Synthetic Draft User",
                password_hash=None,
                timezone="UTC",
                locale="en-US",
                brief_time=time(8, 0),
            )
            connection = OAuthConnectionModel(
                id=connection_id,
                user_id=user_id,
                provider="google",
                provider_account_id=f"draft-{connection_id}",
                account_email="owner@example.test",
                scopes=[],
                status="connected",
            )
            capabilities = tuple(
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability=capability.value,
                    status="enabled",
                    actual_scopes=[],
                )
                for capability in (
                    ConnectionCapability.MAIL_READ,
                    ConnectionCapability.MAIL_SEND,
                )
            )
            session.add_all((user, connection, *capabilities))
        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, cipher)
            use_case = MailDraftUseCase(
                drafts=repository,
                connections=repository,
                clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
            )
            created = await use_case.create(
                CreateMailDraftInput(
                    user_id=user_id,
                    mode=MailMode.NEW,
                    idempotency_key="integration-draft-1",
                    connection_id=connection_id,
                    to_recipients=("peer@example.test",),
                    body_text="Synthetic body one",
                )
            )
            updated = await use_case.update(
                UpdateMailDraftInput(
                    user_id=user_id,
                    draft_id=created.draft_id,
                    expected_version=1,
                    body_text="Synthetic body two",
                )
            )
            assert updated.current_version == 2
            assert updated.status is MailDraftStatus.EDITING
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(MailDraftVersionModel).where(
                    MailDraftVersionModel.draft_id == created.draft_id
                )
            )
            ciphertexts = tuple(
                (
                    await session.scalars(
                        select(MailDraftVersionModel.body_ciphertext).where(
                            MailDraftVersionModel.draft_id == created.draft_id
                        )
                    )
                ).all()
            )
        assert count == 2
        assert all(ciphertext is not None for ciphertext in ciphertexts)
        assert all(
            b"Synthetic body one" not in ciphertext
            and b"Synthetic body two" not in ciphertext
            for ciphertext in ciphertexts
            if ciphertext is not None
        )
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_default_mail_connection_does_not_fallback_and_distinguishes_scope_state(
    database_url: str,
) -> None:
    """默认账户不可用时不改选其他账户，并区分本地关闭与需重新授权。"""
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    default_connection_id, fallback_connection_id = uuid4(), uuid4()
    cipher = ActionPayloadCipher.from_key(b"p" * 32)
    now = datetime(2026, 8, 9, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email=f"capability-{user_id}@example.test",
                    display_name="Synthetic Capability User",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8, 0),
                    default_mail_connection_id=default_connection_id,
                )
            )
            session.add_all(
                (
                    OAuthConnectionModel(
                        id=default_connection_id,
                        user_id=user_id,
                        provider="google",
                        provider_account_id=f"default-{default_connection_id}",
                        account_email="default-owner@example.test",
                        scopes=[],
                        status="connected",
                    ),
                    OAuthConnectionModel(
                        id=fallback_connection_id,
                        user_id=user_id,
                        provider="google",
                        provider_account_id=f"fallback-{fallback_connection_id}",
                        account_email="fallback-owner@example.test",
                        scopes=[],
                        status="connected",
                    ),
                )
            )
            session.add_all(
                (
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=default_connection_id,
                        capability=ConnectionCapability.MAIL_READ.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=[],
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=default_connection_id,
                        capability=ConnectionCapability.MAIL_SEND.value,
                        status=CapabilityStatus.DISABLED.value,
                        actual_scopes=[],
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=fallback_connection_id,
                        capability=ConnectionCapability.MAIL_READ.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=[],
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=fallback_connection_id,
                        capability=ConnectionCapability.MAIL_SEND.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=[],
                    ),
                )
            )

        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, cipher)
            states = await repository.get_capability_states(
                user_id=user_id,
                connection_id=default_connection_id,
            )
            assert states is not None
            assert {
                (state.capability, state.status, state.last_error_code)
                for state in states
            } == {
                (ConnectionCapability.MAIL_READ, CapabilityStatus.ENABLED, None),
                (ConnectionCapability.MAIL_SEND, CapabilityStatus.DISABLED, None),
            }
            use_case = MailDraftUseCase(
                drafts=repository,
                connections=repository,
                clock=lambda: now,
            )
            with pytest.raises(StateConflictError) as disabled:
                await use_case.create_new(
                    user_id=user_id,
                    idempotency_key="default-disabled",
                )
            assert disabled.value.error_code == "connection_capability_disabled"

        async with session_factory.begin() as session:
            await session.execute(
                update(ConnectionCapabilityModel)
                .where(
                    ConnectionCapabilityModel.user_id == user_id,
                    ConnectionCapabilityModel.connection_id == default_connection_id,
                    ConnectionCapabilityModel.capability
                    == ConnectionCapability.MAIL_SEND.value,
                )
                .values(
                    status=CapabilityStatus.ACTION_REQUIRED.value,
                    last_error_code="connection_scope_missing",
                )
            )
            repository = SqlAlchemyMailDraftRepository(session, cipher)
            use_case = MailDraftUseCase(
                drafts=repository,
                connections=repository,
                clock=lambda: now,
            )
            with pytest.raises(StateConflictError) as scope_missing:
                await use_case.create_new(
                    user_id=user_id,
                    idempotency_key="default-scope-missing",
                )
            assert scope_missing.value.error_code == "connection_scope_missing"

        async with session_factory() as session:
            draft_count = await session.scalar(
                select(func.count()).select_from(MailDraftModel).where(
                    MailDraftModel.user_id == user_id
                )
            )
        assert draft_count == 0
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_reply_source_provider_ids_without_connection_fail_closed_when_ambiguous(
    database_url: str,
) -> None:
    """同一用户两连接共享 opaque provider ID 时不得按时间静默选择账户。"""
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    connection_ids = (uuid4(), uuid4())
    thread_ids = (uuid4(), uuid4())
    message_ids = (uuid4(), uuid4())
    now = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
    cipher = ActionPayloadCipher.from_key(b"a" * 32)
    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email=f"ambiguous-{user_id}@example.test",
                    display_name="Synthetic Ambiguous Source User",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8, 0),
                )
            )
            await session.flush()
            for index, connection_id in enumerate(connection_ids):
                session.add(
                    OAuthConnectionModel(
                        id=connection_id,
                        user_id=user_id,
                        provider="google" if index == 0 else "microsoft",
                        provider_account_id=f"ambiguous-{connection_id}",
                        provider_tenant_id="" if index == 0 else f"tenant-{connection_id}",
                        account_type="google" if index == 0 else "work_school",
                        account_email=f"owner-{index}@example.test",
                        scopes=[],
                        status="connected",
                    )
                )
                session.add_all(
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability=capability.value,
                        status="enabled",
                        actual_scopes=[],
                    )
                    for capability in (
                        ConnectionCapability.MAIL_READ,
                        ConnectionCapability.MAIL_SEND,
                    )
                )
                await session.flush()
                session.add(
                    EmailThreadModel(
                        id=thread_ids[index],
                        user_id=user_id,
                        connection_id=connection_id,
                        provider_thread_id="shared-provider-thread",
                        subject="Synthetic shared subject",
                        participants=[],
                        latest_message_at=now,
                        provider_url=f"https://provider.example.test/thread/{index}",
                    )
                )
                # ORM 模型没有 relationship；先落线程 FK 目标，避免 fixture 的批量插入顺序
                # 掩盖本测试真正要验证的 provider ID 歧义。
                await session.flush()
                session.add(
                    EmailMessageModel(
                        id=message_ids[index],
                        user_id=user_id,
                        connection_id=connection_id,
                        thread_id=thread_ids[index],
                        provider_message_id="shared-provider-message",
                        received_at=now,
                        mailbox_scope_key="mailbox",
                        sender={"email": f"peer-{index}@example.test"},
                        recipients=[{"email": f"owner-{index}@example.test"}],
                        subject="Synthetic shared subject",
                        snippet="Synthetic snippet",
                        labels=["INBOX"],
                        headers={},
                        provider_url=f"https://provider.example.test/message/{index}",
                    )
                )

        async with session_factory.begin() as session:
            drafts = SqlAlchemyMailDraftRepository(session, cipher)
            use_case = MailDraftUseCase(
                drafts=drafts,
                connections=drafts,
                sources=SqlAlchemyMailSyncRepository(session),
                clock=lambda: now,
            )
            with pytest.raises(StateConflictError) as captured:
                await use_case.create(
                    CreateMailDraftInput(
                        user_id=user_id,
                        mode=MailMode.REPLY,
                        idempotency_key="ambiguous-provider-source",
                        source_thread_id="shared-provider-thread",
                        source_message_id="shared-provider-message",
                    )
                )

        assert captured.value.error_code == "mail_thread_binding_conflict"
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_local_uuid_source_reference_never_matches_another_connections_provider_id(
    database_url: str,
) -> None:
    """本地主键字符串必须只走 UUID 精确查询，不得同时匹配另一连接的 provider ID。"""
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    local_connection_id, other_connection_id = uuid4(), uuid4()
    local_thread_id, other_thread_id = uuid4(), uuid4()
    local_message_id, other_message_id = uuid4(), uuid4()
    now = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email=f"local-source-{user_id}@example.test",
                    display_name="Synthetic Local Source User",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8, 0),
                )
            )
            await session.flush()
            for index, connection_id in enumerate((local_connection_id, other_connection_id)):
                session.add(
                    OAuthConnectionModel(
                        id=connection_id,
                        user_id=user_id,
                        provider="google",
                        provider_account_id=f"local-source-{connection_id}",
                        account_email=f"owner-{index}@example.test",
                        scopes=[],
                        status="connected",
                    )
                )
                session.add(
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability=ConnectionCapability.MAIL_READ.value,
                        status="enabled",
                        actual_scopes=[],
                    )
                )
            await session.flush()
            session.add_all(
                (
                    EmailThreadModel(
                        id=local_thread_id,
                        user_id=user_id,
                        connection_id=local_connection_id,
                        provider_thread_id="local-provider-thread",
                        subject="Local source subject",
                        participants=[],
                        latest_message_at=now,
                        provider_url="https://provider.example.test/thread/local",
                    ),
                    EmailThreadModel(
                        id=other_thread_id,
                        user_id=user_id,
                        connection_id=other_connection_id,
                        provider_thread_id=str(local_thread_id),
                        subject="Other source subject",
                        participants=[],
                        latest_message_at=now,
                        provider_url="https://provider.example.test/thread/other",
                    ),
                )
            )
            # 先建立两个线程，再插入消息，保证失败只可能来自来源解析行为。
            await session.flush()
            session.add_all(
                (
                    EmailMessageModel(
                        id=local_message_id,
                        user_id=user_id,
                        connection_id=local_connection_id,
                        thread_id=local_thread_id,
                        provider_message_id="local-provider-message",
                        received_at=now,
                        mailbox_scope_key="mailbox",
                        sender={"email": "local-peer@example.test"},
                        recipients=[{"email": "owner-0@example.test"}],
                        subject="Local source subject",
                        snippet="Local snippet",
                        labels=["INBOX"],
                        headers={},
                        provider_url="https://provider.example.test/message/local",
                    ),
                    EmailMessageModel(
                        id=other_message_id,
                        user_id=user_id,
                        connection_id=other_connection_id,
                        thread_id=other_thread_id,
                        provider_message_id=str(local_message_id),
                        received_at=now + timedelta(minutes=1),
                        mailbox_scope_key="mailbox",
                        sender={"email": "other-peer@example.test"},
                        recipients=[{"email": "owner-1@example.test"}],
                        subject="Other source subject",
                        snippet="Other snippet",
                        labels=["INBOX"],
                        headers={},
                        provider_url="https://provider.example.test/message/other",
                    ),
                )
            )

        async with session_factory() as session:
            source = await SqlAlchemyMailSyncRepository(session).get_draft_source_message(
                user_id=user_id,
                source_thread_id=str(local_thread_id),
                source_message_id=str(local_message_id),
                source_connection_id=None,
            )

        assert source is not None
        assert source.connection_id == local_connection_id
        assert source.thread_id == "local-provider-thread"
        assert source.message_id == "local-provider-message"
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_recipient_history_is_spam_filtered_recent_and_database_bounded(
    database_url: str,
) -> None:
    """自动补全候选必须在 PostgreSQL 中按最近排序、排除 spam 并限制为 200 行。"""
    session_factory = build_session_factory(database_url)
    user_id, connection_id, thread_id = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email=f"recipient-history-{user_id}@example.test",
                    display_name="Synthetic Recipient History User",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8, 0),
                )
            )
            await session.flush()
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id=f"recipient-history-{connection_id}",
                    account_email="owner@example.test",
                    scopes=[],
                    status="connected",
                )
            )
            await session.flush()
            session.add(
                EmailThreadModel(
                    id=thread_id,
                    user_id=user_id,
                    connection_id=connection_id,
                    provider_thread_id="recipient-history-thread",
                    subject="Synthetic recipient history",
                    participants=[],
                    latest_message_at=now,
                    provider_url="https://provider.example.test/thread/recipient-history",
                )
            )
            await session.flush()
            session.add_all(
                EmailMessageModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    thread_id=thread_id,
                    provider_message_id=f"recipient-history-message-{index:03d}",
                    received_at=now - timedelta(minutes=index),
                    mailbox_scope_key="mailbox",
                    sender={"email": f"peer-{index:03d}@example.test"},
                    recipients=[],
                    subject="Synthetic recipient history",
                    snippet="Synthetic snippet",
                    labels=["INBOX"],
                    headers={},
                    provider_url=(
                        "https://provider.example.test/message/"
                        f"recipient-history-{index:03d}"
                    ),
                )
                for index in range(205)
            )
            session.add(
                EmailMessageModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    thread_id=thread_id,
                    provider_message_id="recipient-history-spam",
                    received_at=now + timedelta(minutes=1),
                    mailbox_scope_key="mailbox",
                    sender={"email": "spam-peer@example.test"},
                    recipients=[],
                    subject="Synthetic spam",
                    snippet="Synthetic spam snippet",
                    labels=["SPAM"],
                    headers={},
                    provider_url="https://provider.example.test/message/recipient-history-spam",
                )
            )

        async with session_factory() as session:
            history = await SqlAlchemyMailSyncRepository(session).list_recipient_history(
                user_id=user_id
            )

        assert len(history) == 200
        assert history[0].address == "peer-000@example.test"
        assert history[-1].address == "peer-199@example.test"
        assert all(entry.address != "spam-peer@example.test" for entry in history)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_draft_context_filters_spam_and_limits_three_rows_before_decryption(
    database_url: str,
) -> None:
    """SQL 必须先排除大小写 spam 并限三封，旧损坏密文不能阻断最新上下文。"""
    session_factory = build_session_factory(database_url)
    user_id, connection_id, thread_id = uuid4(), uuid4(), uuid4()
    source_cipher = AeadCipher(b"x" * 32)
    now = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
    provider_thread_id = "context-provider-thread"
    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email=f"context-{user_id}@example.test",
                    display_name="Synthetic Context User",
                    password_hash=None,
                    timezone="UTC",
                    locale="en-US",
                    brief_time=time(8, 0),
                )
            )
            await session.flush()
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id=f"context-{connection_id}",
                    account_email="owner@example.test",
                    scopes=[],
                    status="connected",
                )
            )
            session.add(
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability=ConnectionCapability.MAIL_READ.value,
                    status=CapabilityStatus.ENABLED.value,
                    actual_scopes=[],
                )
            )
            await session.flush()
            session.add(
                EmailThreadModel(
                    id=thread_id,
                    user_id=user_id,
                    connection_id=connection_id,
                    provider_thread_id=provider_thread_id,
                    subject="Synthetic context subject",
                    participants=[],
                    latest_message_at=now + timedelta(minutes=5),
                    provider_url="https://provider.example.test/thread/context",
                )
            )
            await session.flush()
            for index in range(6):
                provider_message_id = f"context-message-{index}"
                encrypted = source_cipher.encrypt(
                    f"Synthetic context body {index}".encode(),
                    (
                        f"{user_id}:{connection_id}:"
                        f"{provider_message_id}:body"
                    ).encode("ascii"),
                )
                labels = ["sPaM"] if index == 5 else ["INBOX"]
                if index in {1, 5}:
                    # 最新 spam 与较旧非 spam 都故意损坏；正确 SQL 两者都不会被解密。
                    ciphertext = b"not-a-valid-aead-ciphertext"
                    nonce = b"0" * 12
                    key_version = 1
                else:
                    ciphertext = encrypted.ciphertext
                    nonce = encrypted.nonce
                    key_version = encrypted.key_version
                session.add(
                    EmailMessageModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        thread_id=thread_id,
                        provider_message_id=provider_message_id,
                        received_at=now + timedelta(minutes=index),
                        mailbox_scope_key="mailbox",
                        sender={"email": "peer@example.test"},
                        recipients=[{"email": "owner@example.test"}],
                        subject="Synthetic context subject",
                        snippet=f"Synthetic snippet {index}",
                        body_ciphertext=ciphertext,
                        body_nonce=nonce,
                        body_key_version=key_version,
                        labels=labels,
                        headers={},
                        provider_url=(
                            f"https://provider.example.test/message/{index}"
                        ),
                    )
                )

        async with session_factory() as session:
            messages = await SqlAlchemyMailSyncRepository(
                session,
                source_cipher,
            ).list_draft_context_messages(
                user_id=user_id,
                connection_id=connection_id,
                source_thread_id=provider_thread_id,
            )

        assert tuple(message.message_id for message in messages) == (
            "context-message-4",
            "context-message-3",
            "context-message-2",
        )
        assert tuple(message.body_text for message in messages) == (
            "Synthetic context body 4",
            "Synthetic context body 3",
            "Synthetic context body 2",
        )
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_model_failures_keep_blank_editable_and_preserve_existing_manual_body(
    database_url: str,
) -> None:
    """失败不清空草稿；空白与人工正文均保留，元数据不保存 Prompt 或正文。"""
    session_factory = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    lease_owner = "mail-draft-generation-worker"
    action_cipher = ActionPayloadCipher.from_key(b"f" * 32)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                id=user_id,
                email=f"generation-failure-{user_id}@example.test",
                display_name="Synthetic Generation Failure",
                password_hash=None,
                timezone="UTC",
                locale="en-US",
                brief_time=time(8, 0),
            )
            connection = OAuthConnectionModel(
                id=connection_id,
                user_id=user_id,
                provider="google",
                provider_account_id=f"generation-failure-{connection_id}",
                account_email="owner@example.test",
                scopes=[],
                status="connected",
            )
            session.add_all(
                (
                    user,
                    connection,
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability=ConnectionCapability.MAIL_READ.value,
                        status="enabled",
                        actual_scopes=[],
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability=ConnectionCapability.MAIL_SEND.value,
                        status="enabled",
                        actual_scopes=[],
                    ),
                )
            )
        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, action_cipher)
            use_case = MailDraftUseCase(
                drafts=repository,
                connections=repository,
                clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
            )
            draft = await use_case.create_new(
                user_id=user_id,
                connection_id=connection_id,
                idempotency_key="generation-failure-draft",
            )
            task = TaskRunModel(
                user_id=user_id,
                kind="mail_draft.generate",
                status="running",
                lease_owner=lease_owner,
                idempotency_key="generation-failure-task",
                input_payload={
                    "draft_id": str(draft.draft_id),
                    "expected_version": 1,
                    "instruction": "Sensitive synthetic instruction that must not be stored",
                },
            )
            session.add(task)
            await session.flush()
            task_id = task.id

        step = GenerateMailDraftTaskStep(
            session_factory=session_factory,
            action_cipher=action_cipher,
            source_cipher=AeadCipher(b"s" * 32),
            model_gateway=FakeModelGateway(scenario="invalid_twice"),
            model_name="fake-mail-model",
            clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
        )
        await step.execute(
            LeasedTask(
                task_id=task_id,
                user_id=user_id,
                kind="mail_draft.generate",
                input_payload={
                    "draft_id": str(draft.draft_id),
                    "expected_version": 1,
                    "instruction": "Sensitive synthetic instruction that must not be stored",
                },
                started_at=datetime(2026, 8, 9, tzinfo=UTC),
                lease_owner=lease_owner,
            )
        )

        async with session_factory() as session:
            repository = SqlAlchemyMailDraftRepository(session, action_cipher)
            current = await repository.get_current(user_id=user_id, draft_id=draft.draft_id)
            invocation = await session.scalar(
                select(LLMInvocationModel).where(LLMInvocationModel.task_id == task_id)
            )
            task_row = await session.get(TaskRunModel, task_id)
            event = await session.scalar(
                select(AuditEventModel).where(
                    AuditEventModel.task_id == task_id,
                    AuditEventModel.event_type == "mail_draft.generation_failed",
                )
            )
            version_count = await session.scalar(
                select(func.count()).select_from(MailDraftVersionModel).where(
                    MailDraftVersionModel.draft_id == draft.draft_id
                )
            )
        assert current is not None
        assert current.current_version == 1 and current.body_text == ""
        assert current.status is MailDraftStatus.EDITING
        assert version_count == 1
        assert invocation is not None
        assert invocation.prompt_version == "mail_draft_v1"
        assert invocation.output_schema == "MailDraftModelOutput"
        assert invocation.status == "failed"
        assert task_row is not None and task_row.result_payload == {
            "draft_id": str(draft.draft_id),
            "generation_status": "failed",
            "generated_version": None,
            "error_code": "model_invalid_output",
        }
        assert event is not None and event.event_metadata == {
            "draft_id": str(draft.draft_id),
            "status": "failed",
            "error_code": "model_invalid_output",
        }
        persisted_safe_facts = str(
            {
                "invocation": {
                    "prompt_version": invocation.prompt_version,
                    "input_hash": invocation.input_hash,
                    "output_schema": invocation.output_schema,
                    "error_code": invocation.error_code,
                },
                "task_result": task_row.result_payload,
                "audit": event.event_metadata,
            }
        )
        assert "Sensitive synthetic instruction" not in persisted_safe_facts

        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, action_cipher)
            use_case = MailDraftUseCase(
                drafts=repository,
                connections=repository,
                clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
            )
            manual = await use_case.update(
                UpdateMailDraftInput(
                    user_id=user_id,
                    draft_id=draft.draft_id,
                    expected_version=1,
                    body_text="Synthetic manually edited body.",
                )
            )
            timeout_task = TaskRunModel(
                user_id=user_id,
                kind="mail_draft.generate",
                status="running",
                lease_owner=lease_owner,
                idempotency_key="generation-timeout-task",
                input_payload={
                    "draft_id": str(draft.draft_id),
                    "expected_version": manual.current_version,
                    "instruction": "Rewrite the synthetic manual body",
                },
            )
            session.add(timeout_task)
            await session.flush()
            timeout_task_id = timeout_task.id

        timeout_step = GenerateMailDraftTaskStep(
            session_factory=session_factory,
            action_cipher=action_cipher,
            source_cipher=AeadCipher(b"s" * 32),
            model_gateway=_TimeoutModelGateway(),
            model_name="fake-mail-model",
            clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
        )
        await timeout_step.execute(
            LeasedTask(
                task_id=timeout_task_id,
                user_id=user_id,
                kind="mail_draft.generate",
                input_payload={
                    "draft_id": str(draft.draft_id),
                    "expected_version": 2,
                    "instruction": "Rewrite the synthetic manual body",
                },
                started_at=datetime(2026, 8, 9, tzinfo=UTC),
                lease_owner=lease_owner,
            )
        )

        async with session_factory() as session:
            repository = SqlAlchemyMailDraftRepository(session, action_cipher)
            after_timeout = await repository.get_current(
                user_id=user_id,
                draft_id=draft.draft_id,
            )
            timeout_invocation = await session.scalar(
                select(LLMInvocationModel).where(
                    LLMInvocationModel.task_id == timeout_task_id
                )
            )
            timeout_task_row = await session.get(TaskRunModel, timeout_task_id)
            version_count_after_timeout = await session.scalar(
                select(func.count()).select_from(MailDraftVersionModel).where(
                    MailDraftVersionModel.draft_id == draft.draft_id
                )
            )
        assert after_timeout is not None
        assert after_timeout.current_version == 2
        assert after_timeout.body_text == "Synthetic manually edited body."
        assert after_timeout.status is MailDraftStatus.EDITING
        assert version_count_after_timeout == 2
        assert timeout_invocation is not None
        assert timeout_invocation.status == "failed"
        assert timeout_invocation.error_code == "model_timeout"
        assert timeout_task_row is not None and timeout_task_row.result_payload == {
            "draft_id": str(draft.draft_id),
            "generation_status": "failed",
            "generated_version": None,
            "error_code": "model_timeout",
        }

        async with session_factory.begin() as session:
            success_task = TaskRunModel(
                user_id=user_id,
                kind="mail_draft.generate",
                status="running",
                lease_owner=lease_owner,
                idempotency_key="generation-success-task",
                input_payload={
                    "draft_id": str(draft.draft_id),
                    "expected_version": 2,
                    "instruction": "Reply with the supplied synthetic facts only",
                },
            )
            session.add(success_task)
            await session.flush()
            success_task_id = success_task.id
        success_step = GenerateMailDraftTaskStep(
            session_factory=session_factory,
            action_cipher=action_cipher,
            source_cipher=AeadCipher(b"s" * 32),
            model_gateway=FakeModelGateway(),
            model_name="fake-mail-model",
            clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
        )
        await success_step.execute(
            LeasedTask(
                task_id=success_task_id,
                user_id=user_id,
                kind="mail_draft.generate",
                input_payload={
                    "draft_id": str(draft.draft_id),
                    "expected_version": 2,
                    "instruction": "Reply with the supplied synthetic facts only",
                },
                started_at=datetime(2026, 8, 9, tzinfo=UTC),
                lease_owner=lease_owner,
            )
        )
        async with session_factory() as session:
            repository = SqlAlchemyMailDraftRepository(session, action_cipher)
            generated = await repository.get_current(
                user_id=user_id,
                draft_id=draft.draft_id,
            )
            success_invocation = await session.scalar(
                select(LLMInvocationModel).where(
                    LLMInvocationModel.task_id == success_task_id
                )
            )
        assert generated is not None
        assert generated.current_version == 3
        assert generated.body_text == "Synthetic mail draft body."
        assert generated.prompt_version == "mail_draft_v1"
        assert generated.model_name == "fake-mail-model"
        assert success_invocation is not None and success_invocation.status == "succeeded"
    finally:
        await session_factory.dispose()
