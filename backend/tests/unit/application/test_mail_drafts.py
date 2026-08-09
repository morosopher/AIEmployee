"""验证本地邮件草稿用例的用户隔离、绑定与版本不变量。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_employee.application.use_cases.mail_drafts import (
    CreateMailDraftInput,
    MailDraftSourceMessage,
    MailDraftUseCase,
    MailRecipientHistoryEntry,
    UpdateMailDraftInput,
)
from ai_employee.domain.actions import MailDraftStatus
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode

USER_ID = uuid4()
CONNECTION_ID = uuid4()
SOURCE_THREAD_ID = "thread-synthetic"
SOURCE_MESSAGE_ID = "message-synthetic"
NOW = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)


class _Connection:
    def __init__(self, *, connection_id=CONNECTION_ID, email="owner@example.test") -> None:
        self.id = connection_id
        self.user_id = USER_ID
        self.account_email = email
        self.status = "connected"
        self.provider = "google"


class _Connections:
    def __init__(self) -> None:
        self.connections = [_Connection()]
        self.capabilities = {ConnectionCapability.MAIL_SEND, ConnectionCapability.MAIL_READ}

    async def get_connection(self, *, user_id, connection_id):
        return next(
            (
                value
                for value in self.connections
                if value.user_id == user_id and value.id == connection_id
            ),
            None,
        )

    async def list_connections(self, *, user_id):
        return tuple(value for value in self.connections if value.user_id == user_id)

    async def get_enabled_capabilities(self, *, user_id, connection_id):
        if any(value.id == connection_id and value.user_id == user_id for value in self.connections):
            return frozenset(self.capabilities)
        return None

    async def get_default_mail_connection(self, *, user_id):
        return self.connections[0] if user_id == USER_ID else None

    async def get_mail_draft_retention_days(self, *, user_id):
        return 30 if user_id == USER_ID else None


class _Sources:
    def __init__(
        self, *, history: tuple[MailRecipientHistoryEntry, ...] = ()
    ) -> None:
        self.history = history

    async def get_draft_source_message(
        self,
        *,
        user_id,
        source_thread_id,
        source_message_id,
        source_connection_id,
    ):
        if user_id != USER_ID:
            return None
        if source_thread_id not in {None, SOURCE_THREAD_ID}:
            return None
        if source_message_id != SOURCE_MESSAGE_ID:
            return None
        if source_connection_id not in {None, CONNECTION_ID}:
            return None
        return MailDraftSourceMessage(
            connection_id=CONNECTION_ID,
            thread_id=SOURCE_THREAD_ID,
            message_id=SOURCE_MESSAGE_ID,
            sender="sender@example.test",
            recipients=("owner@example.test", "sender@example.test", "peer@example.test"),
            subject="Original",
            received_at=NOW,
        )

    async def list_recipient_history(self, *, user_id):
        return self.history if user_id == USER_ID else ()


class _Drafts:
    def __init__(self) -> None:
        self.values = {}
        self.calls = []

    async def create(self, **values):
        self.calls.append(("create", values))
        value = {
            "draft_id": values["draft_id"],
            "connection_id": values["connection_id"],
            "mode": values["mode"],
            "source_thread_id": values["source_thread_id"],
            "source_message_id": values["source_message_id"],
            "current_version": 1,
            "version": 1,
            "status": MailDraftStatus.EDITING,
            "to_recipients": values["to_recipients"],
            "cc_recipients": values["cc_recipients"],
            "bcc_recipients": values["bcc_recipients"],
            "subject": values["subject"],
            "body_text": values["body_text"],
        }
        self.values[value["draft_id"]] = value
        return value

    async def list_current(self, *, user_id, limit, offset):
        if user_id != USER_ID:
            return ()
        return tuple(self.values.values())[offset : offset + limit]

    async def get_current(self, *, user_id, draft_id):
        value = self.values.get(draft_id)
        return value if value and user_id == USER_ID else None

    async def save_next_version(self, **values):
        self.calls.append(("save_next_version", values))
        value = self.values[values["draft_id"]]
        if value["current_version"] != values["expected_version"]:
            raise StateConflictError(
                error_code="draft_version_conflict", message="mail draft version changed"
            )
        value.update(
            current_version=value["current_version"] + 1,
            version=value["current_version"] + 1,
            to_recipients=values["to_recipients"],
            cc_recipients=values["cc_recipients"],
            bcc_recipients=values["bcc_recipients"],
            subject=values["subject"],
            body_text=values["body_text"],
        )
        return value

    async def cancel(self, *, user_id, draft_id):
        value = await self.get_current(user_id=user_id, draft_id=draft_id)
        if value is not None:
            value["status"] = MailDraftStatus.CANCELLED
        return value


@pytest.mark.asyncio
async def test_create_reply_all_binds_source_connection_and_excludes_own_addresses() -> None:
    """全部回复固定源连接，并排除用户自己的所有连接地址。"""
    drafts = _Drafts()
    connections = _Connections()
    use_case = MailDraftUseCase(
        drafts=drafts,
        connections=connections,
        sources=_Sources(),
        clock=lambda: NOW,
    )

    result = await use_case.create_reply_all(
        user_id=USER_ID,
        source_thread_id=SOURCE_THREAD_ID,
        source_message_id=SOURCE_MESSAGE_ID,
        source_connection_id=CONNECTION_ID,
        idempotency_key="reply-all-1",
    )

    assert result.connection_id == CONNECTION_ID
    assert result.mode is MailMode.REPLY_ALL
    assert "owner@example.test" not in result.to_recipients + result.cc_recipients


@pytest.mark.asyncio
async def test_update_reply_cannot_change_subject_or_source_binding() -> None:
    """回复 PATCH 只能修改收件人与正文。"""
    drafts = _Drafts()
    connections = _Connections()
    use_case = MailDraftUseCase(
        drafts=drafts,
        connections=connections,
        sources=_Sources(),
        clock=lambda: NOW,
    )
    created = await use_case.create(
        CreateMailDraftInput(
            user_id=USER_ID,
            mode=MailMode.REPLY,
            idempotency_key="reply-1",
            connection_id=CONNECTION_ID,
            source_thread_id=SOURCE_THREAD_ID,
            source_message_id=SOURCE_MESSAGE_ID,
            to_recipients=("peer@example.test",),
            subject="Re: Original",
        )
    )

    with pytest.raises(StateConflictError) as error:
        await use_case.update(
            UpdateMailDraftInput(
                user_id=USER_ID,
                draft_id=created.draft_id,
                expected_version=1,
                subject="Changed subject",
            )
        )
    assert error.value.error_code == "mail_draft_binding_immutable"


@pytest.mark.asyncio
async def test_recipient_limit_has_stable_error_code() -> None:
    """规范化去重后的 To/CC/BCC 总数不得超过 50。"""
    drafts = _Drafts()
    use_case = MailDraftUseCase(drafts=drafts, connections=_Connections(), clock=lambda: NOW)
    recipients = tuple(f"person-{index}@example.test" for index in range(51))
    with pytest.raises(StateConflictError) as error:
        await use_case.create(
            CreateMailDraftInput(
                user_id=USER_ID,
                mode=MailMode.NEW,
                idempotency_key="too-many",
                connection_id=CONNECTION_ID,
                to_recipients=recipients,
            )
        )
    assert error.value.error_code == "mail_recipient_limit_exceeded"


@pytest.mark.asyncio
async def test_blank_new_draft_uses_configured_default_connection() -> None:
    """未显式选账户的新邮件使用默认发送连接，并允许全部可编辑字段为空。"""
    drafts = _Drafts()
    use_case = MailDraftUseCase(drafts=drafts, connections=_Connections(), clock=lambda: NOW)

    created = await use_case.create_new(user_id=USER_ID, idempotency_key="blank-new")

    assert created.connection_id == CONNECTION_ID
    assert created.mode is MailMode.NEW
    assert created.to_recipients == ()
    assert created.subject == ""
    assert created.body_text == ""


@pytest.mark.asyncio
async def test_reply_subject_is_derived_deterministically_from_source() -> None:
    """回复主题由来源生成一次稳定 ``Re:`` 前缀，不能由调用方自由决定。"""
    use_case = MailDraftUseCase(
        drafts=_Drafts(),
        connections=_Connections(),
        sources=_Sources(),
        clock=lambda: NOW,
    )

    created = await use_case.create_reply(
        user_id=USER_ID,
        source_message_id=SOURCE_MESSAGE_ID,
        source_thread_id=SOURCE_THREAD_ID,
        idempotency_key="reply-subject",
    )

    assert created.subject == "Re: Original"


@pytest.mark.asyncio
async def test_stale_patch_and_approval_lock_have_distinct_conflicts() -> None:
    """陈旧版本与待审批锁分别返回稳定冲突，编辑不能隐式撤回审批。"""
    drafts = _Drafts()
    use_case = MailDraftUseCase(drafts=drafts, connections=_Connections(), clock=lambda: NOW)
    created = await use_case.create_new(
        user_id=USER_ID,
        idempotency_key="conflict-new",
        to=("peer@example.test",),
    )

    with pytest.raises(StateConflictError) as stale:
        await use_case.update(
            UpdateMailDraftInput(
                user_id=USER_ID,
                draft_id=created.draft_id,
                expected_version=2,
                body_text="Synthetic edit",
            )
        )
    assert stale.value.error_code == "draft_version_conflict"

    drafts.values[created.draft_id]["status"] = MailDraftStatus.AWAITING_APPROVAL
    with pytest.raises(StateConflictError) as locked:
        await use_case.update(
            UpdateMailDraftInput(
                user_id=USER_ID,
                draft_id=created.draft_id,
                expected_version=1,
                body_text="Must withdraw first",
            )
        )
    assert locked.value.error_code == "mail_draft_approval_withdrawal_required"


@pytest.mark.asyncio
async def test_cancel_cannot_bypass_pending_approval_withdrawal() -> None:
    """删除本地草稿不能绕过可信任务撤回并遗留仍可批准的冻结命令。"""
    drafts = _Drafts()
    use_case = MailDraftUseCase(drafts=drafts, connections=_Connections(), clock=lambda: NOW)
    created = await use_case.create_new(
        user_id=USER_ID,
        idempotency_key="cancel-approval-lock",
    )
    drafts.values[created.draft_id]["status"] = MailDraftStatus.AWAITING_APPROVAL

    with pytest.raises(StateConflictError) as locked:
        await use_case.cancel(user_id=USER_ID, draft_id=created.draft_id)

    assert locked.value.error_code == "mail_draft_approval_withdrawal_required"
    assert drafts.values[created.draft_id]["status"] is MailDraftStatus.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_invalid_recipient_is_rejected_before_persistence() -> None:
    """任意地址可手工输入，但语法无效时不能进入本地版本。"""
    drafts = _Drafts()
    use_case = MailDraftUseCase(drafts=drafts, connections=_Connections(), clock=lambda: NOW)

    with pytest.raises(ValueError):
        await use_case.create_new(
            user_id=USER_ID,
            idempotency_key="invalid-recipient",
            to=("not-an-address",),
        )

    assert drafts.calls == []


@pytest.mark.asyncio
async def test_send_connection_requires_enabled_mail_read_dependency() -> None:
    """``mail.send`` 不能在其只读核对依赖关闭时被视为可用发送连接。"""
    drafts = _Drafts()
    connections = _Connections()
    connections.capabilities = {ConnectionCapability.MAIL_SEND}
    use_case = MailDraftUseCase(drafts=drafts, connections=connections, clock=lambda: NOW)

    with pytest.raises(StateConflictError) as unavailable:
        await use_case.create_new(
            user_id=USER_ID,
            idempotency_key="missing-mail-read",
        )

    assert unavailable.value.error_code == "connection_capability_disabled"
    assert drafts.calls == []


@pytest.mark.asyncio
async def test_recipient_suggestions_are_local_unique_recent_and_exclude_self() -> None:
    """自动补全只使用本地历史，按最近时间排序并限制二十个非自有地址。"""
    connections = _Connections()
    connections.connections.append(
        _Connection(connection_id=uuid4(), email="second-owner@example.test")
    )
    history = tuple(
        MailRecipientHistoryEntry(
            address=f"person-{index}@example.test",
            last_seen_at=NOW - timedelta(minutes=index),
        )
        for index in range(25)
    ) + (
        MailRecipientHistoryEntry(address="owner@example.test", last_seen_at=NOW),
        MailRecipientHistoryEntry(address="person-0@EXAMPLE.TEST", last_seen_at=NOW),
        MailRecipientHistoryEntry(address="second-owner@example.test", last_seen_at=NOW),
    )
    use_case = MailDraftUseCase(
        drafts=_Drafts(),
        connections=connections,
        sources=_Sources(history=history),
        clock=lambda: NOW,
    )

    suggestions = await use_case.recipient_suggestions(user_id=USER_ID)

    assert len(suggestions) == 20
    assert suggestions[0] == "person-0@example.test"
    assert len(set(suggestions)) == len(suggestions)
    assert "owner@example.test" not in suggestions
    assert "second-owner@example.test" not in suggestions
