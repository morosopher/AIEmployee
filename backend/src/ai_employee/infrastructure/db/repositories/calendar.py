"""提供 Calendar 事件、游标和审计的 SQLAlchemy 事务仓储。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.calendar import (
    CalendarConnectionState,
    CalendarEvent,
    ProviderCalendar,
)
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.domain.errors import StateConflictError, TransientProviderError
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyCalendarSyncRepository:
    """维护 Calendar 事实；所有修改交由 factory 外层事务提交。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._calendar_permissions: dict[tuple[UUID, UUID, str], tuple[str, bool] | None] = {}

    async def _lock_syncable_connection(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> OAuthConnectionModel | None:
        """按固定 connection→capability 顺序锁定并验证日历读取前提。

        PostgreSQL 无需保证多表 ``JOIN ... FOR UPDATE`` 的 rowmark 锁顺序与 SQL 书写顺序
        一致；各同步入口若各自依赖 JOIN，仍可能交错锁定 connection、capability 与 cursor。
        因此所有状态读取、目录提交、游标清理和同步完成都复用本方法：先按所有权与 connected
        状态锁住唯一 connection，再锁定同用户的 enabled ``calendar.read`` 行，调用方随后
        才能获取目录、日历或精确 cursor 锁。

        Args:
            user_id: 当前管理员用户。
            connection_id: 待验证的 OAuth 连接。

        Returns:
            连接与读取能力均存在且已按固定顺序锁定时返回连接行，否则返回 ``None``。
        """
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == "connected",
            )
            .with_for_update()
        )
        if connection is None:
            return None
        capability = await self._session.scalar(
            select(ConnectionCapabilityModel.id)
            .where(
                ConnectionCapabilityModel.connection_id == connection_id,
                ConnectionCapabilityModel.user_id == user_id,
                ConnectionCapabilityModel.capability == "calendar.read",
                ConnectionCapabilityModel.status == "enabled",
            )
            .with_for_update()
        )
        return connection if capability is not None else None

    async def get_state(
        self, *, user_id: UUID, connection_id: UUID, scope_key: str
    ) -> CalendarConnectionState | None:
        """按用户、启用能力和精确日历 scope 锁定 cursor。"""
        connection = await self._lock_syncable_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if connection is None:
            return None
        if scope_key != "directory":
            # 事件 scope 只能来自当前连接的可见目录。锁住目录行直到调用方短事务结束，
            # 使并发 tombstone 必须等事件提交完成，或先删除后令本次同步 fail closed。
            calendar_id = await self._session.scalar(
                select(ProviderCalendarModel.id)
                .where(
                    ProviderCalendarModel.user_id == user_id,
                    ProviderCalendarModel.connection_id == connection_id,
                    ProviderCalendarModel.provider_calendar_id == scope_key,
                )
                .with_for_update()
            )
            if calendar_id is None:
                return None
        cursor = await self._session.scalar(
            select(SyncCursorModel)
            .where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == scope_key,
            )
            .with_for_update()
        )
        if cursor is None:
            cursor = SyncCursorModel(
                connection_id=connection_id,
                resource_kind="calendar",
                scope_key=scope_key,
                cursor=None,
            )
            self._session.add(cursor)
            await self._session.flush()
        return CalendarConnectionState(
            provider=connection.provider,
            scope_key=scope_key,
            cursor=cursor.cursor,
        )

    async def upsert_event(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        event: CalendarEvent,
        encrypted_description: EncryptedValue,
        encrypted_location: EncryptedValue,
    ) -> None:
        """按连接、日历和供应商 event ID 幂等覆盖事件，取消状态保留 tombstone。

        事件自身的 ``locked``/等价事实只能缩小修改能力；真正账户 ACL 必须来自同用户、
        同连接、同 calendar 的目录行。两者取交集后持久化，目录缺失时 fail closed，避免
        旧 primary 兼容任务或伪造 scope 把事件标记为可写。
        """
        permission = await self._calendar_permission_projection(
            user_id=user_id,
            connection_id=connection_id,
            calendar_id=event.calendar_id,
        )
        access_role = permission[0] if permission is not None else event.access_role
        can_edit = (
            permission is not None
            and permission[1]
            and event.can_edit
            and event.status != "cancelled"
        )
        stmt = insert(CalendarEventModel).values(
            user_id=user_id,
            connection_id=connection_id,
            provider_event_id=event.event_id,
            calendar_id=event.calendar_id,
            title=event.title,
            description_ciphertext=encrypted_description.ciphertext,
            description_nonce=encrypted_description.nonce,
            description_key_version=encrypted_description.key_version,
            location_ciphertext=encrypted_location.ciphertext,
            location_nonce=encrypted_location.nonce,
            location_key_version=encrypted_location.key_version,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            all_day=event.all_day,
            transparency=event.transparency,
            status=event.status,
            timezone=event.timezone,
            recurring_event_id=event.recurring_event_id,
            etag=event.etag,
            organizer=dict(event.organizer) if event.organizer is not None else None,
            attendees=[dict(attendee) for attendee in event.attendees],
            access_role=access_role,
            can_edit=can_edit,
            provider_url=event.provider_url,
            provider_updated_at=event.updated_at,
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_calendar_events_connection_calendar_provider_event",
                set_={
                    key: getattr(stmt.excluded, key)
                    for key in (
                        "title",
                        "description_ciphertext",
                        "description_nonce",
                        "description_key_version",
                        "location_ciphertext",
                        "location_nonce",
                        "location_key_version",
                        "starts_at",
                        "ends_at",
                        "all_day",
                        "transparency",
                        "status",
                        "timezone",
                        "recurring_event_id",
                        "etag",
                        "organizer",
                        "attendees",
                        "access_role",
                        "can_edit",
                        "provider_url",
                        "provider_updated_at",
                    )
                },
            )
        )

    async def _calendar_permission_projection(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        calendar_id: str,
    ) -> tuple[str, bool] | None:
        """读取并缓存同一短事务内的目录访问角色与写能力。

        一个事件同步事务只处理一个 calendar scope，但可能包含多页和大量事件；缓存避免
        为每个事件重复查询相同 ACL。缓存键仍包含用户和连接，不能跨所有权边界复用。
        """
        key = (user_id, connection_id, calendar_id)
        if key not in self._calendar_permissions:
            row = (
                await self._session.execute(
                    select(
                        ProviderCalendarModel.access_role,
                        ProviderCalendarModel.can_write,
                    ).where(
                        ProviderCalendarModel.user_id == user_id,
                        ProviderCalendarModel.connection_id == connection_id,
                        ProviderCalendarModel.provider_calendar_id == calendar_id,
                    )
                )
            ).one_or_none()
            self._calendar_permissions[key] = (
                (row.access_role, row.can_write) if row is not None else None
            )
        return self._calendar_permissions[key]

    async def mark_directory_success(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        calendars: tuple[ProviderCalendar, ...],
        expected_cursor: str | None,
        next_cursor: str,
        completed_at: datetime,
    ) -> tuple[str, ...]:
        """原子保存目录事实、建立日历 placeholder 并推进独立目录游标。

        目录页已在供应商 I/O 边界完成分页、字段校验和稳定排序；此方法只在一个短事务内
        做用户/连接能力复核、可见目录 upsert、增量 tombstone 或全量快照差集清理、事件 ACL
        收紧、日历 cursor placeholder 与 directory CAS。持续可见日历保留 cursor；本次事务前
        没有目录投影的首次发现或重新出现日历必须清空保留 cursor 与成功时间，强制用受限完整
        窗口重建已经撤销的缓存。CalendarList 410 回退仍不会影响持续可见日历的恢复位置。

        Args:
            user_id: 当前管理员用户。
            connection_id: 已连接的 Google/Microsoft 连接主键。
            calendars: 已规范化且按 provider calendar ID 稳定排序的可见项与删除项聚合。
            expected_cursor: 读取目录前观察到的 directory cursor。
            next_cursor: 供应商最终确认的 directory cursor。
            completed_at: 注入的 UTC 完成时间。

        Returns:
            该连接当前仍可见的全部日历 ID，供后续逐日历同步使用。

        Raises:
            StateConflictError: 连接能力撤销、目录对象不安全或 CAS 竞争。
        """
        if next_cursor == "":
            raise StateConflictError(
                error_code="calendar_directory_cursor_invalid",
                message="Calendar directory cursor is invalid",
            )
        calendar_ids = tuple(calendar.calendar_id for calendar in calendars)
        if (
            any(calendar_id in {"", "directory"} for calendar_id in calendar_ids)
            or len(set(calendar_ids)) != len(calendar_ids)
            or tuple(sorted(calendar_ids)) != calendar_ids
        ):
            raise StateConflictError(
                error_code="calendar_directory_scopes_invalid",
                message="Calendar directory scopes are not validated and stably sorted",
            )
        connection = await self._lock_syncable_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if connection is None:
            raise StateConflictError(
                error_code="calendar_connection_not_syncable",
                message="Calendar connection is no longer available for discovery",
            )

        directory_cursor = await self._session.scalar(
            select(SyncCursorModel)
            .where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == "directory",
            )
            .with_for_update()
        )
        if directory_cursor is None:
            directory_cursor = SyncCursorModel(
                connection_id=connection_id,
                resource_kind="calendar",
                scope_key="directory",
                cursor=None,
            )
            self._session.add(directory_cursor)
            await self._session.flush()
        if directory_cursor.cursor != expected_cursor:
            raise TransientProviderError(
                error_code="calendar_directory_cursor_conflict",
                message="Calendar directory cursor changed during provider read",
                retry_after=1,
            )

        # 必须在删除 tombstone 与 upsert 新目录行之前记录当前可见集合。cursor 行会跨目录
        # 删除保留，仅凭 cursor 是否存在无法区分持续可见与重新出现，正是空 delta 无法恢复
        # 缓存的根因。锁定这些行也让同一连接的目录变更在本事务内保持稳定。
        existing_visible_calendar_ids = set(
            (
                await self._session.scalars(
                    select(ProviderCalendarModel.provider_calendar_id)
                    .where(
                        ProviderCalendarModel.user_id == user_id,
                        ProviderCalendarModel.connection_id == connection_id,
                    )
                    .with_for_update()
                )
            ).all()
        )

        visible_calendars = tuple(calendar for calendar in calendars if not calendar.is_deleted)
        visible_calendar_ids = tuple(calendar.calendar_id for calendar in visible_calendars)
        explicit_deleted_calendar_ids = tuple(
            calendar.calendar_id for calendar in calendars if calendar.is_deleted
        )
        removed_calendar_ids = set(explicit_deleted_calendar_ids)
        if expected_cursor is None:
            # 初始同步与 410 回退返回完整目录快照；未再次出现的历史行已经不再可见，
            # 必须与显式 tombstone 合并清理。增量 delta 不具备该完备性，绝不能做差集。
            removed_calendar_ids.update(
                existing_visible_calendar_ids.difference(visible_calendar_ids)
            )
        ordered_removed_calendar_ids = tuple(sorted(removed_calendar_ids))
        for calendar_id in ordered_removed_calendar_ids:
            # CalendarList tombstone 撤销的是当前目录与来源缓存事实；独立 cursor 和历史
            # 审计仍保留，以便重获访问权后由受限增量/410 回退安全恢复。
            await self._session.execute(
                delete(CalendarEventModel).where(
                    CalendarEventModel.user_id == user_id,
                    CalendarEventModel.connection_id == connection_id,
                    CalendarEventModel.calendar_id == calendar_id,
                )
            )
            await self._session.execute(
                delete(ProviderCalendarModel).where(
                    ProviderCalendarModel.user_id == user_id,
                    ProviderCalendarModel.connection_id == connection_id,
                    ProviderCalendarModel.provider_calendar_id == calendar_id,
                )
            )

        for calendar in visible_calendars:
            statement = insert(ProviderCalendarModel).values(
                user_id=user_id,
                connection_id=connection_id,
                provider_calendar_id=calendar.calendar_id,
                name=calendar.display_name,
                timezone=calendar.timezone,
                is_primary=calendar.is_primary,
                access_role=calendar.access_role,
                can_write=calendar.can_write,
                provider_url=calendar.provider_url,
            )
            await self._session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_provider_calendars_connection_provider_calendar",
                    set_={
                        "name": statement.excluded.name,
                        "timezone": statement.excluded.timezone,
                        "is_primary": statement.excluded.is_primary,
                        "access_role": statement.excluded.access_role,
                        "can_write": statement.excluded.can_write,
                        "provider_url": statement.excluded.provider_url,
                    },
                )
            )

            permission_values: dict[str, object] = {"access_role": calendar.access_role}
            if not calendar.can_write:
                # ACL 降级必须立即收紧历史事件；升级时保留既有 can_edit=false，等待
                # 后续事件列表或精确 GET 再结合 locked 等事件事实确认，禁止盲目放宽。
                permission_values["can_edit"] = False
            await self._session.execute(
                update(CalendarEventModel)
                .where(
                    CalendarEventModel.user_id == user_id,
                    CalendarEventModel.connection_id == connection_id,
                    CalendarEventModel.calendar_id == calendar.calendar_id,
                )
                .values(**permission_values)
            )

        if visible_calendar_ids:
            cursor_rows = tuple(
                (
                    await self._session.scalars(
                        select(SyncCursorModel)
                        .where(
                            SyncCursorModel.connection_id == connection_id,
                            SyncCursorModel.resource_kind == "calendar",
                            SyncCursorModel.scope_key.in_(visible_calendar_ids),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            cursors_by_scope = {cursor.scope_key: cursor for cursor in cursor_rows}
            for calendar_id in visible_calendar_ids:
                cursor = cursors_by_scope.get(calendar_id)
                if cursor is None:
                    # placeholder 只证明目录已发现该 calendar；事件同步的尝试/成功时间和
                    # opaque token 必须由该日历独立的 finish_sync 事务写入。
                    self._session.add(
                        SyncCursorModel(
                            connection_id=connection_id,
                            resource_kind="calendar",
                            scope_key=calendar_id,
                            cursor=None,
                        )
                    )
                elif calendar_id not in existing_visible_calendar_ids:
                    # tombstone/full-snapshot 缺席会删除目录与事件缓存但保留 cursor。重新
                    # 获得访问权后旧 delta 可能合法返回空页，必须先失效旧恢复位置与 freshness，
                    # 让紧随目录同步的事件路径确定性调用 initial_pages() 重建缓存。
                    cursor.cursor = None
                    cursor.last_success_at = None

        directory_cursor.cursor = next_cursor
        directory_cursor.last_success_at = completed_at
        directory_cursor.last_attempt_at = completed_at
        directory_cursor.last_error_code = None
        all_calendar_ids = tuple(
            (
                await self._session.scalars(
                    select(ProviderCalendarModel.provider_calendar_id)
                    .where(
                        ProviderCalendarModel.user_id == user_id,
                        ProviderCalendarModel.connection_id == connection_id,
                    )
                    .order_by(ProviderCalendarModel.provider_calendar_id)
                )
            ).all()
        )
        self._session.add(
            AuditEventModel(
                user_id=user_id,
                task_id=None,
                event_type="source.calendar.directory_discovered",
                actor_type="system",
                actor_id=str(connection_id),
                event_metadata={
                    "calendar_count": len(visible_calendar_ids),
                    "removed_calendar_count": len(ordered_removed_calendar_ids),
                    "scope_key": "directory",
                    "cutoff": completed_at.isoformat(),
                },
            )
        )
        return tuple(all_calendar_ids)

    async def clear_cursor(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scope_key: str,
        expected_cursor: str,
    ) -> None:
        """按 connection→capability→cursor 固定锁序执行单 scope 失效 CAS。"""
        connection = await self._lock_syncable_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if connection is None:
            raise TransientProviderError(
                error_code="calendar_sync_cursor_conflict",
                message="Calendar sync cursor changed during provider read",
                retry_after=1,
            )
        cursor = await self._session.scalar(
            select(SyncCursorModel)
            .where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == scope_key,
            )
            .with_for_update()
        )
        if cursor is None or cursor.cursor != expected_cursor:
            raise TransientProviderError(
                error_code="calendar_sync_cursor_conflict",
                message="Calendar sync cursor changed during provider read",
                retry_after=1,
            )
        cursor.cursor = None

    async def finish_sync(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scope_key: str,
        expected_cursor: str | None,
        next_cursor: str,
        event_count: int,
        used_full_resync: bool,
        completed_at: datetime,
    ) -> None:
        """CAS 验证原 scoped cursor 后推进最终 token，同时追加无敏感字段审计。"""
        connection = await self._lock_syncable_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if connection is None:
            raise StateConflictError(
                error_code="calendar_connection_not_syncable",
                message="Calendar connection is no longer available",
            )
        cursor = await self._session.scalar(
            select(SyncCursorModel)
            .where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == scope_key,
            )
            .with_for_update()
        )
        if cursor is None:
            cursor = SyncCursorModel(
                connection_id=connection_id,
                resource_kind="calendar",
                scope_key=scope_key,
                cursor=None,
            )
            self._session.add(cursor)
        if cursor.cursor != expected_cursor:
            raise TransientProviderError(
                error_code="calendar_sync_cursor_conflict",
                message="Calendar sync cursor changed during provider read",
                retry_after=1,
            )
        cursor.cursor, cursor.last_success_at, cursor.last_attempt_at, cursor.last_error_code = (
            next_cursor,
            completed_at,
            completed_at,
            None,
        )
        self._session.add(
            AuditEventModel(
                user_id=user_id,
                task_id=None,
                event_type="source.calendar.synced",
                actor_type="system",
                actor_id=str(connection_id),
                event_metadata={
                    "events_upserted": event_count,
                    "scope_key": scope_key,
                    "used_full_resync": used_full_resync,
                    # 仅记录同步完成的 UTC cutoff，不复制 title、description 或地点。
                    "cutoff": completed_at.isoformat(),
                },
            )
        )

    async def mark_calendar_capability_action_required(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        error_code: str = "microsoft_calendar_permission_required",
    ) -> None:
        """仅降级 calendar.read 能力，保留连接与其它邮件能力。

        Graph 403 只证明当前 delegated 日历 scope 不可用；不能把身份连接误记为 revoked，
        也不能清除其它日历或邮件的恢复位置。通过同用户/连接组合条件加锁，避免跨用户
        capability 被错误修改。
        """
        capability = await self._session.scalar(
            select(ConnectionCapabilityModel)
            .join(
                OAuthConnectionModel,
                (OAuthConnectionModel.id == ConnectionCapabilityModel.connection_id)
                & (OAuthConnectionModel.user_id == ConnectionCapabilityModel.user_id),
            )
            .where(
                ConnectionCapabilityModel.user_id == user_id,
                ConnectionCapabilityModel.connection_id == connection_id,
                ConnectionCapabilityModel.capability == "calendar.read",
                OAuthConnectionModel.status == "connected",
            )
            .with_for_update()
        )
        if capability is not None:
            capability.status = "action_required"
            capability.last_error_code = error_code


class SqlAlchemyCalendarSyncRepositoryFactory:
    """为每次日历同步提供自动提交/回滚的真实事务。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SqlAlchemyCalendarSyncRepository]:
        """异常时使事件、游标和审计一起回滚。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyCalendarSyncRepository(session)


@dataclass(frozen=True, slots=True)
class EnabledSyncScope:
    """表示已通过连接状态和读取能力过滤的一个持久同步 scope。"""

    user_id: UUID
    connection_id: UUID
    provider: str
    resource_kind: str
    scope_key: str


class SqlAlchemyEnabledSyncScopeReader:
    """读取所有供应商已启用 read capability 对应的普通周期 owner。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存短会话工厂，不接触凭据密文。"""
        self._session_factory = session_factory

    async def enabled_scopes(self) -> tuple[EnabledSyncScope, ...]:
        """返回 connected 且能力 enabled 的 mail/calendar 周期 owners。

        邮件仍消费 PostgreSQL 已有的 mailbox owner；Task 12/13 后所有支持 provider 的
        Calendar 无论持久层仍只有迁移保留的 primary，还是已经包含 directory 与多个 provider
        calendar cursor，都只向普通周期投影一个连接级 ``directory`` owner。每个事件 cursor
        继续是独立权威恢复位置，只是不再由普通周期并发排队；显式维修任务可直接使用原
        calendar ID。
        未知资源种类和 disabled 能力在 SQL 层直接排除。
        """
        async with self._session_factory() as session:
            rows = await session.execute(
                select(
                    OAuthConnectionModel.user_id,
                    OAuthConnectionModel.id.label("connection_id"),
                    OAuthConnectionModel.provider,
                    SyncCursorModel.resource_kind,
                    SyncCursorModel.scope_key,
                )
                .join(
                    ConnectionCapabilityModel,
                    (ConnectionCapabilityModel.connection_id == OAuthConnectionModel.id)
                    & (ConnectionCapabilityModel.user_id == OAuthConnectionModel.user_id),
                )
                .join(
                    SyncCursorModel,
                    SyncCursorModel.connection_id == OAuthConnectionModel.id,
                )
                .where(
                    OAuthConnectionModel.status == "connected",
                    ConnectionCapabilityModel.status == "enabled",
                    # Microsoft 的真实 folder cursor 是恢复事实，但普通周期只能触发一次
                    # mailbox 目录 owner；显式 folder task 仍可由维修/恢复路径直接创建。
                    or_(
                        OAuthConnectionModel.provider != "microsoft",
                        SyncCursorModel.resource_kind != "mail",
                        SyncCursorModel.scope_key == "mailbox",
                    ),
                    or_(
                        and_(
                            ConnectionCapabilityModel.capability == "mail.read",
                            SyncCursorModel.resource_kind == "mail",
                        ),
                        and_(
                            ConnectionCapabilityModel.capability == "calendar.read",
                            SyncCursorModel.resource_kind == "calendar",
                        ),
                    ),
                )
                .order_by(
                    OAuthConnectionModel.user_id,
                    OAuthConnectionModel.id,
                    SyncCursorModel.resource_kind.desc(),
                    SyncCursorModel.scope_key,
                )
            )
            result: list[EnabledSyncScope] = []
            seen_calendar_owners: set[tuple[UUID, UUID]] = set()
            for row in rows:
                scope_key = row.scope_key
                if row.resource_kind == "calendar":
                    owner = (row.user_id, row.connection_id)
                    if owner in seen_calendar_owners:
                        continue
                    seen_calendar_owners.add(owner)
                    scope_key = "directory"
                result.append(
                    EnabledSyncScope(
                        user_id=row.user_id,
                        connection_id=row.connection_id,
                        provider=row.provider,
                        resource_kind=row.resource_kind,
                        scope_key=scope_key,
                    )
                )
            return tuple(result)
