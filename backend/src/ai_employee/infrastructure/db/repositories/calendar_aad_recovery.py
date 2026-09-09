"""持久化 0019 精确 marker 恢复计划与共享的 marker/task CAS 读取。

扫描从数据库 owner 推导用户，不接受外部过滤；连接、能力、目录、游标、任务按固定
顺序锁定。新 ordinal 复用既有 TaskRun/Audit/Outbox 原子 writer，不修改历史终态。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.calendar_aad_recovery import (
    CALENDAR_AAD_RECOVERY_KIND,
    CALENDAR_AAD_RECOVERY_MARKER,
    CalendarAadRecoveryAttempt,
    CalendarAadRecoveryInput,
    CalendarAadTaskBinding,
    MarkedCalendarScopeState,
)
from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadPair,
    CalendarAadRolloutError,
)
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepository
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


async def lock_calendar_aad_scope(
    session: AsyncSession, *, user_id: UUID, connection_id: UUID, scope_key: str
) -> MarkedCalendarScopeState | None:
    """锁定并重证精确恢复前提；marker 已清返回 None，缺失 cursor 永不补造。

    Returns:
        已冻结的 owner/generation/目录身份/cursor 身份；该短事务不读取凭据明文。

    Raises:
        CalendarAadRolloutError: 连接、能力、目录或被失效游标的事实发生变化。
    """
    connection = await session.scalar(
        select(OAuthConnectionModel)
        .where(
            OAuthConnectionModel.id == connection_id,
            OAuthConnectionModel.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        connection is None
        or connection.status != "connected"
        or connection.provider not in {"google", "microsoft"}
    ):
        raise CalendarAadRolloutError("calendar_aad_recovery_state_changed")
    capability = await session.scalar(
        select(ConnectionCapabilityModel)
        .where(
            ConnectionCapabilityModel.user_id == user_id,
            ConnectionCapabilityModel.connection_id == connection_id,
            ConnectionCapabilityModel.capability == "calendar.read",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if capability is None or capability.status != "enabled":
        raise CalendarAadRolloutError("calendar_aad_recovery_state_changed")
    calendar = await session.scalar(
        select(ProviderCalendarModel)
        .where(
            ProviderCalendarModel.user_id == user_id,
            ProviderCalendarModel.connection_id == connection_id,
            ProviderCalendarModel.provider_calendar_id == scope_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    cursor = await session.scalar(
        select(SyncCursorModel)
        .where(
            SyncCursorModel.connection_id == connection_id,
            SyncCursorModel.resource_kind == "calendar",
            SyncCursorModel.scope_key == scope_key,
            SyncCursorModel.scope_key != "directory",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if calendar is None or cursor is None:
        raise CalendarAadRolloutError("calendar_aad_recovery_state_changed")
    if cursor.last_error_code != CALENDAR_AAD_RECOVERY_MARKER:
        return None
    if cursor.cursor is not None or cursor.last_success_at is not None:
        raise CalendarAadRolloutError("calendar_aad_recovery_state_changed")
    timezone = await session.scalar(select(UserModel.timezone).where(UserModel.id == user_id))
    if timezone is None:
        raise CalendarAadRolloutError("calendar_aad_recovery_state_changed")
    return MarkedCalendarScopeState(
        CalendarAadPair(
            user_id,
            connection_id,
            scope_key,
            connection.provider,
            timezone,
            connection.authorization_generation,
        ),
        calendar.id,
        cursor.id,
        cursor.last_attempt_at,
    )


async def require_calendar_aad_task(
    session: AsyncSession, *, binding: CalendarAadTaskBinding, now: datetime
) -> None:
    """在 cursor 之后锁定任务，校验精确输入、幂等键、running 状态和未过期租约。

    解析 LeasedTask 只证明调度输入形状；此处用用户条件重读持久行，防止 provider 窗口
    中输入、ordinal、所有权或租约已被改变但旧快照仍继续提交。
    """
    row = await session.scalar(
        select(TaskRunModel)
        .where(
            TaskRunModel.id == binding.task_id,
            TaskRunModel.user_id == binding.user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise CalendarAadRolloutError("calendar_aad_recovery_state_changed")
    try:
        parsed = CalendarAadRecoveryInput.parse(row.input_payload)
    except CalendarAadRolloutError:
        raise CalendarAadRolloutError("calendar_aad_recovery_state_changed") from None
    if (
        row.kind != CALENDAR_AAD_RECOVERY_KIND
        or parsed != binding.input
        or row.idempotency_key != binding.input.idempotency_key
        or row.status != TaskStatus.RUNNING.value
        or row.lease_owner != binding.lease_owner
        or row.lease_expires_at is None
        or row.lease_expires_at <= now
    ):
        raise CalendarAadRolloutError("calendar_aad_recovery_state_changed")


class SqlAlchemyCalendarAadRecoveryStore:
    """在单一调用方事务内扫描 marker、验证历史 ordinal 并原子创建任务。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定已由应用打开的短事务，不自行 commit 或执行 provider I/O。"""
        self._session = session

    async def marked_pairs(self) -> tuple[CalendarAadPair, ...]:
        """仅由typed CurrentGuard包围的调用方扫描非目录marker，保留坏owner/目录以拒绝。

        revision由固定one-off的owner RR/RO完整事实读取证明；应用基线无版本表权限。
        本store只提供app业务查询/锁定/CAS，不接收缓存revision或owner连接。
        """
        rows = await self._session.execute(
            select(
                SyncCursorModel.connection_id,
                SyncCursorModel.scope_key,
                OAuthConnectionModel.user_id,
                OAuthConnectionModel.provider,
                OAuthConnectionModel.authorization_generation,
                UserModel.timezone,
                ProviderCalendarModel.id.label("calendar_row_id"),
            )
            .select_from(SyncCursorModel)
            .outerjoin(
                OAuthConnectionModel,
                OAuthConnectionModel.id == SyncCursorModel.connection_id,
            )
            .outerjoin(UserModel, UserModel.id == OAuthConnectionModel.user_id)
            .outerjoin(
                ProviderCalendarModel,
                (ProviderCalendarModel.connection_id == SyncCursorModel.connection_id)
                & (ProviderCalendarModel.user_id == OAuthConnectionModel.user_id)
                & (ProviderCalendarModel.provider_calendar_id == SyncCursorModel.scope_key),
            )
            .where(
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key != "directory",
                SyncCursorModel.last_error_code == CALENDAR_AAD_RECOVERY_MARKER,
            )
            .order_by(SyncCursorModel.connection_id, SyncCursorModel.scope_key)
        )
        pairs: list[CalendarAadPair] = []
        for row in rows:
            if row.user_id is None or row.calendar_row_id is None or row.timezone is None:
                raise CalendarAadRolloutError("calendar_aad_local_recoverability_failed")
            pair = CalendarAadPair(
                row.user_id,
                row.connection_id,
                row.scope_key,
                row.provider,
                row.timezone,
                row.authorization_generation,
            )
            try:
                _ = pair.digest
            except (TypeError, ValueError):
                raise CalendarAadRolloutError("calendar_aad_recovery_input_invalid") from None
            pairs.append(pair)
        return tuple(pairs)

    async def lock_marked_pair(self, pair: CalendarAadPair) -> bool:
        """按固定锁序重读 exact marker，确保同 pair 的并发 planner 只能分配一个 ordinal。"""
        current = await lock_calendar_aad_scope(
            self._session,
            user_id=pair.user_id,
            connection_id=pair.connection_id,
            scope_key=pair.calendar_id,
        )
        if current is not None and current.pair != pair:
            raise CalendarAadRolloutError("calendar_aad_recovery_state_changed")
        return current is not None

    async def attempts(self, pair: CalendarAadPair) -> tuple[CalendarAadRecoveryAttempt, ...]:
        """用精确 pair 输入、digest 或规范键发现候选，再严格验证所有历史行。

        任一身份部分命中都不能隐藏另一部分篡改；用户条件始终来自 owning connection，
        不读取其他用户任务。锁定顺序为已持有 cursor 后按任务 UUID，避免多 planner 互锁。
        """
        rows = await self._session.scalars(
            select(TaskRunModel)
            .where(
                TaskRunModel.user_id == pair.user_id,
                or_(
                    TaskRunModel.idempotency_key.startswith(
                        f"calendar-aad-0019:{pair.digest}:attempt:", autoescape=True
                    ),
                    (TaskRunModel.kind == CALENDAR_AAD_RECOVERY_KIND)
                    & (
                        (TaskRunModel.input_payload["pair_digest"].astext == pair.digest)
                        | (
                            (
                                TaskRunModel.input_payload["connection_id"].astext
                                == str(pair.connection_id)
                            )
                            & (TaskRunModel.input_payload["scope_key"].astext == pair.calendar_id)
                        )
                    ),
                ),
            )
            .order_by(TaskRunModel.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        attempts: list[CalendarAadRecoveryAttempt] = []
        for row in rows:
            try:
                parsed = CalendarAadRecoveryInput.parse(row.input_payload)
                status = TaskStatus(row.status)
                if (
                    row.kind != CALENDAR_AAD_RECOVERY_KIND
                    or parsed.connection_id != pair.connection_id
                    or parsed.scope_key != pair.calendar_id
                    or parsed.pair_digest != pair.digest
                    or row.idempotency_key != parsed.idempotency_key
                ):
                    raise ValueError("recovery intent changed")
            except (CalendarAadRolloutError, ValueError):
                raise CalendarAadRolloutError("calendar_aad_recovery_invariant") from None
            attempts.append(CalendarAadRecoveryAttempt(row.id, parsed, status))
        return tuple(attempts)

    async def create(self, pair: CalendarAadPair, input: CalendarAadRecoveryInput) -> UUID:
        """复用任务三事实 writer，唯一赢家仍必须满足固定 kind/完整输入/键。"""
        try:
            result = await SqlAlchemyTaskRepository(self._session).create_with_outbox(
                user_id=pair.user_id,
                kind=CALENDAR_AAD_RECOVERY_KIND,
                input_payload=input.payload(),
                idempotency_key=input.idempotency_key,
            )
        except StateConflictError:
            raise CalendarAadRolloutError("calendar_aad_recovery_invariant") from None
        return result.task_id


class SqlAlchemyCalendarAadRecoveryStoreFactory:
    """为每次 planner 或只读 marker 查询提供自动回滚的应用事务。"""

    def __init__(self, sessions: ManagedAsyncSessionMaker) -> None:
        """复用进程级 managed session factory；不建立第二条数据库生命周期通路。"""
        self._sessions = sessions

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SqlAlchemyCalendarAadRecoveryStore]:
        """所有 pair 任务及末尾 guard 同属该事务，任何失败回滚三事实。"""
        async with self._sessions.begin() as session:
            yield SqlAlchemyCalendarAadRecoveryStore(session)
