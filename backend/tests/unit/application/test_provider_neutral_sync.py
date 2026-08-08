"""验证邮件与日历同步只依赖供应商中立端口和精确 scope 游标。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest

from ai_employee.application.ports.calendar import (
    CalendarConnectionState,
    CalendarEvent,
    CalendarSyncPage,
)
from ai_employee.application.ports.mail import (
    MailConnectionState,
    MailMessageUpsertResult,
    MailSyncPage,
)
from ai_employee.application.use_cases.sync_calendar import SyncCalendarUseCase
from ai_employee.application.use_cases.sync_mail import SyncMailUseCase
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.errors import InternalInvariantError

USER_ID = UUID("00000000-0000-0000-0000-000000000001")
MICROSOFT_CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000002")
GOOGLE_CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000003")


@dataclass(slots=True)
class FakeMailReader:
    """按测试给定的有限页面模拟任意供应商邮件读取端口。"""

    pages: tuple[MailSyncPage, ...]

    async def list_sync_scopes(self) -> tuple[object, ...]:
        """本用例已有精确 scope，不在该测试重复验证目录发现。"""
        return ()

    async def initial_pages(
        self, scope_key: str, *, since: datetime
    ) -> AsyncIterator[MailSyncPage]:
        """返回受限初始页，并确认用例传入明确 scope 与 UTC 下界。"""
        assert scope_key == "inbox"
        assert since.tzinfo is UTC
        for page in self.pages:
            yield page

    async def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """从已有 Microsoft Delta 游标返回同一组受限页面。"""
        assert scope_key == "inbox"
        assert cursor == "delta-1"
        for page in self.pages:
            yield page


@dataclass(slots=True)
class FakeCalendarReader:
    """为每个 opaque calendar ID 返回独立的增量页面。"""

    pages_by_calendar: dict[str, CalendarSyncPage]

    async def directory_pages(self, cursor: str | None = None) -> AsyncIterator[object]:
        """本测试只验证已经发现的两个日历 scope。"""
        del cursor
        if False:
            yield object()

    async def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """从对应日历的初始页面开始读取。"""
        yield self.pages_by_calendar[calendar_id]

    async def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """确认旧游标属于同一个日历，再返回该 scope 的下一页。"""
        assert cursor == f"{calendar_id}-cursor-1"
        yield self.pages_by_calendar[calendar_id]

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """精确只读接口不属于本同步测试的数据路径。"""
        del calendar_id, provider_event_id
        return None


@dataclass(slots=True)
class FakeReadAdapterRegistry:
    """记录应用层按连接 provider 与精确 scope 请求的读取适配器。"""

    mail_readers: dict[str, FakeMailReader] = field(default_factory=dict)
    calendar_readers: dict[str, FakeCalendarReader] = field(default_factory=dict)
    requested: list[tuple[str, UUID, str]] = field(default_factory=list)

    def mail_reader(self, *, provider: str, connection_id: UUID, scope_key: str) -> FakeMailReader:
        """按固定供应商键返回邮件读取器并记录选择事实。"""
        self.requested.append((provider, connection_id, scope_key))
        return self.mail_readers[provider]

    def calendar_reader(
        self, *, provider: str, connection_id: UUID, scope_key: str
    ) -> FakeCalendarReader:
        """按固定供应商键返回日历读取器并记录选择事实。"""
        self.requested.append((provider, connection_id, scope_key))
        return self.calendar_readers[provider]


@dataclass(slots=True)
class FakeMailStore:
    """用单个 scope 状态模拟事务存储，不复制供应商页面内容。"""

    state: MailConnectionState
    message_write_count: int = 0
    finish_sync_count: int = 0

    async def get_state(
        self, *, user_id: UUID, connection_id: UUID, scope_key: str
    ) -> MailConnectionState | None:
        """返回已验证归属和能力的 Microsoft inbox 状态。"""
        assert (user_id, connection_id, scope_key) == (
            USER_ID,
            MICROSOFT_CONNECTION_ID,
            "inbox",
        )
        return self.state

    async def clear_cursor(self, **kwargs: object) -> None:
        """初始同步不应清除游标。"""
        raise AssertionError(f"unexpected cursor clear: {kwargs!r}")

    async def upsert_message(self, **kwargs: object) -> MailMessageUpsertResult:
        """空页面不应产生消息写入。"""
        self.message_write_count += 1
        raise AssertionError(f"unexpected message write: {kwargs!r}")

    async def finish_sync(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scope_key: str,
        expected_cursor: str | None,
        next_cursor: str,
        **kwargs: object,
    ) -> None:
        """只推进本次 inbox scope 的最终 Delta 游标。"""
        del kwargs
        self.finish_sync_count += 1
        assert (user_id, connection_id, scope_key, expected_cursor) == (
            USER_ID,
            MICROSOFT_CONNECTION_ID,
            "inbox",
            self.state.cursor,
        )
        self.state = MailConnectionState(
            provider="microsoft", scope_key=scope_key, cursor=next_cursor
        )


@dataclass(slots=True)
class FakeCalendarStore:
    """以字典模拟 `(connection_id, calendar_id)` 隔离的游标 CAS。"""

    states: dict[str, CalendarConnectionState]

    async def get_state(
        self, *, user_id: UUID, connection_id: UUID, scope_key: str
    ) -> CalendarConnectionState | None:
        """按精确日历 ID 返回游标，不提供连接级共享状态。"""
        assert user_id == USER_ID
        assert connection_id == GOOGLE_CONNECTION_ID
        return self.states.get(scope_key)

    async def clear_cursor(self, **kwargs: object) -> None:
        """有效游标场景不应进入回退。"""
        raise AssertionError(f"unexpected cursor clear: {kwargs!r}")

    async def upsert_event(self, **kwargs: object) -> None:
        """空页面不应产生事件写入。"""
        raise AssertionError(f"unexpected event write: {kwargs!r}")

    async def finish_sync(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scope_key: str,
        expected_cursor: str | None,
        next_cursor: str,
        **kwargs: object,
    ) -> None:
        """以 scope 级 CAS 更新一个日历，保留其他日历状态。"""
        del kwargs
        assert user_id == USER_ID
        assert connection_id == GOOGLE_CONNECTION_ID
        assert self.states[scope_key].cursor == expected_cursor
        self.states[scope_key] = CalendarConnectionState(
            provider="google", scope_key=scope_key, cursor=next_cursor
        )


@asynccontextmanager
async def _store_factory(store: object):
    """把内存 Fake 暴露为与真实短事务一致的异步上下文。"""
    yield store


@pytest.mark.asyncio
async def test_mail_sync_selects_adapter_from_connection_provider() -> None:
    """邮件同步必须按连接 provider 选择 Microsoft reader，而不是硬编码 Google。"""
    registry = FakeReadAdapterRegistry()
    registry.mail_readers["microsoft"] = FakeMailReader(
        pages=(MailSyncPage(messages=(), next_page_token=None, next_cursor="delta-1"),)
    )
    store = FakeMailStore(MailConnectionState(provider="microsoft", scope_key="inbox", cursor=None))

    result = await SyncMailUseCase(lambda: _store_factory(store), registry).execute(
        user_id=USER_ID,
        connection_id=MICROSOFT_CONNECTION_ID,
        scope_key="inbox",
    )

    assert registry.requested == [("microsoft", MICROSOFT_CONNECTION_ID, "inbox")]
    assert result.next_cursor == "delta-1"


@pytest.mark.asyncio
async def test_mail_initial_sync_without_final_cursor_fails_before_any_store_mutation() -> None:
    """初始页没有有效游标时必须拒绝成功，且不得写消息或推进空游标。"""
    registry = FakeReadAdapterRegistry()
    registry.mail_readers["microsoft"] = FakeMailReader(
        pages=(MailSyncPage(messages=(), next_page_token=None, next_cursor=None),)
    )
    store = FakeMailStore(MailConnectionState(provider="microsoft", scope_key="inbox", cursor=None))

    with pytest.raises(InternalInvariantError) as captured:
        await SyncMailUseCase(lambda: _store_factory(store), registry).execute(
            user_id=USER_ID,
            connection_id=MICROSOFT_CONNECTION_ID,
            scope_key="inbox",
        )

    assert captured.value.error_code == "mail_final_cursor_missing"
    assert store.state.cursor is None
    assert store.message_write_count == 0
    assert store.finish_sync_count == 0
    assert registry.requested == [("microsoft", MICROSOFT_CONNECTION_ID, "inbox")]


@pytest.mark.asyncio
async def test_mail_empty_increment_without_page_cursor_keeps_existing_cursor() -> None:
    """增量页没有新游标时可保留已验证旧游标，仍按同一 scope 完成幂等同步。"""
    registry = FakeReadAdapterRegistry()
    registry.mail_readers["microsoft"] = FakeMailReader(
        pages=(MailSyncPage(messages=(), next_page_token=None, next_cursor=None),)
    )
    store = FakeMailStore(
        MailConnectionState(provider="microsoft", scope_key="inbox", cursor="delta-1")
    )

    result = await SyncMailUseCase(lambda: _store_factory(store), registry).execute(
        user_id=USER_ID,
        connection_id=MICROSOFT_CONNECTION_ID,
        scope_key="inbox",
    )

    assert result.next_cursor == "delta-1"
    assert not result.used_full_resync
    assert store.state.cursor == "delta-1"
    assert store.message_write_count == 0
    assert store.finish_sync_count == 1


@pytest.mark.asyncio
async def test_calendar_sync_advances_only_the_requested_calendar_cursor() -> None:
    """两个日历共享连接时，推进一个 scope 绝不能覆盖另一个日历游标。"""
    calendar_a = "calendar-a"
    calendar_b = "calendar-b"
    store = FakeCalendarStore(
        states={
            calendar_a: CalendarConnectionState(
                provider="google", scope_key=calendar_a, cursor=f"{calendar_a}-cursor-1"
            ),
            calendar_b: CalendarConnectionState(
                provider="google", scope_key=calendar_b, cursor=f"{calendar_b}-cursor-1"
            ),
        }
    )
    registry = FakeReadAdapterRegistry(
        calendar_readers={
            "google": FakeCalendarReader(
                pages_by_calendar={
                    calendar_a: CalendarSyncPage(
                        events=(),
                        next_page_token=None,
                        next_cursor=f"{calendar_a}-cursor-2",
                    ),
                    calendar_b: CalendarSyncPage(
                        events=(),
                        next_page_token=None,
                        next_cursor=f"{calendar_b}-cursor-2",
                    ),
                }
            )
        }
    )

    result = await SyncCalendarUseCase(lambda: _store_factory(store), registry).execute(
        user_id=USER_ID,
        connection_id=GOOGLE_CONNECTION_ID,
        scope_key=calendar_a,
    )

    assert result.next_cursor == f"{calendar_a}-cursor-2"
    assert store.states[calendar_a].cursor == f"{calendar_a}-cursor-2"
    assert store.states[calendar_b].cursor == f"{calendar_b}-cursor-1"
    assert registry.requested == [("google", GOOGLE_CONNECTION_ID, calendar_a)]


def test_task_runner_routes_new_and_legacy_mail_kinds_to_the_same_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新 ``sync_mail`` 与持久 legacy ``sync_gmail`` 必须解析到同一邮件执行步骤。"""
    from ai_employee.workers import execute_task as execute_task_module

    captured: dict[str, object] = {}
    mail_step = object()

    class CapturingRunner:
        """捕获组合根的步骤解析器，不创建数据库连接或执行外部读取。"""

        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(execute_task_module, "DurableTaskRunner", CapturingRunner)
    monkeypatch.setattr(
        execute_task_module,
        "build_mail_sync_task_step",
        lambda **_kwargs: mail_step,
        raising=False,
    )
    execute_task_module.build_task_runner_for_session(object(), settings=Settings())
    resolver = captured["resolve_steps"]
    assert callable(resolver)

    def leased(kind: str) -> LeasedTask:
        """构造只含任务路由所需字段的合成租约快照。"""
        return LeasedTask(
            task_id=UUID("00000000-0000-0000-0000-000000000010"),
            kind=kind,
            input_payload={"connection_id": str(GOOGLE_CONNECTION_ID)},
            started_at=datetime(2030, 1, 1, tzinfo=UTC),
            user_id=USER_ID,
        )

    assert resolver(leased("sync_mail")) == (mail_step,)
    assert resolver(leased("sync_gmail")) == (mail_step,)
