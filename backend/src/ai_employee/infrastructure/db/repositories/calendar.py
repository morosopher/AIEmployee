"""提供 Calendar 事件、游标和审计的 SQLAlchemy 事务仓储。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.calendar import CalendarConnectionState, CalendarEvent
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.domain.errors import StateConflictError, TransientProviderError
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyCalendarSyncRepository:
    """维护 Calendar 事实；所有修改交由 factory 外层事务提交。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_state(
        self, *, user_id: UUID, connection_id: UUID, scope_key: str
    ) -> CalendarConnectionState | None:
        """按用户、启用能力和精确日历 scope 锁定 cursor。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .join(
                ConnectionCapabilityModel,
                (ConnectionCapabilityModel.connection_id == OAuthConnectionModel.id)
                & (ConnectionCapabilityModel.user_id == OAuthConnectionModel.user_id),
            )
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == "connected",
                ConnectionCapabilityModel.capability == "calendar.read",
                ConnectionCapabilityModel.status == "enabled",
            )
            .with_for_update()
        )
        if connection is None:
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
        """按连接和供应商 event ID 幂等覆盖事件，取消状态保留 tombstone。"""
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
            access_role=event.access_role,
            can_edit=event.can_edit,
            provider_url=event.provider_url,
            provider_updated_at=event.updated_at,
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_calendar_events_connection_provider_event",
                set_={
                    key: getattr(stmt.excluded, key)
                    for key in (
                        "calendar_id",
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

    async def clear_cursor(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scope_key: str,
        expected_cursor: str,
    ) -> None:
        """在游标失效后以 CAS 只清除同一日历 scope，避免影响其他日历。"""
        cursor = await self._session.scalar(
            select(SyncCursorModel)
            .join(OAuthConnectionModel, OAuthConnectionModel.id == SyncCursorModel.connection_id)
            .join(
                ConnectionCapabilityModel,
                (ConnectionCapabilityModel.connection_id == OAuthConnectionModel.id)
                & (ConnectionCapabilityModel.user_id == OAuthConnectionModel.user_id),
            )
            .where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                SyncCursorModel.scope_key == scope_key,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == "connected",
                ConnectionCapabilityModel.capability == "calendar.read",
                ConnectionCapabilityModel.status == "enabled",
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
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .join(
                ConnectionCapabilityModel,
                (ConnectionCapabilityModel.connection_id == OAuthConnectionModel.id)
                & (ConnectionCapabilityModel.user_id == OAuthConnectionModel.user_id),
            )
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == "connected",
                ConnectionCapabilityModel.capability == "calendar.read",
                ConnectionCapabilityModel.status == "enabled",
            )
            .with_for_update()
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
    """读取所有供应商已启用 read capability 对应的持久游标 scope。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存短会话工厂，不接触凭据密文。"""
        self._session_factory = session_factory

    async def enabled_scopes(self) -> tuple[EnabledSyncScope, ...]:
        """返回 connected 且能力 enabled 的 mail/calendar scopes。

        调度器只消费 PostgreSQL 已有的恢复位置：一个连接可以有多个 mailbox/folder 或
        provider calendar，查询不会以连接级固定双任务覆盖它们。未知资源种类和 disabled
        能力在 SQL 层直接排除，Scheduler 不需要重复领域判断。
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
            return tuple(
                EnabledSyncScope(
                    user_id=row.user_id,
                    connection_id=row.connection_id,
                    provider=row.provider,
                    resource_kind=row.resource_kind,
                    scope_key=row.scope_key,
                )
                for row in rows
            )
