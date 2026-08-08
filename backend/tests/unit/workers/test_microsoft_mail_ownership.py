"""验证 Microsoft 邮件同步的 mailbox owner 聚合边界。"""

from dataclasses import dataclass
from uuid import UUID

import pytest

from ai_employee.workers import schedules
from ai_employee.workers.generate_brief import GenerateBriefTaskStep


@dataclass(frozen=True, slots=True)
class _Scope:
    """模拟已通过连接/能力过滤的持久同步 scope。"""

    user_id: UUID
    connection_id: UUID
    provider: str
    resource_kind: str
    scope_key: str


@pytest.mark.asyncio
async def test_microsoft_mail_scheduler_uses_mailbox_owner_once(monkeypatch) -> None:
    """Microsoft 邮件目录由 mailbox owner 周期触发，folder cursor 不应各自重复排队。"""
    user_id = UUID("00000000-0000-0000-0000-000000000011")
    connection_id = UUID("00000000-0000-0000-0000-000000000012")

    class _MixedReader:
        async def enabled_scopes(self) -> tuple[_Scope, ...]:
            """返回 mailbox、真实 folders 与 calendar，复现普通周期读取集合。"""
            return (
                _Scope(user_id, connection_id, "microsoft", "mail", "mailbox"),
                _Scope(user_id, connection_id, "microsoft", "mail", "folder-inbox"),
                _Scope(user_id, connection_id, "microsoft", "mail", "folder-sent"),
                _Scope(user_id, connection_id, "microsoft", "calendar", "calendar-primary"),
            )

    created: list[dict[str, object]] = []

    class _Creator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def execute(self, **kwargs: object) -> None:
            created.append(kwargs)

    monkeypatch.setattr(schedules, "SqlAlchemyEnabledSyncScopeReader", lambda _factory: _MixedReader())
    monkeypatch.setattr(schedules, "CreateTaskUseCase", _Creator)
    monkeypatch.setattr(schedules, "_build_outbox_relay", lambda: object())

    await schedules.dispatch_google_incremental_syncs()

    assert [(item["kind"], item["input_payload"]["scope_key"]) for item in created] == [
        ("sync_mail", "mailbox"),
        ("sync_calendar", "calendar-primary"),
    ]


@pytest.mark.asyncio
async def test_brief_refreshes_one_microsoft_mailbox_for_many_stale_folders() -> None:
    """同一 Microsoft connection 的 mailbox/folder stale 只触发一次目录 owner。"""
    connection_id = UUID("00000000-0000-0000-0000-000000000021")
    user_id = UUID("00000000-0000-0000-0000-000000000022")
    calls: list[tuple[str, UUID, UUID, str]] = []

    async def sync_source(
        resource_kind: str,
        callback_connection_id: UUID,
        callback_user_id: UUID,
        scope_key: str,
    ) -> None:
        """记录刷新调用，证明 owner 聚合不会逐 folder 重复访问 Graph。"""
        calls.append((resource_kind, callback_connection_id, callback_user_id, scope_key))

    step = GenerateBriefTaskStep(object(), sync_source=sync_source)  # type: ignore[arg-type]
    stale = (
        ("mail", connection_id, "mailbox"),
        ("mail", connection_id, "folder-inbox"),
        ("mail", connection_id, "folder-sent"),
    )

    assert await step._refresh_stale_sources(user_id, stale) == []
    assert calls == [("mail", connection_id, user_id, "mailbox")]
