"""以两段短事务准备日历恢复提案，并把供应商精确 GET 放在事务之外。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.calendar_event_aad import calendar_event_field_aad_v2
from ai_employee.application.ports.calendar import CalendarEvent, CalendarReader
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.use_cases.calendar_proposals import (
    CalendarProposalNotFoundError,
    CalendarProposalTargetSnapshot,
    CalendarProposalUseCase,
    CalendarRestoreSourceSnapshot,
    validate_calendar_creation_idempotency_key,
)
from ai_employee.application.use_cases.task_execution import LeasedTask, utc_instant
from ai_employee.config import Settings
from ai_employee.domain.connections import CapabilityStatus, ConnectionStatus
from ai_employee.domain.errors import PermanentProviderError, StateConflictError
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.sources import CalendarEventModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.repositories.email import (
    SqlAlchemyMailSyncRepositoryFactory,
)
from ai_employee.infrastructure.db.repositories.identity import lock_active_user
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.google.calendar import GoogleCalendarAdapter
from ai_employee.integrations.microsoft.calendar import MicrosoftCalendarAdapter


class CalendarRestoreReaderResolver(Protocol):
    """按已复核连接身份解析供应商中立的日历只读端口。"""

    async def resolve(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        provider: str,
        timezone: str,
    ) -> CalendarReader:
        """返回只绑定该用户连接的精确读取器，不执行供应商请求。"""
        ...


@dataclass(frozen=True, slots=True)
class _RestoreCacheVersion:
    """GET 前的精确缓存身份与版本/可编辑性，用于拒绝网络期间的同步覆盖竞争。"""

    event_id: UUID
    etag: str | None
    status: str
    recurring_event_id: str | None
    can_edit: bool


@dataclass(frozen=True, slots=True)
class _RestoreReadPlan:
    """保存首事务确认、可安全跨事务携带的非敏感精确读取标识。"""

    source: CalendarRestoreSourceSnapshot
    provider: str
    timezone: str
    creation_idempotency_key: str
    cache_version: _RestoreCacheVersion


class _ConfiguredCalendarRestoreReaderResolver:
    """从短事务中的 AEAD 凭据构造 Google/Microsoft 精确读取器。"""

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        credential_cipher: AeadCipher,
    ) -> None:
        """保存会话工厂与凭据解密器；不缓存 token 或 reader。"""
        self._stores = SqlAlchemyMailSyncRepositoryFactory(session_factory)
        self._credential_cipher = credential_cipher

    async def resolve(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        provider: str,
        timezone: str,
    ) -> CalendarReader:
        """在独立短事务读取凭据，随后仅在内存中构造 provider adapter。"""
        async with self._stores() as store:
            credentials = await store.get_credentials(
                user_id=user_id,
                connection_id=connection_id,
            )
        if credentials is None or credentials.provider != provider:
            raise CalendarProposalNotFoundError
        access_token = self._credential_cipher.decrypt(
            credentials.access_token,
            _credential_aad(user_id, connection_id, "access_token"),
        ).decode("utf-8")
        if provider == "google":
            return GoogleCalendarAdapter(
                access_token=access_token,
                user_timezone=timezone,
            )
        if provider == "microsoft":
            return MicrosoftCalendarAdapter(
                access_token=access_token,
                user_timezone=timezone,
            )
        raise PermanentProviderError(
            error_code="unsupported_provider",
            message="Connection provider is unsupported",
        )


class PrepareCalendarRestoreTaskStep:
    """读取供应商当前事件并原子保存新的恢复提案与任务结果 marker。"""

    name = "prepare_calendar_restore"

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        *,
        action_cipher: ActionPayloadCipher,
        reader_resolver: CalendarRestoreReaderResolver,
        source_cipher: AeadCipher,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """注入数据库、两类 AEAD、供应商读取器解析和可替换 UTC 时钟。

        Args:
            session_factory: Worker 生命周期拥有的数据库会话工厂。
            action_cipher: desired/before snapshot 的记录绑定 AEAD。
            reader_resolver: 按连接构造 provider-neutral 精确读取器的端口。
            source_cipher: 本地事件字段加密器；精确 GET 的规范结果和恢复提案在同一
                事务提交，避免提交审批时仍读取旧缓存 ETag。不会推进任何同步游标。
            clock: 计算新提案保留期的带时区时钟。
        """
        self._session_factory = session_factory
        self._action_cipher = action_cipher
        self._reader_resolver = reader_resolver
        self._source_cipher = source_cipher
        self._clock = clock or (lambda: datetime.now(UTC))

    async def execute(self, task: LeasedTask) -> None:
        """执行“短事务读取 → 事务外 GET → 短事务复核并原子写入”。

        任何租约丢失、能力撤销、source 身份变化或供应商事件不可恢复都会在第二事务
        写入前停止。真实供应商调用只读且位于两个事务之间；本步骤不冻结、不审批、
        不创建 ToolExecution，也不调用日历写接口。
        """
        user_id = _required_user_id(task)
        lease_owner = _required_lease_owner(task)
        read_plan = await self._load_read_plan(
            task=task,
            user_id=user_id,
            lease_owner=lease_owner,
        )
        if read_plan is None:
            return
        reader = await self._reader_resolver.resolve(
            user_id=user_id,
            connection_id=read_plan.source.connection_id,
            provider=read_plan.provider,
            timezone=read_plan.timezone,
        )
        current_event = await reader.get_current_event(
            read_plan.source.calendar_id,
            read_plan.source.provider_event_id,
        )

        write_now = utc_instant(self._clock(), field="calendar restore clock")
        async with self._session_factory.begin() as session:
            task_row = await session.scalar(
                select(TaskRunModel)
                .where(
                    TaskRunModel.id == task.task_id,
                    TaskRunModel.user_id == user_id,
                    TaskRunModel.kind == "calendar.restore.prepare",
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                    TaskRunModel.lease_expires_at.is_not(None),
                    TaskRunModel.lease_expires_at > write_now,
                )
                .with_for_update()
            )
            if task_row is None or task_row.result_payload is not None:
                return
            # 必须先Task→user再锁source/event。GET前的active读取不能授权返回后的提交，
            # 用户锁还需连续覆盖提案、v2缓存与marker，防止删除屏障插入三者之间。
            if not await lock_active_user(session, user_id=user_id):
                return
            persisted_snapshot_id, persisted_creation_key = _restore_input(task_row.input_payload)
            if (
                persisted_snapshot_id != read_plan.source.source_snapshot_id
                or persisted_creation_key != read_plan.creation_idempotency_key
            ):
                raise StateConflictError(
                    error_code="calendar_restore_task_input_conflict",
                    message="calendar restore task input changed during provider read",
                )
            proposals = SqlAlchemyCalendarProposalRepository(session, self._action_cipher)
            calendar = SqlAlchemyCalendarSyncRepository(session, self._source_cipher)
            # source/target 必须在供应商 GET 后按新的 write_now 重新锁定并验证；若期间
            # 生命周期、保留密文或本地精确事件绑定变化，绝不提交陈旧读取结果。
            refreshed_source = await proposals.get_eligible_restore_source(
                user_id=user_id,
                source_snapshot_id=persisted_snapshot_id,
                now=write_now,
            )
            if refreshed_source != read_plan.source:
                raise CalendarProposalNotFoundError
            cache_version = await _cache_version(session, user_id=user_id, source=refreshed_source)
            if cache_version != read_plan.cache_version:
                # get_eligible_restore_source 已锁住同一事件；比较后到 upsert/commit 之间
                # 同步不能插入新版本。网络期间已提交的新版本必须保留，不能被旧 GET 覆盖。
                raise StateConflictError(
                    error_code="calendar_event_version_conflict",
                    message="calendar event changed during restore preparation",
                )
            proposal = await CalendarProposalUseCase(
                proposals=proposals,
                calendar=calendar,
                clock=self._clock,
            ).create_restore(
                user_id=user_id,
                source_snapshot_id=persisted_snapshot_id,
                current_event=current_event,
                idempotency_key=persisted_creation_key,
            )
            # 缺失/取消必须先经过原领域校验并保留 calendar_event_deleted；到这里才收窄类型。
            assert current_event is not None
            # create_restore 已验证供应商身份、权限、非重复状态和新 ETag。完整规范事件
            # 通过既有三元 upsert/AAD-v2 保存；只更新这一条事件，绝不伪造 scope 新鲜度。
            description, location = _encrypt_current_fields(
                self._source_cipher,
                user_id=user_id,
                connection_id=refreshed_source.connection_id,
                current=current_event,
            )
            await calendar.upsert_event(
                user_id=user_id,
                connection_id=refreshed_source.connection_id,
                event=current_event,
                encrypted_description=description,
                encrypted_location=location,
            )
            task_row.result_payload = {"calendar_proposal_id": str(proposal.proposal_id)}

    async def _load_read_plan(
        self,
        *,
        task: LeasedTask,
        user_id: UUID,
        lease_owner: str,
    ) -> _RestoreReadPlan | None:
        """首事务复核租约、source 归属和当前写能力，返回精确 GET 标识。"""
        read_now = utc_instant(self._clock(), field="calendar restore clock")
        async with self._session_factory.begin() as session:
            task_row = await session.scalar(
                select(TaskRunModel).where(
                    TaskRunModel.id == task.task_id,
                    TaskRunModel.user_id == user_id,
                    TaskRunModel.kind == "calendar.restore.prepare",
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                    TaskRunModel.lease_expires_at.is_not(None),
                    TaskRunModel.lease_expires_at > read_now,
                )
            )
            if task_row is None or task_row.result_payload is not None:
                return None
            source_snapshot_id, creation_key = _restore_input(task_row.input_payload)
            proposals = SqlAlchemyCalendarProposalRepository(session, self._action_cipher)
            source = await proposals.get_eligible_restore_source(
                user_id=user_id,
                source_snapshot_id=source_snapshot_id,
                now=read_now,
            )
            if source is None:
                raise CalendarProposalNotFoundError
            calendar = SqlAlchemyCalendarSyncRepository(session, self._source_cipher)
            target = await calendar.get_proposal_target(
                user_id=user_id,
                connection_id=source.connection_id,
                calendar_id=source.calendar_id,
            )
            target = _require_writable_target(target)
            cache_version = await _cache_version(session, user_id=user_id, source=source)
            if cache_version is None:
                raise CalendarProposalNotFoundError
            return _RestoreReadPlan(
                source=source,
                provider=target.provider,
                timezone=target.timezone,
                creation_idempotency_key=creation_key,
                cache_version=cache_version,
            )


async def _cache_version(
    session: AsyncSession,
    *,
    user_id: UUID,
    source: CalendarRestoreSourceSnapshot,
) -> _RestoreCacheVersion | None:
    """读取既有来源查询已锁定的精确事件标量，不解密缓存内容或扩大用户作用域。"""
    row = (
        await session.execute(
            select(
                CalendarEventModel.id,
                CalendarEventModel.etag,
                CalendarEventModel.status,
                CalendarEventModel.recurring_event_id,
                CalendarEventModel.can_edit,
            ).where(
                CalendarEventModel.user_id == user_id,
                CalendarEventModel.connection_id == source.connection_id,
                CalendarEventModel.calendar_id == source.calendar_id,
                CalendarEventModel.provider_event_id == source.provider_event_id,
            )
        )
    ).one_or_none()
    return None if row is None else _RestoreCacheVersion(*row)


def _encrypt_current_fields(
    cipher: AeadCipher,
    *,
    user_id: UUID,
    connection_id: UUID,
    current: CalendarEvent,
) -> tuple[EncryptedValue, EncryptedValue]:
    """沿正式同步的共享 AAD-v2 五项身份加密描述与地点；明文只存在于当前步骤内存。"""

    def encrypt(field: Literal["description", "location"], value: str) -> EncryptedValue:
        """同一精确供应商事件的两字段使用不同认证身份，不可交换密文。"""
        return cipher.encrypt(
            value.encode("utf-8"),
            calendar_event_field_aad_v2(
                user_id=str(user_id),
                connection_id=str(connection_id),
                calendar_id=current.calendar_id,
                provider_event_id=current.event_id,
                field=field,
            ),
        )

    return encrypt("description", current.description), encrypt("location", current.location)


def build_prepare_calendar_restore_task_step(
    *,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings,
) -> PrepareCalendarRestoreTaskStep:
    """从受控 Secret 配置组合恢复准备步骤，不把 token 放入任务载荷。"""
    source_cipher = AeadCipher.from_file(settings.app_master_key_file)
    resolver: CalendarRestoreReaderResolver
    if settings.app_env == "test" and settings.app_test_mode:
        from ai_employee.infrastructure.testing.calendar_restore import (
            SyntheticCalendarRestoreReaderResolver,
        )

        resolver = SyntheticCalendarRestoreReaderResolver(session_factory, settings)
    else:
        resolver = _ConfiguredCalendarRestoreReaderResolver(session_factory, source_cipher)
    return PrepareCalendarRestoreTaskStep(
        session_factory,
        action_cipher=ActionPayloadCipher(source_cipher),
        source_cipher=source_cipher,
        reader_resolver=resolver,
    )


def _required_user_id(task: LeasedTask) -> UUID:
    """要求恢复任务携带租约读取出的用户归属。"""
    if task.user_id is None:
        raise ValueError("calendar.restore.prepare requires user_id")
    return task.user_id


def _required_lease_owner(task: LeasedTask) -> str:
    """要求恢复任务由当前 DurableTaskRunner owner 持有。"""
    if not isinstance(task.lease_owner, str) or task.lease_owner == "":
        raise ValueError("calendar.restore.prepare requires lease_owner")
    return task.lease_owner


def _restore_input(payload: Mapping[str, object]) -> tuple[UUID, str]:
    """解析精确、规范且可重放的恢复准备输入。

    恢复 Worker 必须在解析连接身份或发起供应商 GET 之前完成这层边界校验。精确键集
    防止未来字段被静默解释；UUID 只接受规范小写文本；创建键复用日历提案的公共
    validator，确保 API、应用和 Worker 对空白、长度及所有 Unicode ``Cc`` 控制字符
    采用完全相同的拒绝规则。

    Args:
        payload: 从 PostgreSQL JSONB 读取的任务输入映射。

    Returns:
        已解析的 source snapshot UUID 与原样幂等键。

    Raises:
        TypeError: 外层键集不精确或值类型不正确。
        ValueError: UUID 非规范文本或创建键未通过共享边界校验。
    """
    expected_keys = frozenset({"source_snapshot_id", "creation_idempotency_key"})
    if frozenset(payload) != expected_keys:
        raise TypeError("calendar.restore.prepare input is invalid")
    raw_snapshot = payload["source_snapshot_id"]
    creation_key = payload["creation_idempotency_key"]
    if not isinstance(raw_snapshot, str) or not isinstance(creation_key, str):
        raise TypeError("calendar.restore.prepare input is invalid")
    parsed_snapshot = UUID(raw_snapshot)
    if str(parsed_snapshot) != raw_snapshot:
        raise ValueError("calendar.restore.prepare snapshot id is not canonical")
    return parsed_snapshot, validate_calendar_creation_idempotency_key(creation_key)


def _require_writable_target(
    target: CalendarProposalTargetSnapshot | None,
) -> CalendarProposalTargetSnapshot:
    """在供应商 GET 前要求连接、读写能力和目录写权限均仍有效。"""
    if target is None or target.connection_status is not ConnectionStatus.CONNECTED:
        raise StateConflictError(
            error_code="connection_capability_disabled",
            message="calendar connection capability is disabled",
        )
    statuses = (target.read_capability_status, target.write_capability_status)
    if target.write_capability_error_code == "connection_scope_missing" or any(
        status in {CapabilityStatus.ACTION_REQUIRED, CapabilityStatus.REVOKED}
        for status in statuses
    ):
        raise StateConflictError(
            error_code="connection_scope_missing",
            message="calendar write requires reauthorization for the selected connection",
        )
    if any(status is not CapabilityStatus.ENABLED for status in statuses):
        raise StateConflictError(
            error_code="connection_capability_disabled",
            message="calendar connection capability is disabled",
        )
    if not target.can_write:
        raise StateConflictError(
            error_code="calendar_read_only",
            message="selected calendar is read-only",
        )
    return target


def _credential_aad(user_id: UUID, connection_id: UUID, kind: str) -> bytes:
    """保持 OAuth token 与同步 Worker 完全相同的记录绑定 AAD。"""
    return f"{user_id}:{connection_id}:{kind}".encode("ascii")


__all__ = [
    "CalendarRestoreReaderResolver",
    "PrepareCalendarRestoreTaskStep",
    "build_prepare_calendar_restore_task_step",
]
