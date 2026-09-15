"""以外部 Fake 应用标记和原批准密文重建精确当前事件，绝不把本地旧缓存冒充供应商。

Redis 只保存 operation ID/场景/计数；内容仍来自 PostgreSQL 的原不可变加密命令。
缺失外部标记、保留期已清理或身份/哈希不匹配时返回不可用，不猜测恢复所需状态。
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time
from hmac import compare_digest
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ai_employee.application.commands import parse_trusted_command, trusted_command_hash
from ai_employee.application.ports.calendar import (
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarReader,
    CalendarSyncPage,
)
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.config import Settings
from ai_employee.domain.calendar_actions import CalendarCreateCommand
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailSendCommand
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.infrastructure.testing.scenarios import M2FakeActionAdapter
from ai_employee.infrastructure.testing.trusted_actions import SyntheticTrustedActionRegistry


class SyntheticCalendarRestoreReaderResolver:
    """只为双开关及同用户精确合成连接构造没有 HTTP 客户端的读取器。"""

    def __init__(self, sessions: ManagedAsyncSessionMaker, settings: Settings) -> None:
        """复用同一 sealed Fake registry 检查，不读取真实 OAuth token。"""
        self._sessions, self._settings = sessions, settings
        self._registry = SyntheticTrustedActionRegistry(session_factory=sessions, settings=settings)

    async def resolve(self, *, user_id: UUID, connection_id: UUID, provider: str, timezone: str) -> CalendarReader:
        """在返回端口前复核用户、账户命名空间与能力；供应商调用只能是离线索引读取。"""
        del timezone
        await self._registry.validate_trusted_action_connection(
            user_id=user_id, provider=provider, connection_id=connection_id, action="calendar.update",
        )
        return SyntheticCurrentCalendarReader(
            self._sessions, self._settings, user_id=user_id, connection_id=connection_id, provider=provider,
        )


class SyntheticCurrentCalendarReader:
    """只提供恢复需要的精确 GET；目录/同步入口明确拒绝，避免产生第二套同步事实。"""

    def __init__(self, sessions: ManagedAsyncSessionMaker, settings: Settings, *, user_id: UUID, connection_id: UUID, provider: str) -> None:
        """冻结本次只读的用户连接与 Fake 身份。"""
        self._sessions, self._settings = sessions, settings
        self._user_id, self._connection_id, self._provider = user_id, connection_id, provider
        self._external = M2FakeActionAdapter(redis_url=settings.redis_url, user_id=user_id, provider=provider)

    def directory_pages(self, cursor: str | None = None) -> AsyncIterator[CalendarDirectoryPage]:
        """精确恢复 reader 不可用于目录同步。"""
        raise StateConflictError(error_code="synthetic_exact_read_only", message="Only exact calendar reads are available")

    def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """精确恢复 reader 不生成虚构初始页。"""
        raise StateConflictError(error_code="synthetic_exact_read_only", message="Only exact calendar reads are available")

    def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """精确恢复 reader 不生成虚构增量游标。"""
        raise StateConflictError(error_code="synthetic_exact_read_only", message="Only exact calendar reads are available")

    async def get_current_event(self, calendar_id: str, provider_event_id: str) -> CalendarEvent | None:
        """先证明外部 Fake 已应用，再认证原命令；无证据或已清理时不使用本地事件缓存。"""
        operation = await self._external.current_calendar_operation(
            connection_id=self._connection_id, calendar_id=calendar_id, event_id=provider_event_id,
        )
        if operation is None:
            return None
        async with self._sessions() as session:
            approval = await session.scalar(select(ApprovalRequestModel)
                .join(TaskRunModel, TaskRunModel.id == ApprovalRequestModel.task_id)
                .join(ToolExecutionModel, ToolExecutionModel.task_id == TaskRunModel.id)
                .where(TaskRunModel.user_id == self._user_id, ToolExecutionModel.operation_id == operation,
                       ToolExecutionModel.provider == self._provider, ApprovalRequestModel.status == "approved"))
            if approval is None or approval.schema_version is None or approval.payload_ciphertext is None or approval.payload_nonce is None or approval.payload_key_version is None:
                return None
            if approval.payload != {"storage": "encrypted", "schema_version": approval.schema_version}:
                return None
            payload = ActionPayloadCipher(AeadCipher.from_file(self._settings.app_master_key_file)).decrypt_json(
                EncryptedValue(approval.payload_ciphertext, approval.payload_nonce, approval.payload_key_version),
                user_id=self._user_id, record_id=approval.id, content_kind="approval_command",
                action=approval.action, schema_version=approval.schema_version,
            )
            if not compare_digest(trusted_command_hash(payload), approval.payload_hash):
                return None
        command = parse_trusted_command(payload)
        if isinstance(command, MailSendCommand) or command.operation_id != operation or command.connection_id != self._connection_id or command.calendar_id != calendar_id:
            return None
        actual_event_id = command.client_event_id if isinstance(command, CalendarCreateCommand) else command.provider_event_id
        if actual_event_id != provider_event_id:
            return None
        return CalendarEvent(
            event_id=provider_event_id, calendar_id=calendar_id, title=command.title,
            description=command.description or "", location=command.location or "",
            starts_at=_instant(command.starts_at, command.timezone), ends_at=_instant(command.ends_at, command.timezone),
            all_day=command.all_day, transparency="opaque", status="confirmed", timezone=command.timezone,
            recurring_event_id=None, etag=f'"synthetic-{operation}"', provider_url="https://example.test/event",
            organizer={"email": f"synthetic-{self._connection_id}@example.test"},
            attendees=tuple({"email": address} for address in command.attendees), access_role="owner", can_edit=True,
        )


def _instant(value: date | datetime, timezone: str) -> datetime:
    """全天日期按明确 IANA 时区解释，其余冻结时间保持同一 UTC 瞬间。"""
    return (value if isinstance(value, datetime) else datetime.combine(value, time.min, ZoneInfo(timezone))).astimezone(UTC)
