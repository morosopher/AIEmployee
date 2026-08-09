"""验证本地邮件草稿用例的用户隔离、绑定与版本不变量。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import get_type_hints
from uuid import UUID, uuid4

import pytest

from ai_employee.application.use_cases.mail_drafts import (
    CreateMailDraftInput,
    MailDraftCapabilitySnapshot,
    MailDraftConnectionReader,
    MailDraftConnectionSnapshot,
    MailDraftRepository,
    MailDraftSnapshot,
    MailDraftSourceMessage,
    MailDraftStateSnapshot,
    MailDraftUseCase,
    MailRecipientHistoryEntry,
    UpdateMailDraftInput,
)
from ai_employee.domain.actions import MailDraftStatus
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode

USER_ID = uuid4()
CONNECTION_ID = uuid4()
SOURCE_THREAD_ID = "thread-synthetic"
SOURCE_MESSAGE_ID = "message-synthetic"
NOW = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)


def test_mail_draft_ports_publish_exact_application_snapshot_types() -> None:
    """公共端口必须声明应用层快照，不能以 ``object`` 隐藏结构契约。"""
    from ai_employee.application.use_cases import mail_drafts as mail_drafts_module

    draft_snapshot = getattr(mail_drafts_module, "MailDraftSnapshot", None)
    connection_snapshot = getattr(mail_drafts_module, "MailDraftConnectionSnapshot", None)
    state_snapshot = getattr(mail_drafts_module, "MailDraftStateSnapshot", None)

    assert draft_snapshot is not None
    assert connection_snapshot is not None
    assert state_snapshot is not None
    repository_hints = {
        name: get_type_hints(getattr(MailDraftRepository, name))["return"]
        for name in ("list_current", "get_current", "create", "save_next_version", "cancel")
    }
    assert repository_hints == {
        "list_current": tuple[draft_snapshot, ...],
        "get_current": draft_snapshot | None,
        "create": draft_snapshot,
        "save_next_version": draft_snapshot | None,
        "cancel": state_snapshot | None,
    }
    connection_hints = {
        name: get_type_hints(getattr(MailDraftConnectionReader, name))["return"]
        for name in ("get_default_mail_connection", "get_connection", "list_connections")
    }
    assert connection_hints == {
        "get_default_mail_connection": connection_snapshot | None,
        "get_connection": connection_snapshot | None,
        "list_connections": tuple[connection_snapshot, ...],
    }


class _Connections:
    def __init__(self) -> None:
        self.connections = [
            MailDraftConnectionSnapshot(
                id=CONNECTION_ID,
                user_id=USER_ID,
                provider="google",
                account_email="owner@example.test",
                status="connected",
            )
        ]
        self.capabilities = {ConnectionCapability.MAIL_SEND, ConnectionCapability.MAIL_READ}
        self.capability_overrides: dict[
            ConnectionCapability, tuple[CapabilityStatus, str | None]
        ] = {}

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

    async def get_capability_states(self, *, user_id, connection_id):
        """返回当前连接的类型化状态，供新错误分类边界测试。"""
        if not any(
            value.id == connection_id and value.user_id == user_id
            for value in self.connections
        ):
            return None
        states = []
        for capability in ConnectionCapability:
            override = self.capability_overrides.get(capability)
            if override is not None:
                status, last_error_code = override
            elif capability in self.capabilities:
                status, last_error_code = CapabilityStatus.ENABLED, None
            else:
                continue
            states.append(
                MailDraftCapabilitySnapshot(
                    capability=capability,
                    status=status,
                    last_error_code=last_error_code,
                )
            )
        return tuple(states)

    async def get_default_mail_connection(self, *, user_id):
        return self.connections[0] if user_id == USER_ID else None

    async def get_mail_draft_retention_days(self, *, user_id):
        return 30 if user_id == USER_ID else None


class _Sources:
    def __init__(
        self, *, history: tuple[MailRecipientHistoryEntry, ...] = ()
    ) -> None:
        self.history = history
        self.history_calls = 0

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
        self.history_calls += 1
        return self.history if user_id == USER_ID else ()


class _Drafts:
    def __init__(self) -> None:
        self.values: dict[UUID, MailDraftSnapshot] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def create(self, **values):
        self.calls.append(("create", values))
        value = MailDraftSnapshot(
            draft_id=values["draft_id"],
            connection_id=values["connection_id"],
            mode=values["mode"],
            source_thread_id=values["source_thread_id"],
            source_message_id=values["source_message_id"],
            current_version=1,
            status=MailDraftStatus.EDITING,
            retain_until=values["retain_until"],
            version_id=values["version_id"],
            version=1,
            to_recipients=values["to_recipients"],
            cc_recipients=values["cc_recipients"],
            bcc_recipients=values["bcc_recipients"],
            subject=values["subject"],
            body_text=values["body_text"],
            prompt_version=values["prompt_version"],
            model_name=values["model_name"],
            created_at=NOW,
        )
        self.values[value.draft_id] = value
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
        if value.current_version != values["expected_version"]:
            raise StateConflictError(
                error_code="draft_version_conflict", message="mail draft version changed"
            )
        next_version = value.current_version + 1
        value = replace(
            value,
            current_version=next_version,
            version_id=values["version_id"],
            version=next_version,
            to_recipients=values["to_recipients"],
            cc_recipients=values["cc_recipients"],
            bcc_recipients=values["bcc_recipients"],
            subject=values["subject"],
            body_text=values["body_text"],
            prompt_version=values["prompt_version"],
            model_name=values["model_name"],
            retain_until=values["retain_until"],
            created_at=NOW,
        )
        self.values[value.draft_id] = value
        return value

    async def cancel(self, *, user_id, draft_id):
        value = await self.get_current(user_id=user_id, draft_id=draft_id)
        if value is None:
            return None
        self.values[draft_id] = replace(value, status=MailDraftStatus.CANCELLED)
        return MailDraftStateSnapshot(
            draft_id=draft_id,
            current_version=value.current_version,
            status=MailDraftStatus.CANCELLED,
        )


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

    drafts.values[created.draft_id] = replace(
        drafts.values[created.draft_id],
        status=MailDraftStatus.AWAITING_APPROVAL,
    )
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
    drafts.values[created.draft_id] = replace(
        drafts.values[created.draft_id],
        status=MailDraftStatus.AWAITING_APPROVAL,
    )

    with pytest.raises(StateConflictError) as locked:
        await use_case.cancel(user_id=USER_ID, draft_id=created.draft_id)

    assert locked.value.error_code == "mail_draft_approval_withdrawal_required"
    assert drafts.values[created.draft_id].status is MailDraftStatus.AWAITING_APPROVAL


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
@pytest.mark.parametrize(
    ("status", "last_error_code", "expected_error_code"),
    (
        (CapabilityStatus.DISABLED, None, "connection_capability_disabled"),
        (CapabilityStatus.AUTHORIZING, None, "connection_capability_disabled"),
        (CapabilityStatus.DEGRADED, None, "connection_capability_disabled"),
        (CapabilityStatus.ACTION_REQUIRED, None, "connection_scope_missing"),
        (CapabilityStatus.REVOKED, None, "connection_scope_missing"),
        (
            CapabilityStatus.DEGRADED,
            "connection_scope_missing",
            "connection_scope_missing",
        ),
    ),
)
async def test_send_capability_state_distinguishes_local_disable_from_missing_scope(
    status: CapabilityStatus,
    last_error_code: str | None,
    expected_error_code: str,
) -> None:
    """本地不可用与需重新授权必须返回不同稳定错误，且均不得创建草稿。"""
    drafts = _Drafts()
    connections = _Connections()
    connections.capability_overrides[ConnectionCapability.MAIL_SEND] = (
        status,
        last_error_code,
    )
    use_case = MailDraftUseCase(
        drafts=drafts,
        connections=connections,
        clock=lambda: NOW,
    )

    with pytest.raises(StateConflictError) as captured:
        await use_case.create_new(
            user_id=USER_ID,
            idempotency_key=f"capability-state-{status.value}",
        )

    assert captured.value.error_code == expected_error_code
    assert drafts.calls == []


@pytest.mark.asyncio
async def test_recipient_suggestions_are_local_unique_recent_and_exclude_self() -> None:
    """自动补全只使用本地历史，按最近时间排序并限制二十个非自有地址。"""
    connections = _Connections()
    connections.connections.append(
        MailDraftConnectionSnapshot(
            id=uuid4(),
            user_id=USER_ID,
            provider="microsoft",
            account_email="second-owner@example.test",
            status="connected",
        )
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


@pytest.mark.asyncio
async def test_create_does_not_compute_ui_recipient_suggestions() -> None:
    """创建写路径只返回持久化结果，不在同一事务中追加 UI 自动补全查询。"""
    sources = _Sources(
        history=(MailRecipientHistoryEntry(address="peer@example.test", last_seen_at=NOW),)
    )
    use_case = MailDraftUseCase(
        drafts=_Drafts(),
        connections=_Connections(),
        sources=sources,
        clock=lambda: NOW,
    )

    created = await use_case.create_new(user_id=USER_ID, idempotency_key="no-create-suggest")

    assert created.recipient_suggestions == ()
    assert sources.history_calls == 0


@pytest.mark.asyncio
async def test_update_does_not_compute_ui_recipient_suggestions() -> None:
    """版本 CAS 完成后不得在仍持有草稿锁的事务中读取历史建议。"""
    drafts = _Drafts()
    created = await MailDraftUseCase(
        drafts=drafts,
        connections=_Connections(),
        clock=lambda: NOW,
    ).create_new(user_id=USER_ID, idempotency_key="no-update-suggest")
    sources = _Sources(
        history=(MailRecipientHistoryEntry(address="peer@example.test", last_seen_at=NOW),)
    )
    use_case = MailDraftUseCase(
        drafts=drafts,
        connections=_Connections(),
        sources=sources,
        clock=lambda: NOW,
    )

    updated = await use_case.update(
        UpdateMailDraftInput(
            user_id=USER_ID,
            draft_id=created.draft_id,
            expected_version=1,
            body_text="Synthetic update",
        )
    )

    assert updated.recipient_suggestions == ()
    assert sources.history_calls == 0


@pytest.mark.asyncio
async def test_cancel_does_not_compute_ui_recipient_suggestions() -> None:
    """取消状态写入不附带与状态迁移无关的 UI 历史查询。"""
    drafts = _Drafts()
    created = await MailDraftUseCase(
        drafts=drafts,
        connections=_Connections(),
        clock=lambda: NOW,
    ).create_new(user_id=USER_ID, idempotency_key="no-cancel-suggest")
    sources = _Sources(
        history=(MailRecipientHistoryEntry(address="peer@example.test", last_seen_at=NOW),)
    )
    use_case = MailDraftUseCase(
        drafts=drafts,
        connections=_Connections(),
        sources=sources,
        clock=lambda: NOW,
    )

    cancelled = await use_case.cancel(user_id=USER_ID, draft_id=created.draft_id)

    assert cancelled.recipient_suggestions == ()
    assert sources.history_calls == 0
