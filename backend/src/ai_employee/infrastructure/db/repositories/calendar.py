"""提供 Calendar 事件、游标和审计的 SQLAlchemy 事务仓储。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.calendar import CalendarConnectionState, CalendarEvent
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.domain.errors import StateConflictError, TransientProviderError
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
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
        self, *, user_id: UUID, connection_id: UUID
    ) -> CalendarConnectionState | None:
        """锁定有效连接和 cursor，避免并行网络读取后提交倒退。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.provider == "google",
                OAuthConnectionModel.status == "connected",
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
            )
            .with_for_update()
        )
        if cursor is None:
            cursor = SyncCursorModel(
                connection_id=connection_id, resource_kind="calendar", cursor=None
            )
            self._session.add(cursor)
            await self._session.flush()
        return CalendarConnectionState(cursor.cursor)

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
                        "provider_url",
                        "provider_updated_at",
                    )
                },
            )
        )

    async def clear_cursor(
        self, *, user_id: UUID, connection_id: UUID, expected_cursor: str
    ) -> None:
        """在 410 后以 CAS 清除失效 token，避免覆盖已完成的并发同步。"""
        cursor = await self._session.scalar(
            select(SyncCursorModel)
            .join(OAuthConnectionModel, OAuthConnectionModel.id == SyncCursorModel.connection_id)
            .where(
                SyncCursorModel.connection_id == connection_id,
                SyncCursorModel.resource_kind == "calendar",
                OAuthConnectionModel.user_id == user_id,
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
        expected_cursor: str | None,
        next_sync_token: str,
        event_count: int,
        used_full_resync: bool,
        completed_at: datetime,
    ) -> None:
        """CAS 验证原 cursor 后推进最终 token，同时追加无敏感字段审计。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.provider == "google",
                OAuthConnectionModel.status == "connected",
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
            )
            .with_for_update()
        )
        if cursor is None:
            cursor = SyncCursorModel(
                connection_id=connection_id, resource_kind="calendar", cursor=None
            )
            self._session.add(cursor)
        if cursor.cursor != expected_cursor:
            raise TransientProviderError(
                error_code="calendar_sync_cursor_conflict",
                message="Calendar sync cursor changed during provider read",
                retry_after=1,
            )
        cursor.cursor, cursor.last_success_at, cursor.last_attempt_at, cursor.last_error_code = (
            next_sync_token,
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
                    "cursor": next_sync_token,
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


class SqlAlchemyConnectedGoogleReader:
    """读取健康 Google 连接，供固定周期调度器使用。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存短会话工厂，不接触凭据密文。"""
        self._session_factory = session_factory

    async def connected_connections(self) -> tuple[tuple[UUID, UUID], ...]:
        """只返回明确 connected 的用户和连接，degraded/断开状态不会被吞掉。"""
        async with self._session_factory() as session:
            rows = await session.execute(
                select(OAuthConnectionModel.user_id, OAuthConnectionModel.id).where(
                    OAuthConnectionModel.provider == "google",
                    OAuthConnectionModel.status == "connected",
                )
            )
            return tuple((row.user_id, row.id) for row in rows)
