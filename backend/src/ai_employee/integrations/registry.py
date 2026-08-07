"""提供固定 Google/Microsoft 键的读取适配器注册表与 M1 兼容包装。"""

from collections.abc import AsyncIterator
from datetime import datetime
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from ai_employee.application.ports.calendar import (
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarReader,
    CalendarSyncPage,
)
from ai_employee.application.ports.mail import MailReader, MailScope, MailSyncPage
from ai_employee.domain.errors import InternalInvariantError, PermanentProviderError


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

    def __init__(self, reader: LegacyGoogleCalendarReader) -> None:
        self._reader = reader

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """Task 12 前不伪造供应商目录页；调用者必须使用已迁移 primary scope。"""
        del cursor
        if False:
            yield CalendarDirectoryPage((), None, None)

    def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """验证 primary 后委托旧适配器的受限初始窗口。"""
        self._require_primary(calendar_id)
        return self._reader.initial_pages()

    def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """验证 primary 后委托旧适配器的 syncToken 读取。"""
        self._require_primary(calendar_id)
        return self._reader.sync_pages(cursor)

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """Task 12 实现精确 GET 前明确拒绝，不用列表结果伪造当前事件。"""
        del calendar_id, provider_event_id
        raise ProviderAdapterUnavailableError

    @staticmethod
    def _require_primary(calendar_id: str) -> None:
        """拒绝让旧固定 URL 适配器静默读取其他 provider calendar ID。"""
        if calendar_id != "primary":
            raise ProviderAdapterUnavailableError


class ProviderAdapterRegistry:
    """保存固定 Google/Microsoft 键的只读适配器，不提供运行期注册入口。"""

    __slots__ = ("_calendar_readers", "_mail_readers")

    def __init__(
        self,
        *,
        google_mail: LegacyGoogleMailReader | None = None,
        microsoft_mail: MailReader | None = None,
        google_calendar: LegacyGoogleCalendarReader | None = None,
        microsoft_calendar: CalendarReader | None = None,
    ) -> None:
        """以四个显式构造参数冻结注册表；未知键无法动态加入。"""
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


__all__ = [
    "ProviderAdapterRegistry",
    "ProviderAdapterUnavailableError",
    "UnsupportedProviderError",
]
