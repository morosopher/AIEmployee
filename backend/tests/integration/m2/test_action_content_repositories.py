"""在真实 PostgreSQL 上验证 M2 加密内容 Repository 的事务与隔离不变量。"""

import asyncio
import hashlib
from collections.abc import ItemsView, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import cast
from uuid import UUID

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import event, func, select, text, update

from ai_employee.application.commands import trusted_command_hash
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.use_cases.mail_drafts import MailDraftRecipient
from ai_employee.domain.actions import CalendarProposalStatus, MailDraftStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode
from ai_employee.domain.tasks import ApprovalProposal, ApprovalStatus
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
)
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    CALENDAR_SNAPSHOT_ACTION,
    CALENDAR_SNAPSHOT_CONTENT_KIND,
    CALENDAR_SNAPSHOT_SCHEMA_VERSION,
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.repositories.mail_drafts import SqlAlchemyMailDraftRepository
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepository,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.action_payloads import (
    ActionPayloadCipher,
    ActionPayloadFormatError,
    action_payload_aad,
)
from ai_employee.infrastructure.security.encryption import AeadCipher

FIXED_RETAIN_UNTIL = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)
FIXED_NEXT_RETAIN_UNTIL = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
FIRST_USER_ID = UUID("00000000-0000-0000-0000-000000000611")
SECOND_USER_ID = UUID("00000000-0000-0000-0000-000000000612")
FIRST_CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000613")
SECOND_CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000614")
MAIL_DRAFT_ID = UUID("00000000-0000-0000-0000-000000000615")
MAIL_VERSION_ONE_ID = UUID("00000000-0000-0000-0000-000000000616")
MAIL_VERSION_TWO_FIRST_ID = UUID("00000000-0000-0000-0000-000000000617")
MAIL_VERSION_TWO_SECOND_ID = UUID("00000000-0000-0000-0000-000000000618")
SECOND_MAIL_DRAFT_ID = UUID("00000000-0000-0000-0000-000000000619")
SECOND_MAIL_VERSION_ID = UUID("00000000-0000-0000-0000-000000000620")
THIRD_MAIL_DRAFT_ID = UUID("00000000-0000-0000-0000-000000000640")
THIRD_MAIL_VERSION_ID = UUID("00000000-0000-0000-0000-000000000641")
CALENDAR_PROPOSAL_ID = UUID("00000000-0000-0000-0000-000000000621")
CALENDAR_DESIRED_ONE_ID = UUID("00000000-0000-0000-0000-000000000622")
CALENDAR_DESIRED_TWO_ID = UUID("00000000-0000-0000-0000-000000000623")
CALENDAR_BEFORE_ID = UUID("00000000-0000-0000-0000-000000000624")
RESTORE_PROPOSAL_ID = UUID("00000000-0000-0000-0000-000000000625")
RESTORE_DESIRED_ID = UUID("00000000-0000-0000-0000-000000000626")
TASK_ID = UUID("00000000-0000-0000-0000-000000000627")
M2_STEP_ID = UUID("00000000-0000-0000-0000-000000000628")
COPY_STEP_ID = UUID("00000000-0000-0000-0000-000000000629")
LEGACY_STEP_ID = UUID("00000000-0000-0000-0000-000000000630")
INVALID_LEGACY_STEP_ID = UUID("00000000-0000-0000-0000-000000000631")
APPROVAL_ID = UUID("00000000-0000-0000-0000-000000000632")
COPY_APPROVAL_ID = UUID("00000000-0000-0000-0000-000000000633")
LEGACY_APPROVAL_ID = UUID("00000000-0000-0000-0000-000000000634")
INVALID_LEGACY_APPROVAL_ID = UUID("00000000-0000-0000-0000-000000000635")
OPERATION_ID = UUID("00000000-0000-0000-0000-000000000636")
MAIL_BODY_ONE = "synthetic-mail-body-marker-one"
MAIL_BODY_TWO_FIRST = "synthetic-mail-body-marker-two-first"
MAIL_BODY_TWO_SECOND = "synthetic-mail-body-marker-two-second"
CALENDAR_CONTENT_MARKER = "synthetic-calendar-snapshot-marker"
TRUSTED_COMMAND_BODY = "synthetic-trusted-command-body-marker"


class _ExplodingMapping(Mapping[str, object]):
    """记录 ``items`` 调用并抛出携带合成日程正文的异常。"""

    def __init__(self, marker: str) -> None:
        self._marker = marker
        self.items_calls = 0

    def __getitem__(self, key: str) -> object:
        if key != "description":
            raise KeyError(key)
        return self._marker

    def __iter__(self) -> Iterator[str]:
        return iter(("description",))

    def __len__(self) -> int:
        return 1

    def items(self) -> ItemsView[str, object]:
        self.items_calls += 1
        raise RuntimeError(f"calendar mapping exposed {self._marker}")


class _StatefulMapping(Mapping[str, object]):
    """每次遍历都返回不同日程正文，用于暴露双重规范化竞态。"""

    def __init__(self, marker: str) -> None:
        self._marker = marker
        self.items_calls = 0

    def __getitem__(self, key: str) -> object:
        if key != "description":
            raise KeyError(key)
        return f"{self._marker}-fallback"

    def __iter__(self) -> Iterator[str]:
        return iter(("description",))

    def __len__(self) -> int:
        return 1

    def items(self) -> ItemsView[str, object]:
        self.items_calls += 1
        return {"description": f"{self._marker}-{self.items_calls}"}.items()


@dataclass(frozen=True, slots=True)
class ActionContentFacts:
    """保存两名合成用户及其独立 OAuth 连接。"""

    first_user_id: UUID
    second_user_id: UUID
    first_connection_id: UUID
    second_connection_id: UUID


@dataclass(frozen=True, slots=True)
class ApprovalFacts:
    """保存 M2、密文替换目标和两条 legacy 审批标识。"""

    task_id: UUID
    approval_id: UUID
    copy_approval_id: UUID
    legacy_approval_id: UUID
    invalid_legacy_approval_id: UUID


def _approval_freeze_state(approval: ApprovalRequestModel) -> tuple[object, ...]:
    """复制首次冻结会观察或修改的全部字段，验证拒绝路径完全无 mutation。"""
    return (
        approval.status,
        approval.decided_at,
        approval.decided_by_user_id,
        approval.approved_execution_deadline_at,
        dict(approval.payload),
        approval.payload_hash,
        approval.payload_ciphertext,
        approval.payload_nonce,
        approval.payload_key_version,
    )


def _assert_action_payload_format_error_is_content_free(
    error: BaseException,
    *,
    sensitive_marker: str,
) -> None:
    """验证 Repository 内容准备错误不保留敏感正文或底层解析器状态。"""
    assert type(error) is ActionPayloadFormatError
    assert str(error) == "action payload format is invalid"
    assert sensitive_marker not in str(error)
    assert sensitive_marker not in repr(error)
    assert not hasattr(error, "object")
    assert not hasattr(error, "doc")
    assert error.__cause__ is None
    assert error.__context__ is None


def _synthetic_user(*, user_id: UUID, ordinal: str) -> UserModel:
    """构造不含真实个人数据且显式使用 UTC 的测试用户。"""
    return UserModel(
        id=user_id,
        email=f"action-content-{ordinal}@example.test",
        display_name=f"Action Content {ordinal}",
        password_hash=None,
        timezone="UTC",
        locale="zh-CN",
        brief_time=time(8, 0),
        is_active=True,
    )


async def _seed_users_and_connections(
    session_factory: ManagedAsyncSessionMaker,
) -> ActionContentFacts:
    """提交两套互不归属的用户与连接，供 Repository 逐层验证用户条件。"""
    async with session_factory.begin() as session:
        first_user = _synthetic_user(user_id=FIRST_USER_ID, ordinal="first")
        second_user = _synthetic_user(user_id=SECOND_USER_ID, ordinal="second")
        first_connection = OAuthConnectionModel(
            id=FIRST_CONNECTION_ID,
            user_id=FIRST_USER_ID,
            provider="google",
            provider_account_id="action-content-first",
            account_email="action-content-first@example.test",
            scopes=[],
            status="connected",
            last_error_code=None,
        )
        second_connection = OAuthConnectionModel(
            id=SECOND_CONNECTION_ID,
            user_id=SECOND_USER_ID,
            provider="microsoft",
            provider_account_id="action-content-second",
            provider_tenant_id="synthetic-tenant",
            account_type="work_school",
            account_email="action-content-second@example.test",
            scopes=[],
            status="connected",
            last_error_code=None,
        )
        session.add_all((first_user, second_user))
        await session.flush()
        session.add_all((first_connection, second_connection))

    return ActionContentFacts(
        first_user_id=FIRST_USER_ID,
        second_user_id=SECOND_USER_ID,
        first_connection_id=FIRST_CONNECTION_ID,
        second_connection_id=SECOND_CONNECTION_ID,
    )


async def _seed_trusted_approvals(
    session_factory: ManagedAsyncSessionMaker,
    facts: ActionContentFacts,
) -> ApprovalFacts:
    """为同一用户提交 M2、替换目标和 legacy 审批骨架。"""
    trusted_payload_hash = trusted_command_hash(_mail_command(facts))
    legacy_payload = {"synthetic": True}
    legacy_payload_hash = ApprovalProposal.create(
        "fake.write",
        legacy_payload,
    ).payload_hash
    async with session_factory.begin() as session:
        task = TaskRunModel(
            id=TASK_ID,
            user_id=facts.first_user_id,
            kind="trusted_action",
            status="created",
            idempotency_key="action-content:task",
            input_payload={"synthetic": True},
        )
        steps = (
            TaskStepModel(
                id=M2_STEP_ID,
                task_id=TASK_ID,
                sequence=1,
                name="m2-command",
                kind="approval",
                status="pending",
                input_summary={},
            ),
            TaskStepModel(
                id=COPY_STEP_ID,
                task_id=TASK_ID,
                sequence=2,
                name="copy-target",
                kind="approval",
                status="pending",
                input_summary={},
            ),
            TaskStepModel(
                id=LEGACY_STEP_ID,
                task_id=TASK_ID,
                sequence=3,
                name="legacy-fake-write",
                kind="approval",
                status="pending",
                input_summary={},
            ),
            TaskStepModel(
                id=INVALID_LEGACY_STEP_ID,
                task_id=TASK_ID,
                sequence=4,
                name="invalid-legacy",
                kind="approval",
                status="pending",
                input_summary={},
            ),
        )
        session.add(task)
        session.add_all(steps)
        await session.flush()
        session.add_all(
            (
                ApprovalRequestModel(
                    id=APPROVAL_ID,
                    task_id=TASK_ID,
                    step_id=M2_STEP_ID,
                    version=1,
                    action="mail.send",
                    schema_version="mail_send.v1",
                    payload={},
                    payload_hash=trusted_payload_hash,
                    preview_markdown="Synthetic M2 approval preview",
                    status="pending",
                    expires_at=FIXED_RETAIN_UNTIL,
                ),
                ApprovalRequestModel(
                    id=COPY_APPROVAL_ID,
                    task_id=TASK_ID,
                    step_id=COPY_STEP_ID,
                    version=1,
                    action="mail.send",
                    schema_version="mail_send.v1",
                    payload={},
                    payload_hash=trusted_payload_hash,
                    preview_markdown="Synthetic copy target preview",
                    status="pending",
                    expires_at=FIXED_RETAIN_UNTIL,
                ),
                ApprovalRequestModel(
                    id=LEGACY_APPROVAL_ID,
                    task_id=TASK_ID,
                    step_id=LEGACY_STEP_ID,
                    version=1,
                    action="fake.write",
                    schema_version=None,
                    payload=legacy_payload,
                    payload_hash=legacy_payload_hash,
                    preview_markdown="Synthetic legacy preview",
                    status="pending",
                    expires_at=FIXED_RETAIN_UNTIL,
                ),
                ApprovalRequestModel(
                    id=INVALID_LEGACY_APPROVAL_ID,
                    task_id=TASK_ID,
                    step_id=INVALID_LEGACY_STEP_ID,
                    version=1,
                    action="mail.send",
                    schema_version=None,
                    payload={"synthetic": True},
                    payload_hash="d" * 64,
                    preview_markdown="Synthetic invalid legacy preview",
                    status="pending",
                    expires_at=FIXED_RETAIN_UNTIL,
                ),
            )
        )

    return ApprovalFacts(
        task_id=TASK_ID,
        approval_id=APPROVAL_ID,
        copy_approval_id=COPY_APPROVAL_ID,
        legacy_approval_id=LEGACY_APPROVAL_ID,
        invalid_legacy_approval_id=INVALID_LEGACY_APPROVAL_ID,
    )


def _mail_command(facts: ActionContentFacts) -> dict[str, object]:
    """构造只含标准 JSON 类型且不包含真实地址或正文的可信邮件命令。"""
    return {
        "schema_version": "mail_send.v1",
        "action": "mail.send",
        "operation_id": str(OPERATION_ID),
        "connection_id": str(facts.first_connection_id),
        "draft_id": str(MAIL_DRAFT_ID),
        "draft_version": 1,
        "message_date": "2026-08-07T08:00:00Z",
        "mode": "new",
        "source_thread_id": None,
        "source_message_id": None,
        "to": ["recipient@example.test"],
        "cc": [],
        "bcc": [],
        "subject": "Synthetic trusted subject",
        "body_text": TRUSTED_COMMAND_BODY,
        "thread_headers": None,
    }


async def _create_mail_draft(
    session_factory: ManagedAsyncSessionMaker,
    facts: ActionContentFacts,
    cipher: ActionPayloadCipher,
    *,
    draft_id: UUID = MAIL_DRAFT_ID,
    version_id: UUID = MAIL_VERSION_ONE_ID,
    creation_key: str = "mail:create:one",
    body_text: str = MAIL_BODY_ONE,
) -> None:
    """在调用方事务中创建一个可编辑的本地加密草稿。"""
    async with session_factory.begin() as session:
        repository = SqlAlchemyMailDraftRepository(session, cipher)
        await repository.create(
            draft_id=draft_id,
            version_id=version_id,
            user_id=facts.first_user_id,
            connection_id=facts.first_connection_id,
            creation_idempotency_key=creation_key,
            creation_payload_hash="1" * 64,
            source_thread_id=None,
            source_message_id=None,
            mode=MailMode.NEW,
            retain_until=FIXED_RETAIN_UNTIL,
            to_recipients=(MailDraftRecipient(address="recipient@example.test"),),
            cc_recipients=(),
            bcc_recipients=(),
            subject="Synthetic subject",
            body_text=body_text,
            prompt_version=None,
            model_name=None,
        )


@pytest.mark.asyncio
async def test_mail_creation_replay_is_hash_bound_and_user_scoped(database_url: str) -> None:
    """同用户同键同哈希复用一行，同键异哈希拒绝且跨用户表现为不存在。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    try:
        facts = await _seed_users_and_connections(session_factory)
        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, cipher)
            first = await repository.create(
                draft_id=MAIL_DRAFT_ID,
                version_id=MAIL_VERSION_ONE_ID,
                user_id=facts.first_user_id,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="mail:create:one",
                creation_payload_hash="1" * 64,
                source_thread_id=None,
                source_message_id=None,
                mode=MailMode.NEW,
                retain_until=FIXED_RETAIN_UNTIL,
                to_recipients=(MailDraftRecipient(address="recipient@example.test"),),
                cc_recipients=(),
                bcc_recipients=(),
                subject="Synthetic subject",
                body_text=MAIL_BODY_ONE,
                prompt_version="mail_draft_v1",
                model_name="synthetic-model",
            )
            replay = await repository.create(
                draft_id=SECOND_MAIL_DRAFT_ID,
                version_id=SECOND_MAIL_VERSION_ID,
                user_id=facts.first_user_id,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="mail:create:one",
                creation_payload_hash="1" * 64,
                source_thread_id=None,
                source_message_id=None,
                mode=MailMode.NEW,
                retain_until=FIXED_RETAIN_UNTIL,
                to_recipients=(MailDraftRecipient(address="recipient@example.test"),),
                cc_recipients=(),
                bcc_recipients=(),
                subject="Synthetic subject",
                body_text=MAIL_BODY_ONE,
                prompt_version="mail_draft_v1",
                model_name="synthetic-model",
            )

            assert first.draft_id == MAIL_DRAFT_ID
            assert replay.draft_id == first.draft_id
            assert replay.version_id == first.version_id
            assert replay.body_text == MAIL_BODY_ONE
            assert (
                await repository.get_current(
                    user_id=facts.second_user_id,
                    draft_id=MAIL_DRAFT_ID,
                )
                is None
            )

            with pytest.raises(StateConflictError) as mismatch:
                await repository.create(
                    draft_id=SECOND_MAIL_DRAFT_ID,
                    version_id=SECOND_MAIL_VERSION_ID,
                    user_id=facts.first_user_id,
                    connection_id=facts.first_connection_id,
                    creation_idempotency_key="mail:create:one",
                    creation_payload_hash="2" * 64,
                    source_thread_id=None,
                    source_message_id=None,
                    mode=MailMode.NEW,
                    retain_until=FIXED_RETAIN_UNTIL,
                    to_recipients=(MailDraftRecipient(address="recipient@example.test"),),
                    cc_recipients=(),
                    bcc_recipients=(),
                    subject="Synthetic subject",
                    body_text="different synthetic body",
                    prompt_version=None,
                    model_name=None,
                )

            assert mismatch.value.error_code == "idempotency_key_payload_mismatch"
            assert "mail:create:one" not in mismatch.value.message
            assert MAIL_BODY_ONE not in mismatch.value.message

        async with session_factory() as session:
            draft_count = await session.scalar(select(func.count()).select_from(MailDraftModel))
            version_count = await session.scalar(
                select(func.count()).select_from(MailDraftVersionModel)
            )
        assert draft_count == 1
        assert version_count == 1
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_mail_list_current_loads_all_current_versions_in_one_query(
    database_url: str,
) -> None:
    """有界草稿列表必须批量连接当前版本，查询数不能随返回草稿数量增长。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"q" * 32)
    statements: list[str] = []
    listener_installed = False

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        """只记录命中草稿父表或版本表的 SQL，忽略连接池探活。"""
        if "mail_drafts" in statement or "mail_draft_versions" in statement:
            statements.append(statement)

    try:
        facts = await _seed_users_and_connections(session_factory)
        await _create_mail_draft(session_factory, facts, cipher)
        await _create_mail_draft(
            session_factory,
            facts,
            cipher,
            draft_id=SECOND_MAIL_DRAFT_ID,
            version_id=SECOND_MAIL_VERSION_ID,
            creation_key="mail:create:two",
            body_text="synthetic-mail-body-two",
        )
        await _create_mail_draft(
            session_factory,
            facts,
            cipher,
            draft_id=THIRD_MAIL_DRAFT_ID,
            version_id=THIRD_MAIL_VERSION_ID,
            creation_key="mail:create:three",
            body_text="synthetic-mail-body-three",
        )
        event.listen(
            session_factory.engine.sync_engine,
            "before_cursor_execute",
            capture_statement,
        )
        listener_installed = True

        async with session_factory() as session:
            drafts = await SqlAlchemyMailDraftRepository(session, cipher).list_current(
                user_id=facts.first_user_id,
                limit=10,
                offset=0,
            )

        assert len(drafts) == 3
        assert len(statements) == 1
    finally:
        if listener_installed:
            event.remove(
                session_factory.engine.sync_engine,
                "before_cursor_execute",
                capture_statement,
            )
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_mail_create_preparation_failure_commits_no_partial_rows(
    database_url: str,
) -> None:
    """recipient 转换失败被调用方捕获后，正常提交也不能留下孤立草稿头。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    invalid_recipients = cast(tuple[MailDraftRecipient, ...], ("invalid-recipient",))
    try:
        facts = await _seed_users_and_connections(session_factory)
        caught_error: TypeError | ValueError | None = None
        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, cipher)
            try:
                await repository.create(
                    draft_id=MAIL_DRAFT_ID,
                    version_id=MAIL_VERSION_ONE_ID,
                    user_id=facts.first_user_id,
                    connection_id=facts.first_connection_id,
                    creation_idempotency_key="mail:create:preparation-failure",
                    creation_payload_hash="1" * 64,
                    source_thread_id=None,
                    source_message_id=None,
                    mode=MailMode.NEW,
                    retain_until=FIXED_RETAIN_UNTIL,
                    to_recipients=invalid_recipients,
                    cc_recipients=(),
                    bcc_recipients=(),
                    subject="Synthetic subject",
                    body_text=MAIL_BODY_ONE,
                    prompt_version=None,
                    model_name=None,
                )
            except (TypeError, ValueError) as error:
                # 故意吞掉内容准备异常，让外层事务走正常 commit，验证 Repository 零 mutation。
                caught_error = error
        assert caught_error is not None

        async with session_factory() as session:
            draft_count = await session.scalar(
                select(func.count())
                .select_from(MailDraftModel)
                .where(MailDraftModel.id == MAIL_DRAFT_ID)
            )
            version_count = await session.scalar(
                select(func.count())
                .select_from(MailDraftVersionModel)
                .where(MailDraftVersionModel.draft_id == MAIL_DRAFT_ID)
            )
        assert draft_count == 0
        assert version_count == 0
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_mail_next_version_preparation_failure_keeps_parent_and_versions_unchanged(
    database_url: str,
) -> None:
    """recipient 转换失败被捕获并提交后，CAS 版本和保留截止时间必须原样保留。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    invalid_recipients = cast(tuple[MailDraftRecipient, ...], ("invalid-recipient",))
    try:
        facts = await _seed_users_and_connections(session_factory)
        await _create_mail_draft(session_factory, facts, cipher)
        caught_error: TypeError | ValueError | None = None
        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, cipher)
            try:
                await repository.save_next_version(
                    version_id=MAIL_VERSION_TWO_FIRST_ID,
                    user_id=facts.first_user_id,
                    draft_id=MAIL_DRAFT_ID,
                    expected_version=1,
                    to_recipients=invalid_recipients,
                    cc_recipients=(),
                    bcc_recipients=(),
                    subject="Synthetic next subject",
                    body_text=MAIL_BODY_TWO_FIRST,
                    prompt_version=None,
                    model_name=None,
                    retain_until=FIXED_NEXT_RETAIN_UNTIL,
                )
            except (TypeError, ValueError) as error:
                caught_error = error
        assert caught_error is not None

        async with session_factory() as session:
            draft = await session.get(MailDraftModel, MAIL_DRAFT_ID)
            version_count = await session.scalar(
                select(func.count())
                .select_from(MailDraftVersionModel)
                .where(MailDraftVersionModel.draft_id == MAIL_DRAFT_ID)
            )
        assert draft is not None
        assert draft.current_version == 1
        assert draft.retain_until == FIXED_RETAIN_UNTIL
        assert version_count == 1
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_concurrent_mail_next_version_uses_cas_and_never_stores_plaintext(
    database_url: str,
) -> None:
    """并发保存同一 expected version 只有一个赢家，陈旧调用返回稳定冲突。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    try:
        facts = await _seed_users_and_connections(session_factory)
        await _create_mail_draft(session_factory, facts, cipher)

        async def save(version_id: UUID, body_text: str) -> object:
            """在独立真实事务中竞争同一父行 CAS。"""
            async with session_factory.begin() as session:
                repository = SqlAlchemyMailDraftRepository(session, cipher)
                return await repository.save_next_version(
                    version_id=version_id,
                    user_id=facts.first_user_id,
                    draft_id=MAIL_DRAFT_ID,
                    expected_version=1,
                    to_recipients=(MailDraftRecipient(address="recipient@example.test"),),
                    cc_recipients=(),
                    bcc_recipients=(),
                    subject="Synthetic next subject",
                    body_text=body_text,
                    prompt_version=None,
                    model_name=None,
                    retain_until=FIXED_NEXT_RETAIN_UNTIL,
                )

        results = await asyncio.gather(
            save(MAIL_VERSION_TWO_FIRST_ID, MAIL_BODY_TWO_FIRST),
            save(MAIL_VERSION_TWO_SECOND_ID, MAIL_BODY_TWO_SECOND),
            return_exceptions=True,
        )
        conflicts = [result for result in results if isinstance(result, StateConflictError)]
        successes = [result for result in results if not isinstance(result, BaseException)]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert conflicts[0].error_code == "draft_version_conflict"
        assert conflicts[0].message == "mail draft version changed"

        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, cipher)
            with pytest.raises(StateConflictError) as stale:
                await repository.save_next_version(
                    version_id=UUID("00000000-0000-0000-0000-000000000637"),
                    user_id=facts.first_user_id,
                    draft_id=MAIL_DRAFT_ID,
                    expected_version=1,
                    to_recipients=(MailDraftRecipient(address="recipient@example.test"),),
                    cc_recipients=(),
                    bcc_recipients=(),
                    subject="Synthetic stale subject",
                    body_text="synthetic stale body marker",
                    prompt_version=None,
                    model_name=None,
                    retain_until=FIXED_NEXT_RETAIN_UNTIL,
                )
            assert stale.value.error_code == "draft_version_conflict"
            assert stale.value.message == "mail draft version changed"

        async with session_factory() as session:
            version_two_count = await session.scalar(
                select(func.count())
                .select_from(MailDraftVersionModel)
                .where(
                    MailDraftVersionModel.draft_id == MAIL_DRAFT_ID,
                    MailDraftVersionModel.version == 2,
                )
            )
            rows = tuple(
                (
                    await session.execute(
                        text(
                            "SELECT body_ciphertext, to_jsonb(mail_draft_versions)::text "
                            "FROM mail_draft_versions WHERE draft_id = :draft_id"
                        ),
                        {"draft_id": MAIL_DRAFT_ID},
                    )
                ).all()
            )
            draft = await session.get(MailDraftModel, MAIL_DRAFT_ID)
        assert version_two_count == 1
        assert draft is not None
        assert draft.retain_until == FIXED_NEXT_RETAIN_UNTIL
        for ciphertext, serialized_row in rows:
            assert isinstance(ciphertext, bytes)
            assert isinstance(serialized_row, str)
            for marker in (MAIL_BODY_ONE, MAIL_BODY_TWO_FIRST, MAIL_BODY_TWO_SECOND):
                assert marker.encode("utf-8") not in ciphertext
                assert marker not in serialized_row
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_mail_lock_cancel_and_retention_fail_closed(database_url: str) -> None:
    """提交锁返回精确版本，取消遵守状态机，正文清除后读取不能伪造空值。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    try:
        facts = await _seed_users_and_connections(session_factory)
        await _create_mail_draft(session_factory, facts, cipher)
        await _create_mail_draft(
            session_factory,
            facts,
            cipher,
            draft_id=SECOND_MAIL_DRAFT_ID,
            version_id=SECOND_MAIL_VERSION_ID,
            creation_key="mail:create:second",
        )

        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, cipher)
            locked = await repository.lock_for_submit(
                user_id=facts.first_user_id,
                draft_id=MAIL_DRAFT_ID,
            )
            assert locked is not None
            assert locked.current_version == 1
            assert locked.version == 1
            assert (
                await repository.lock_for_submit(
                    user_id=facts.second_user_id,
                    draft_id=MAIL_DRAFT_ID,
                )
                is None
            )

            await session.execute(
                update(MailDraftModel)
                .where(MailDraftModel.id == MAIL_DRAFT_ID)
                .values(status=MailDraftStatus.AWAITING_APPROVAL.value)
            )
            with pytest.raises(StateConflictError) as awaiting_approval:
                await repository.cancel(
                    user_id=facts.first_user_id,
                    draft_id=MAIL_DRAFT_ID,
                )
            assert (
                awaiting_approval.value.error_code
                == "mail_draft_approval_withdrawal_required"
            )
            awaiting_row = await session.get(MailDraftModel, MAIL_DRAFT_ID)
            assert awaiting_row is not None
            assert awaiting_row.status == MailDraftStatus.AWAITING_APPROVAL.value

            await session.execute(
                update(MailDraftModel)
                .where(MailDraftModel.id == MAIL_DRAFT_ID)
                .values(status=MailDraftStatus.NEEDS_ATTENTION.value)
            )
            with pytest.raises(StateConflictError) as needs_attention:
                await repository.cancel(
                    user_id=facts.first_user_id,
                    draft_id=MAIL_DRAFT_ID,
                )
            assert (
                needs_attention.value.error_code
                == "mail_draft_result_confirmation_required"
            )
            attention_row = await session.get(MailDraftModel, MAIL_DRAFT_ID)
            assert attention_row is not None
            assert attention_row.status == MailDraftStatus.NEEDS_ATTENTION.value

            # 后续正文保留断言与取消用例使用原有 editing 草稿；本测试不模拟任务撤回。
            await session.execute(
                update(MailDraftModel)
                .where(MailDraftModel.id == MAIL_DRAFT_ID)
                .values(status=MailDraftStatus.EDITING.value)
            )

            cancelled = await repository.cancel(
                user_id=facts.first_user_id,
                draft_id=SECOND_MAIL_DRAFT_ID,
            )
            assert cancelled is not None
            assert cancelled.status is MailDraftStatus.CANCELLED
            with pytest.raises(StateConflictError) as terminal:
                await repository.cancel(
                    user_id=facts.first_user_id,
                    draft_id=SECOND_MAIL_DRAFT_ID,
                )
            assert terminal.value.error_code == "invalid_action_transition"

            await session.execute(
                update(MailDraftVersionModel)
                .where(MailDraftVersionModel.id == MAIL_VERSION_ONE_ID)
                .values(
                    body_ciphertext=None,
                    body_nonce=None,
                    body_key_version=None,
                )
            )
            with pytest.raises(StateConflictError) as cleared:
                await repository.get_current(
                    user_id=facts.first_user_id,
                    draft_id=MAIL_DRAFT_ID,
                )
            assert cleared.value.error_code == "mail_draft_content_unavailable"
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_calendar_versions_snapshots_and_restore_are_encrypted_and_hash_bound(
    database_url: str,
) -> None:
    """提案版本、补偿快照和恢复提案均保持新事实、用户隔离与明文不落库。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    desired = {
        "title": "Synthetic title",
        "description": CALENDAR_CONTENT_MARKER,
        "location": "Synthetic room",
    }
    before = {
        "title": "Synthetic old title",
        "description": f"{CALENDAR_CONTENT_MARKER}-before",
        "location": None,
    }
    try:
        facts = await _seed_users_and_connections(session_factory)
        async with session_factory.begin() as session:
            repository = SqlAlchemyCalendarProposalRepository(session, cipher)
            created = await repository.create(
                proposal_id=CALENDAR_PROPOSAL_ID,
                snapshot_id=CALENDAR_DESIRED_ONE_ID,
                user_id=facts.first_user_id,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="calendar:create:one",
                creation_payload_hash="3" * 64,
                calendar_id="primary",
                operation_kind="update",
                target_event_id="synthetic-event",
                base_etag="synthetic-etag-one",
                retain_until=FIXED_RETAIN_UNTIL,
                desired_state=desired,
            )
            replay = await repository.create(
                proposal_id=RESTORE_PROPOSAL_ID,
                snapshot_id=RESTORE_DESIRED_ID,
                user_id=facts.first_user_id,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="calendar:create:one",
                creation_payload_hash="3" * 64,
                calendar_id="primary",
                operation_kind="update",
                target_event_id="synthetic-event",
                base_etag="synthetic-etag-one",
                retain_until=FIXED_RETAIN_UNTIL,
                desired_state=desired,
            )
            assert created.proposal_id == CALENDAR_PROPOSAL_ID
            assert replay.proposal_id == created.proposal_id
            assert replay.desired_snapshot.snapshot_id == created.desired_snapshot.snapshot_id
            assert (
                await repository.get_current(
                    user_id=facts.second_user_id,
                    proposal_id=CALENDAR_PROPOSAL_ID,
                )
                is None
            )

            with pytest.raises(StateConflictError) as mismatch:
                await repository.create(
                    proposal_id=RESTORE_PROPOSAL_ID,
                    snapshot_id=RESTORE_DESIRED_ID,
                    user_id=facts.first_user_id,
                    connection_id=facts.first_connection_id,
                    creation_idempotency_key="calendar:create:one",
                    creation_payload_hash="4" * 64,
                    calendar_id="primary",
                    operation_kind="update",
                    target_event_id="synthetic-event",
                    base_etag="synthetic-etag-one",
                    retain_until=FIXED_RETAIN_UNTIL,
                    desired_state={"description": "different synthetic calendar body"},
                )
            assert mismatch.value.error_code == "idempotency_key_payload_mismatch"

            saved_before = await repository.save_snapshot(
                snapshot_id=CALENDAR_BEFORE_ID,
                user_id=facts.first_user_id,
                proposal_id=CALENDAR_PROPOSAL_ID,
                version=1,
                snapshot_kind="before",
                content=before,
                retain_until=FIXED_RETAIN_UNTIL,
            )
            assert saved_before is not None
            assert saved_before.content == before
            assert (
                await repository.load_snapshot(
                    user_id=facts.second_user_id,
                    snapshot_id=CALENDAR_BEFORE_ID,
                )
                is None
            )

            version_two = await repository.save_next_version(
                snapshot_id=CALENDAR_DESIRED_TWO_ID,
                user_id=facts.first_user_id,
                proposal_id=CALENDAR_PROPOSAL_ID,
                expected_version=1,
                desired_state={**desired, "location": "Synthetic next room"},
                retain_until=FIXED_RETAIN_UNTIL,
            )
            assert version_two.current_version == 2
            with pytest.raises(StateConflictError) as stale:
                await repository.save_next_version(
                    snapshot_id=UUID("00000000-0000-0000-0000-000000000638"),
                    user_id=facts.first_user_id,
                    proposal_id=CALENDAR_PROPOSAL_ID,
                    expected_version=1,
                    desired_state=desired,
                    retain_until=FIXED_RETAIN_UNTIL,
                )
            assert stale.value.error_code == "proposal_version_conflict"
            assert stale.value.message == "calendar proposal version changed"

        async with session_factory.begin() as session:
            repository = SqlAlchemyCalendarProposalRepository(session, cipher)
            restore = await repository.create_restore_proposal(
                proposal_id=RESTORE_PROPOSAL_ID,
                snapshot_id=RESTORE_DESIRED_ID,
                user_id=facts.first_user_id,
                source_snapshot_id=CALENDAR_BEFORE_ID,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="calendar:restore:one",
                creation_payload_hash="5" * 64,
                calendar_id="primary",
                target_event_id="synthetic-event",
                base_etag="synthetic-etag-two",
                retain_until=FIXED_RETAIN_UNTIL,
            )
            assert restore is not None
            assert restore.proposal_id == RESTORE_PROPOSAL_ID
            assert restore.operation_kind == "restore"
            assert restore.desired_snapshot.content == before
            restore_replay = await repository.create_restore_proposal(
                proposal_id=UUID("00000000-0000-0000-0000-000000000639"),
                snapshot_id=UUID("00000000-0000-0000-0000-000000000640"),
                user_id=facts.first_user_id,
                source_snapshot_id=CALENDAR_BEFORE_ID,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="calendar:restore:one",
                creation_payload_hash="5" * 64,
                calendar_id="primary",
                target_event_id="synthetic-event",
                base_etag="synthetic-etag-two",
                retain_until=FIXED_RETAIN_UNTIL,
            )
            assert restore_replay is not None
            assert restore_replay.proposal_id == RESTORE_PROPOSAL_ID
            with pytest.raises(StateConflictError) as restore_mismatch:
                await repository.create_restore_proposal(
                    proposal_id=UUID("00000000-0000-0000-0000-000000000641"),
                    snapshot_id=UUID("00000000-0000-0000-0000-000000000642"),
                    user_id=facts.first_user_id,
                    source_snapshot_id=CALENDAR_BEFORE_ID,
                    connection_id=facts.first_connection_id,
                    creation_idempotency_key="calendar:restore:one",
                    creation_payload_hash="8" * 64,
                    calendar_id="primary",
                    target_event_id="synthetic-event",
                    base_etag="synthetic-etag-two",
                    retain_until=FIXED_RETAIN_UNTIL,
                )
            assert restore_mismatch.value.error_code == "idempotency_key_payload_mismatch"
            source_after_restore = await repository.load_snapshot(
                user_id=facts.first_user_id,
                snapshot_id=CALENDAR_BEFORE_ID,
            )
            assert source_after_restore == saved_before

        async with session_factory() as session:
            proposal_count = await session.scalar(
                select(func.count()).select_from(CalendarChangeProposalModel)
            )
            desired_one_row = await session.get(
                CalendarChangeSnapshotModel,
                CALENDAR_DESIRED_ONE_ID,
            )
            snapshot_rows = tuple(
                (
                    await session.execute(
                        text(
                            "SELECT content_ciphertext, "
                            "to_jsonb(calendar_change_snapshots)::text "
                            "FROM calendar_change_snapshots"
                        )
                    )
                ).all()
            )
        assert proposal_count == 2
        assert desired_one_row is not None
        assert desired_one_row.content_ciphertext is not None
        assert desired_one_row.content_nonce is not None
        assert desired_one_row.content_key_version is not None
        canonical_bytes = AeadCipher(b"k" * 32).decrypt(
            EncryptedValue(
                desired_one_row.content_ciphertext,
                desired_one_row.content_nonce,
                desired_one_row.content_key_version,
            ),
            action_payload_aad(
                user_id=facts.first_user_id,
                record_id=CALENDAR_DESIRED_ONE_ID,
                content_kind=f"{CALENDAR_SNAPSHOT_CONTENT_KIND}:desired",
                action=CALENDAR_SNAPSHOT_ACTION,
                schema_version=CALENDAR_SNAPSHOT_SCHEMA_VERSION,
            ),
        )
        assert hashlib.sha256(canonical_bytes).hexdigest() == desired_one_row.canonical_hash
        for ciphertext, serialized_row in snapshot_rows:
            assert isinstance(ciphertext, bytes)
            assert isinstance(serialized_row, str)
            assert CALENDAR_CONTENT_MARKER.encode("utf-8") not in ciphertext
            assert CALENDAR_CONTENT_MARKER not in serialized_row
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_content_kind",
    ("surrogate", "exploding_mapping", "stateful_mapping"),
)
async def test_calendar_create_preparation_failure_commits_no_partial_rows(
    database_url: str,
    invalid_content_kind: str,
) -> None:
    """非标准或状态化内容被捕获后只给固定错误且不留下提案头。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    custom_mapping: _ExplodingMapping | _StatefulMapping | None = None
    if invalid_content_kind == "surrogate":
        desired_state: Mapping[str, object] = {
            "nested": {"description": f"{CALENDAR_CONTENT_MARKER}\ud800"}
        }
    elif invalid_content_kind == "exploding_mapping":
        custom_mapping = _ExplodingMapping(CALENDAR_CONTENT_MARKER)
        desired_state = {"nested": custom_mapping}
    else:
        custom_mapping = _StatefulMapping(CALENDAR_CONTENT_MARKER)
        desired_state = {"nested": custom_mapping}
    try:
        facts = await _seed_users_and_connections(session_factory)
        caught_error: BaseException | None = None
        async with session_factory.begin() as session:
            repository = SqlAlchemyCalendarProposalRepository(session, cipher)
            try:
                await repository.create(
                    proposal_id=CALENDAR_PROPOSAL_ID,
                    snapshot_id=CALENDAR_DESIRED_ONE_ID,
                    user_id=facts.first_user_id,
                    connection_id=facts.first_connection_id,
                    creation_idempotency_key="calendar:create:preparation-failure",
                    creation_payload_hash="3" * 64,
                    calendar_id="primary",
                    operation_kind="create",
                    target_event_id=None,
                    base_etag=None,
                    retain_until=FIXED_RETAIN_UNTIL,
                    desired_state=desired_state,
                )
            except (RuntimeError, TypeError, ValueError, StateConflictError) as error:
                caught_error = error

        async with session_factory() as session:
            proposal_count = await session.scalar(
                select(func.count())
                .select_from(CalendarChangeProposalModel)
                .where(CalendarChangeProposalModel.id == CALENDAR_PROPOSAL_ID)
            )
            snapshot_count = await session.scalar(
                select(func.count())
                .select_from(CalendarChangeSnapshotModel)
                .where(CalendarChangeSnapshotModel.proposal_id == CALENDAR_PROPOSAL_ID)
            )
        assert proposal_count == 0
        assert snapshot_count == 0
        assert caught_error is not None
        _assert_action_payload_format_error_is_content_free(
            caught_error,
            sensitive_marker=CALENDAR_CONTENT_MARKER,
        )
        if custom_mapping is not None:
            assert custom_mapping.items_calls == 0
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_calendar_next_version_preparation_failure_keeps_parent_and_snapshots_unchanged(
    database_url: str,
) -> None:
    """嵌套 surrogate 失败被提交方捕获后不得推进提案 CAS。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    sensitive_description = f"{CALENDAR_CONTENT_MARKER}\ud800"
    try:
        facts = await _seed_users_and_connections(session_factory)
        async with session_factory.begin() as session:
            repository = SqlAlchemyCalendarProposalRepository(session, cipher)
            await repository.create(
                proposal_id=CALENDAR_PROPOSAL_ID,
                snapshot_id=CALENDAR_DESIRED_ONE_ID,
                user_id=facts.first_user_id,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="calendar:next-preparation-source",
                creation_payload_hash="3" * 64,
                calendar_id="primary",
                operation_kind="update",
                target_event_id="synthetic-event",
                base_etag="synthetic-etag-one",
                retain_until=FIXED_RETAIN_UNTIL,
                desired_state={"description": CALENDAR_CONTENT_MARKER},
            )

        caught_error: TypeError | ValueError | None = None
        async with session_factory.begin() as session:
            repository = SqlAlchemyCalendarProposalRepository(session, cipher)
            try:
                await repository.save_next_version(
                    snapshot_id=CALENDAR_DESIRED_TWO_ID,
                    user_id=facts.first_user_id,
                    proposal_id=CALENDAR_PROPOSAL_ID,
                    expected_version=1,
                    desired_state={"nested": {"description": sensitive_description}},
                    retain_until=FIXED_NEXT_RETAIN_UNTIL,
                )
            except (TypeError, ValueError) as error:
                caught_error = error
        assert caught_error is not None
        _assert_action_payload_format_error_is_content_free(
            caught_error,
            sensitive_marker=CALENDAR_CONTENT_MARKER,
        )

        async with session_factory() as session:
            proposal = await session.get(CalendarChangeProposalModel, CALENDAR_PROPOSAL_ID)
            snapshot_count = await session.scalar(
                select(func.count())
                .select_from(CalendarChangeSnapshotModel)
                .where(CalendarChangeSnapshotModel.proposal_id == CALENDAR_PROPOSAL_ID)
            )
        assert proposal is not None
        assert proposal.current_version == 1
        assert proposal.retain_until == FIXED_RETAIN_UNTIL
        assert snapshot_count == 1
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replay_payload_hash", "expects_existing"),
    (
        ("5" * 64, True),
        ("8" * 64, False),
    ),
)
async def test_restore_replay_resolves_creation_key_before_loading_redacted_source(
    database_url: str,
    replay_payload_hash: str,
    expects_existing: bool,
) -> None:
    """restore 重放先解析创建键，不依赖已成功复制的历史 source 内容。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    desired = {"description": CALENDAR_CONTENT_MARKER}
    before = {"description": f"{CALENDAR_CONTENT_MARKER}-before"}
    try:
        facts = await _seed_users_and_connections(session_factory)
        async with session_factory.begin() as session:
            repository = SqlAlchemyCalendarProposalRepository(session, cipher)
            await repository.create(
                proposal_id=CALENDAR_PROPOSAL_ID,
                snapshot_id=CALENDAR_DESIRED_ONE_ID,
                user_id=facts.first_user_id,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="calendar:restore-source:one",
                creation_payload_hash="3" * 64,
                calendar_id="primary",
                operation_kind="update",
                target_event_id="synthetic-event",
                base_etag="synthetic-etag-one",
                retain_until=FIXED_RETAIN_UNTIL,
                desired_state=desired,
            )
            saved_before = await repository.save_snapshot(
                snapshot_id=CALENDAR_BEFORE_ID,
                user_id=facts.first_user_id,
                proposal_id=CALENDAR_PROPOSAL_ID,
                version=1,
                snapshot_kind="before",
                content=before,
                retain_until=FIXED_RETAIN_UNTIL,
            )
            assert saved_before is not None
            created = await repository.create_restore_proposal(
                proposal_id=RESTORE_PROPOSAL_ID,
                snapshot_id=RESTORE_DESIRED_ID,
                user_id=facts.first_user_id,
                source_snapshot_id=CALENDAR_BEFORE_ID,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="calendar:restore:redacted-source",
                creation_payload_hash="5" * 64,
                calendar_id="primary",
                target_event_id="synthetic-event",
                base_etag="synthetic-etag-two",
                retain_until=FIXED_RETAIN_UNTIL,
            )
            assert created is not None
            await session.execute(
                update(CalendarChangeSnapshotModel)
                .where(CalendarChangeSnapshotModel.id == CALENDAR_BEFORE_ID)
                .values(
                    content_ciphertext=None,
                    content_nonce=None,
                    content_key_version=None,
                )
            )

            if expects_existing:
                replay = await repository.create_restore_proposal(
                    proposal_id=UUID("00000000-0000-0000-0000-000000000643"),
                    snapshot_id=UUID("00000000-0000-0000-0000-000000000644"),
                    user_id=facts.first_user_id,
                    source_snapshot_id=CALENDAR_BEFORE_ID,
                    connection_id=facts.first_connection_id,
                    creation_idempotency_key="calendar:restore:redacted-source",
                    creation_payload_hash=replay_payload_hash,
                    calendar_id="primary",
                    target_event_id="synthetic-event",
                    base_etag="synthetic-etag-two",
                    retain_until=FIXED_RETAIN_UNTIL,
                )
                assert replay is not None
                assert replay.proposal_id == RESTORE_PROPOSAL_ID
                assert replay.desired_snapshot.snapshot_id == RESTORE_DESIRED_ID
            else:
                with pytest.raises(StateConflictError) as mismatch:
                    await repository.create_restore_proposal(
                        proposal_id=UUID("00000000-0000-0000-0000-000000000645"),
                        snapshot_id=UUID("00000000-0000-0000-0000-000000000646"),
                        user_id=facts.first_user_id,
                        source_snapshot_id=CALENDAR_BEFORE_ID,
                        connection_id=facts.first_connection_id,
                        creation_idempotency_key="calendar:restore:redacted-source",
                        creation_payload_hash=replay_payload_hash,
                        calendar_id="primary",
                        target_event_id="synthetic-event",
                        base_etag="synthetic-etag-two",
                        retain_until=FIXED_RETAIN_UNTIL,
                    )
                assert mismatch.value.error_code == "idempotency_key_payload_mismatch"
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_calendar_mark_stale_obeys_state_machine_and_user_scope(database_url: str) -> None:
    """仅当前用户处于 executing 的提案可进入 stale，终态/跨用户不能被绕过。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    try:
        facts = await _seed_users_and_connections(session_factory)
        async with session_factory.begin() as session:
            repository = SqlAlchemyCalendarProposalRepository(session, cipher)
            await repository.create(
                proposal_id=CALENDAR_PROPOSAL_ID,
                snapshot_id=CALENDAR_DESIRED_ONE_ID,
                user_id=facts.first_user_id,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="calendar:stale:one",
                creation_payload_hash="6" * 64,
                calendar_id="primary",
                operation_kind="update",
                target_event_id="synthetic-event",
                base_etag="synthetic-etag-one",
                retain_until=FIXED_RETAIN_UNTIL,
                desired_state={"description": CALENDAR_CONTENT_MARKER},
            )
            await session.execute(
                update(CalendarChangeProposalModel)
                .where(CalendarChangeProposalModel.id == CALENDAR_PROPOSAL_ID)
                .values(status=CalendarProposalStatus.EXECUTING.value)
            )
            assert (
                await repository.mark_stale(
                    user_id=facts.second_user_id,
                    proposal_id=CALENDAR_PROPOSAL_ID,
                )
                is None
            )
            stale = await repository.mark_stale(
                user_id=facts.first_user_id,
                proposal_id=CALENDAR_PROPOSAL_ID,
            )
            assert stale is not None
            assert stale.status is CalendarProposalStatus.STALE
            with pytest.raises(StateConflictError) as terminal:
                await repository.mark_stale(
                    user_id=facts.first_user_id,
                    proposal_id=CALENDAR_PROPOSAL_ID,
                )
            assert terminal.value.error_code == "invalid_action_transition"
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_calendar_snapshot_retention_cleanup_fails_closed(database_url: str) -> None:
    """AEAD 三元组被保留任务清除后不得返回伪造的空日程状态。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    try:
        facts = await _seed_users_and_connections(session_factory)
        async with session_factory.begin() as session:
            repository = SqlAlchemyCalendarProposalRepository(session, cipher)
            await repository.create(
                proposal_id=CALENDAR_PROPOSAL_ID,
                snapshot_id=CALENDAR_DESIRED_ONE_ID,
                user_id=facts.first_user_id,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="calendar:retention:one",
                creation_payload_hash="7" * 64,
                calendar_id="primary",
                operation_kind="create",
                target_event_id=None,
                base_etag=None,
                retain_until=FIXED_RETAIN_UNTIL,
                desired_state={"description": CALENDAR_CONTENT_MARKER},
            )
            await session.execute(
                update(CalendarChangeSnapshotModel)
                .where(CalendarChangeSnapshotModel.id == CALENDAR_DESIRED_ONE_ID)
                .values(
                    content_ciphertext=None,
                    content_nonce=None,
                    content_key_version=None,
                )
            )
            with pytest.raises(StateConflictError) as cleared:
                await repository.load_snapshot(
                    user_id=facts.first_user_id,
                    snapshot_id=CALENDAR_DESIRED_ONE_ID,
                )
            assert cleared.value.error_code == "calendar_snapshot_unavailable"
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_calendar_snapshot_kind_is_part_of_authenticated_content_kind(
    database_url: str,
) -> None:
    """同一 snapshot 行若从 desired 被改称 before，也必须因 AAD 不匹配拒绝。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    try:
        facts = await _seed_users_and_connections(session_factory)
        async with session_factory.begin() as session:
            repository = SqlAlchemyCalendarProposalRepository(session, cipher)
            await repository.create(
                proposal_id=CALENDAR_PROPOSAL_ID,
                snapshot_id=CALENDAR_DESIRED_ONE_ID,
                user_id=facts.first_user_id,
                connection_id=facts.first_connection_id,
                creation_idempotency_key="calendar:kind-aad:one",
                creation_payload_hash="9" * 64,
                calendar_id="primary",
                operation_kind="create",
                target_event_id=None,
                base_etag=None,
                retain_until=FIXED_RETAIN_UNTIL,
                desired_state={"description": CALENDAR_CONTENT_MARKER},
            )
            await session.execute(
                update(CalendarChangeSnapshotModel)
                .where(CalendarChangeSnapshotModel.id == CALENDAR_DESIRED_ONE_ID)
                .values(snapshot_kind="before")
            )
            with pytest.raises(InvalidTag):
                await repository.load_snapshot(
                    user_id=facts.first_user_id,
                    snapshot_id=CALENDAR_DESIRED_ONE_ID,
                )
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_trusted_action_repository_encrypts_marker_and_authenticates_every_dimension(
    database_url: str,
) -> None:
    """M2 命令只留 marker，并绑定用户、审批、内容类别、动作与 Schema。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    try:
        facts = await _seed_users_and_connections(session_factory)
        approvals = await _seed_trusted_approvals(session_factory, facts)
        command = _mail_command(facts)
        async with session_factory.begin() as session:
            repository = SqlAlchemyTrustedActionRepository(session, cipher)
            saved = await repository.save_command(
                user_id=facts.first_user_id,
                approval_id=approvals.approval_id,
                command_payload=command,
            )
            assert saved is not None
            assert saved == command
            assert (
                await repository.save_command(
                    user_id=facts.first_user_id,
                    approval_id=approvals.approval_id,
                    command_payload=command,
                )
                == command
            )
            with pytest.raises(StateConflictError) as frozen_command:
                await repository.save_command(
                    user_id=facts.first_user_id,
                    approval_id=approvals.approval_id,
                    command_payload={
                        **command,
                        "body_text": "different synthetic trusted command body",
                    },
                )
            assert frozen_command.value.error_code == "trusted_action_unavailable"
            assert (
                await repository.load_command(
                    user_id=facts.second_user_id,
                    approval_id=approvals.approval_id,
                )
                is None
            )
            assert (
                await repository.load_command(
                    user_id=facts.first_user_id,
                    approval_id=approvals.approval_id,
                )
                == command
            )

        async with session_factory() as session:
            approval = await session.get(ApprovalRequestModel, approvals.approval_id)
            assert approval is not None
            assert approval.payload == {
                "storage": "encrypted",
                "schema_version": "mail_send.v1",
            }
            assert approval.payload_hash == trusted_command_hash(command)
            assert approval.payload_ciphertext is not None
            assert approval.payload_nonce is not None
            assert approval.payload_key_version is not None
            assert TRUSTED_COMMAND_BODY.encode("utf-8") not in approval.payload_ciphertext
            encrypted = EncryptedValue(
                approval.payload_ciphertext,
                approval.payload_nonce,
                approval.payload_key_version,
            )
            serialized_marker = await session.scalar(
                text("SELECT payload::text FROM approval_requests WHERE id = :approval_id"),
                {"approval_id": approvals.approval_id},
            )
            assert isinstance(serialized_marker, str)
            assert TRUSTED_COMMAND_BODY not in serialized_marker

        alternate_contexts = (
            (
                facts.second_user_id,
                approvals.approval_id,
                "approval_command",
                "mail.send",
                "mail_send.v1",
            ),
            (
                facts.first_user_id,
                approvals.copy_approval_id,
                "approval_command",
                "mail.send",
                "mail_send.v1",
            ),
            (
                facts.first_user_id,
                approvals.approval_id,
                "calendar_snapshot",
                "mail.send",
                "mail_send.v1",
            ),
            (
                facts.first_user_id,
                approvals.approval_id,
                "approval_command",
                "calendar.create",
                "mail_send.v1",
            ),
            (
                facts.first_user_id,
                approvals.approval_id,
                "approval_command",
                "mail.send",
                "calendar_create.v1",
            ),
        )
        for user_id, record_id, content_kind, action, schema_version in alternate_contexts:
            with pytest.raises(InvalidTag):
                cipher.decrypt_json(
                    encrypted,
                    user_id=user_id,
                    record_id=record_id,
                    content_kind=content_kind,
                    action=action,
                    schema_version=schema_version,
                )

        async with session_factory.begin() as session:
            source = await session.get(ApprovalRequestModel, approvals.approval_id)
            target = await session.get(ApprovalRequestModel, approvals.copy_approval_id)
            assert source is not None
            assert target is not None
            target.payload = dict(source.payload)
            target.payload_ciphertext = source.payload_ciphertext
            target.payload_nonce = source.payload_nonce
            target.payload_key_version = source.payload_key_version
            target.payload_hash = source.payload_hash

        async with session_factory.begin() as session:
            repository = SqlAlchemyTrustedActionRepository(session, cipher)
            with pytest.raises(InvalidTag):
                await repository.load_command(
                    user_id=facts.first_user_id,
                    approval_id=approvals.copy_approval_id,
                )
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement_body",
    (
        TRUSTED_COMMAND_BODY,
        "different synthetic trusted command body after retention cleanup",
    ),
)
async def test_trusted_action_redacted_frozen_command_cannot_be_rewritten(
    database_url: str,
    replacement_body: str,
) -> None:
    """冻结 marker 在内容清理后仍阻止同命令重加密或异命令覆盖。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    try:
        facts = await _seed_users_and_connections(session_factory)
        approvals = await _seed_trusted_approvals(session_factory, facts)
        command = _mail_command(facts)
        async with session_factory.begin() as session:
            repository = SqlAlchemyTrustedActionRepository(session, cipher)
            await repository.save_command(
                user_id=facts.first_user_id,
                approval_id=approvals.approval_id,
                command_payload=command,
            )
            await session.execute(
                update(ApprovalRequestModel)
                .where(ApprovalRequestModel.id == approvals.approval_id)
                .values(
                    payload_ciphertext=None,
                    payload_nonce=None,
                    payload_key_version=None,
                )
            )

            with pytest.raises(StateConflictError) as redacted:
                await repository.save_command(
                    user_id=facts.first_user_id,
                    approval_id=approvals.approval_id,
                    command_payload={**command, "body_text": replacement_body},
                )

            assert redacted.value.error_code == "trusted_action_unavailable"

        async with session_factory() as session:
            approval = await session.get(ApprovalRequestModel, approvals.approval_id)
            assert approval is not None
            assert approval.payload == {
                "storage": "encrypted",
                "schema_version": "mail_send.v1",
            }
            assert approval.payload_hash == trusted_command_hash(command)
            assert (
                approval.payload_ciphertext,
                approval.payload_nonce,
                approval.payload_key_version,
            ) == (None, None, None)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_skeleton",
    (
        "approved_status",
        "decision_fields",
        "nonempty_payload",
        "payload_hash_mismatch",
    ),
)
async def test_trusted_action_first_freeze_rejects_non_exact_pending_skeleton(
    database_url: str,
    invalid_skeleton: str,
) -> None:
    """首次冻结只接受 pending、未决定、空 payload 且哈希已精确绑定的骨架。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    try:
        facts = await _seed_users_and_connections(session_factory)
        approvals = await _seed_trusted_approvals(session_factory, facts)
        command = _mail_command(facts)
        expected_state: tuple[object, ...] | None = None
        async with session_factory.begin() as session:
            approval = await session.get(ApprovalRequestModel, approvals.approval_id)
            assert approval is not None
            if invalid_skeleton == "approved_status":
                approval.status = ApprovalStatus.APPROVED.value
            elif invalid_skeleton == "decision_fields":
                approval.decided_at = FIXED_RETAIN_UNTIL
                approval.decided_by_user_id = facts.first_user_id
                approval.approved_execution_deadline_at = FIXED_NEXT_RETAIN_UNTIL
            elif invalid_skeleton == "nonempty_payload":
                approval.payload = {"unexpected": "synthetic"}
            else:
                approval.payload_hash = "0" * 64
            await session.flush()
            expected_state = _approval_freeze_state(approval)

            repository = SqlAlchemyTrustedActionRepository(session, cipher)
            with pytest.raises(StateConflictError) as invalid:
                await repository.save_command(
                    user_id=facts.first_user_id,
                    approval_id=approvals.approval_id,
                    command_payload=command,
                )

            assert invalid.value.error_code == "trusted_action_unavailable"
            assert _approval_freeze_state(approval) == expected_state

        assert expected_state is not None
        async with session_factory() as session:
            persisted = await session.get(ApprovalRequestModel, approvals.approval_id)
            assert persisted is not None
            assert _approval_freeze_state(persisted) == expected_state
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_trusted_action_read_fails_closed_for_hash_marker_and_legacy_spoofing(
    database_url: str,
) -> None:
    """哈希、marker、密文缺列或非 fake.write legacy 伪装均不得返回命令。"""
    session_factory = build_session_factory(database_url)
    cipher = ActionPayloadCipher.from_key(b"k" * 32)
    try:
        facts = await _seed_users_and_connections(session_factory)
        approvals = await _seed_trusted_approvals(session_factory, facts)
        command = _mail_command(facts)
        async with session_factory.begin() as session:
            repository = SqlAlchemyTrustedActionRepository(session, cipher)
            await repository.save_command(
                user_id=facts.first_user_id,
                approval_id=approvals.approval_id,
                command_payload=command,
            )
            assert await repository.load_command(
                user_id=facts.first_user_id,
                approval_id=approvals.legacy_approval_id,
            ) == {"synthetic": True}
            with pytest.raises(StateConflictError) as invalid_legacy:
                await repository.load_command(
                    user_id=facts.first_user_id,
                    approval_id=approvals.invalid_legacy_approval_id,
                )
            assert invalid_legacy.value.error_code == "trusted_action_unavailable"

            m2 = await session.get(ApprovalRequestModel, approvals.approval_id)
            assert m2 is not None
            m2.payload_hash = "0" * 64
            await session.flush()
            with pytest.raises(StateConflictError) as hash_mismatch:
                await repository.load_command(
                    user_id=facts.first_user_id,
                    approval_id=approvals.approval_id,
                )
            assert hash_mismatch.value.error_code == "trusted_action_unavailable"

            m2.payload_hash = trusted_command_hash(command)
            m2.payload = {
                "storage": "encrypted",
                "schema_version": "mail_send.v1",
                "extra": "spoofed",
            }
            await session.flush()
            with pytest.raises(StateConflictError) as marker_spoof:
                await repository.load_command(
                    user_id=facts.first_user_id,
                    approval_id=approvals.approval_id,
                )
            assert marker_spoof.value.error_code == "trusted_action_unavailable"

            m2.payload = {"storage": "encrypted", "schema_version": "mail_send.v1"}
            m2.payload_ciphertext = None
            m2.payload_nonce = None
            m2.payload_key_version = None
            await session.flush()
            with pytest.raises(StateConflictError) as missing_ciphertext:
                await repository.load_command(
                    user_id=facts.first_user_id,
                    approval_id=approvals.approval_id,
                )
            assert missing_ciphertext.value.error_code == "trusted_action_unavailable"
    finally:
        await session_factory.dispose()
