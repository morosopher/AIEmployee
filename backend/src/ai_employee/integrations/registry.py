"""提供固定 Google/Microsoft 键的读取适配器注册表与 M1 兼容包装。"""

from collections.abc import AsyncIterator
from datetime import datetime
from types import MappingProxyType
from typing import Protocol, cast
from uuid import UUID

from ai_employee.application.ports.calendar import (
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarReader,
    CalendarSyncPage,
)
from ai_employee.application.ports.mail import MailReader, MailScope, MailSyncPage
from ai_employee.application.ports.trusted_actions import (
    TrustedActionAdapter,
    TrustedActionPreflight,
)
from ai_employee.domain.errors import (
    InternalInvariantError,
    PermanentProviderError,
    StateConflictError,
)


class UnsupportedProviderError(PermanentProviderError):
    """表示持久连接 provider 不属于 M2 固定 Google/Microsoft 集合。"""

    def __init__(self) -> None:
        """构造不回显未知 provider 原值的稳定安全错误。"""
        super().__init__(
            error_code="unsupported_provider",
            message="Connection provider is unsupported",
        )


class ProviderAdapterUnavailableError(InternalInvariantError):
    """表示受支持 provider 的当前进程尚未组装对应读取适配器。"""

    def __init__(self) -> None:
        """构造不泄露凭据、scope 或供应商响应的稳定组装错误。"""
        super().__init__(
            error_code="provider_read_adapter_unavailable",
            message="Provider read adapter is unavailable",
        )


class LegacyGoogleMailReader(Protocol):
    """描述 Task 7 以前 Gmail 适配器的最小分页形状。"""

    def initial_pages(self) -> AsyncIterator[MailSyncPage]: ...
    def history_pages(self, cursor: str) -> AsyncIterator[MailSyncPage]: ...


class LegacyGoogleCalendarReader(Protocol):
    """描述 Task 7 以前固定 primary Calendar 适配器的最小分页形状。"""

    def initial_pages(self) -> AsyncIterator[CalendarSyncPage]: ...
    def sync_pages(self, cursor: str) -> AsyncIterator[CalendarSyncPage]: ...


class _GoogleCalendarProviderNeutralReader(Protocol):
    """描述 Task 12 后 Google 适配器的目录与分日历端口。"""

    def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]: ...

    def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]: ...

    def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]: ...

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None: ...


class _GoogleMailCompatibilityAdapter:
    """把 M1 单 mailbox Gmail 读取器适配为 provider-neutral 邮件端口。"""

    def __init__(self, reader: LegacyGoogleMailReader) -> None:
        self._reader = reader

    async def list_sync_scopes(self) -> tuple[MailScope, ...]:
        """返回 M1 架构唯一且稳定的 ``mailbox`` scope。"""
        return (MailScope("mailbox", "Mailbox", "mailbox"),)

    def initial_pages(self, scope_key: str, *, since: datetime) -> AsyncIterator[MailSyncPage]:
        """验证唯一 mailbox 后委托旧适配器的七日初始读取。"""
        del since
        self._require_mailbox(scope_key)
        return self._reader.initial_pages()

    def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """验证唯一 mailbox 后把 provider-neutral cursor 委托为 Gmail history。"""
        self._require_mailbox(scope_key)
        return self._reader.history_pages(cursor)

    @staticmethod
    def _require_mailbox(scope_key: str) -> None:
        """拒绝让旧单 mailbox 适配器读取无法表达的其他 folder scope。"""
        if scope_key != "mailbox":
            raise ProviderAdapterUnavailableError


class _GoogleCalendarCompatibilityAdapter:
    """把 M1 固定 primary Calendar 读取器适配为显式日历 ID 端口。"""

    def __init__(
        self, reader: LegacyGoogleCalendarReader | _GoogleCalendarProviderNeutralReader
    ) -> None:
        self._reader = reader

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """委托 Task 12 目录端口；旧 primary reader 继续 fail closed。"""
        if hasattr(self._reader, "directory_pages"):
            async for page in self._reader.directory_pages(cursor):
                yield page
            return
        del cursor
        if False:
            yield CalendarDirectoryPage((), None, None, full_snapshot=True)

    def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """按 provider calendar ID 委托新适配器，旧 reader 仅允许 primary。"""
        if hasattr(self._reader, "directory_pages"):
            reader = cast(_GoogleCalendarProviderNeutralReader, self._reader)
            return reader.initial_pages(calendar_id)
        self._require_primary(calendar_id)
        return self._reader.initial_pages()

    def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """按 provider calendar ID 委托新适配器，旧 reader 仅允许 primary。"""
        if hasattr(self._reader, "directory_pages"):
            reader = cast(_GoogleCalendarProviderNeutralReader, self._reader)
            return reader.sync_pages(calendar_id, cursor)
        self._require_primary(calendar_id)
        return self._reader.sync_pages(cursor)

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """委托新适配器精确 GET；旧 reader 不用列表结果伪造当前事件。"""
        if hasattr(self._reader, "directory_pages"):
            reader = cast(_GoogleCalendarProviderNeutralReader, self._reader)
            return await reader.get_current_event(calendar_id, provider_event_id)
        del calendar_id, provider_event_id
        raise ProviderAdapterUnavailableError

    @staticmethod
    def _require_primary(calendar_id: str) -> None:
        """拒绝让旧固定 URL 适配器静默读取其他 provider calendar ID。"""
        if calendar_id != "primary":
            raise ProviderAdapterUnavailableError


class ProviderAdapterRegistry:
    """保存固定 Google/Microsoft 读适配器与可信动作预检，不提供动态注册。"""

    __slots__ = (
        "_calendar_readers",
        "_mail_readers",
        "_trusted_action_adapters",
        "_trusted_action_preflights",
    )

    def __init__(
        self,
        *,
        google_mail: LegacyGoogleMailReader | None = None,
        microsoft_mail: MailReader | None = None,
        google_calendar: LegacyGoogleCalendarReader
        | _GoogleCalendarProviderNeutralReader
        | None = None,
        microsoft_calendar: CalendarReader | None = None,
        google_mail_preflight: TrustedActionPreflight | None = None,
        google_calendar_preflight: TrustedActionPreflight | None = None,
        microsoft_mail_preflight: TrustedActionPreflight | None = None,
        microsoft_calendar_preflight: TrustedActionPreflight | None = None,
        google_mail_action: TrustedActionAdapter | None = None,
        google_calendar_action: TrustedActionAdapter | None = None,
        microsoft_mail_action: TrustedActionAdapter | None = None,
        microsoft_calendar_action: TrustedActionAdapter | None = None,
    ) -> None:
        """以显式固定 slot 冻结读适配器、预检与四类真实动作 adapter。

        ``*_action`` 同时实现无副作用 preflight；显式 ``*_preflight`` 仅保留 Task 18
        审批提交兼容性，不能因具有相似方法名而自动获得真实写权限。
        """
        self._mail_readers = MappingProxyType(
            {
                "google": (
                    _GoogleMailCompatibilityAdapter(google_mail)
                    if google_mail is not None
                    else None
                ),
                "microsoft": microsoft_mail,
            }
        )
        self._calendar_readers = MappingProxyType(
            {
                "google": (
                    _GoogleCalendarCompatibilityAdapter(google_calendar)
                    if google_calendar is not None
                    else None
                ),
                "microsoft": microsoft_calendar,
            }
        )
        self._trusted_action_preflights = MappingProxyType(
            {
                # 不使用 ``or`` 选择 slot：adapter 可能定义业务意义上的 falsey
                # ``__bool__``，但只要显式注入就必须保持真实动作优先级，不能悄然退回
                # 仅 preflight 的兼容对象。
                ("google", "mail.send"): (
                    google_mail_action
                    if google_mail_action is not None
                    else google_mail_preflight
                ),
                ("google", "calendar.create"): (
                    google_calendar_action
                    if google_calendar_action is not None
                    else google_calendar_preflight
                ),
                ("google", "calendar.update"): (
                    google_calendar_action
                    if google_calendar_action is not None
                    else google_calendar_preflight
                ),
                ("google", "calendar.restore"): (
                    google_calendar_action
                    if google_calendar_action is not None
                    else google_calendar_preflight
                ),
                ("microsoft", "mail.send"): (
                    microsoft_mail_action
                    if microsoft_mail_action is not None
                    else microsoft_mail_preflight
                ),
                ("microsoft", "calendar.create"): (
                    microsoft_calendar_action
                    if microsoft_calendar_action is not None
                    else microsoft_calendar_preflight
                ),
                ("microsoft", "calendar.update"): (
                    microsoft_calendar_action
                    if microsoft_calendar_action is not None
                    else microsoft_calendar_preflight
                ),
                ("microsoft", "calendar.restore"): (
                    microsoft_calendar_action
                    if microsoft_calendar_action is not None
                    else microsoft_calendar_preflight
                ),
            }
        )
        self._trusted_action_adapters = MappingProxyType(
            {
                ("google", "mail.send"): google_mail_action,
                ("google", "calendar.create"): google_calendar_action,
                ("google", "calendar.update"): google_calendar_action,
                ("google", "calendar.restore"): google_calendar_action,
                ("microsoft", "mail.send"): microsoft_mail_action,
                ("microsoft", "calendar.create"): microsoft_calendar_action,
                ("microsoft", "calendar.update"): microsoft_calendar_action,
                ("microsoft", "calendar.restore"): microsoft_calendar_action,
            }
        )

    def mail_reader(self, *, provider: str, connection_id: UUID, scope_key: str) -> MailReader:
        """按固定 provider 键返回连接任务已组装的邮件读取端口。"""
        del connection_id, scope_key
        if provider not in self._mail_readers:
            raise UnsupportedProviderError
        reader = self._mail_readers[provider]
        if reader is None:
            raise ProviderAdapterUnavailableError
        return reader

    def calendar_reader(
        self, *, provider: str, connection_id: UUID, scope_key: str
    ) -> CalendarReader:
        """按固定 provider 键返回连接任务已组装的日历读取端口。"""
        del connection_id, scope_key
        if provider not in self._calendar_readers:
            raise UnsupportedProviderError
        reader = self._calendar_readers[provider]
        if reader is None:
            raise ProviderAdapterUnavailableError
        return reader

    def trusted_action_preflight(
        self,
        *,
        provider: str,
        action: str,
    ) -> TrustedActionPreflight:
        """按精确 provider/action 返回启动时固定的无副作用审批预检。

        未知 provider、超出四种 M2 动作或当前进程未组装适配器都使用同一内容无关
        错误，避免调用方把缺失动作误判为可批准或从错误中探测能力配置。
        """
        preflight = self._trusted_action_preflights.get((provider, action))
        if preflight is None or preflight.provider != provider:
            raise StateConflictError(
                error_code="provider_action_unavailable",
                message="provider action is unavailable",
            )
        return preflight

    def trusted_action_adapter(
        self,
        *,
        provider: str,
        action: str,
    ) -> TrustedActionAdapter:
        """返回固定真实动作 adapter，未知、动态或 provider 错配均 fail closed。"""
        adapter = self._trusted_action_adapters.get((provider, action))
        if adapter is None or adapter.provider != provider:
            raise StateConflictError(
                error_code="provider_action_unavailable",
                message="provider action is unavailable",
            )
        return adapter


__all__ = [
    "ProviderAdapterRegistry",
    "ProviderAdapterUnavailableError",
    "UnsupportedProviderError",
]
