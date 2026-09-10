"""提供用户设置、默认连接能力和日历目录的事务性 SQLAlchemy 适配器。"""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.settings import UserSettingsView
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability, ConnectionStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.settings import WeeklyWorkingHours
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel


class SqlAlchemySettingsRepository:
    """在调用方短事务内读取、锁定并更新当前用户的完整设置。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由 API 依赖控制提交/回滚的异步会话。"""
        self._session = session

    async def get(self, *, user_id: UUID) -> UserSettingsView | None:
        """按用户读取设置投影，不物化密码、会话或凭据。"""
        user = await self._session.scalar(select(UserModel).where(UserModel.id == user_id))
        return None if user is None else _settings_view(user)

    async def get_for_update(self, *, user_id: UUID) -> UserModel | None:
        """兼容既有调用方并锁定用户设置，保证 PATCH 与审计同事务提交。"""
        return await self._session.scalar(
            select(UserModel).where(UserModel.id == user_id).with_for_update()
        )

    async def update(
        self,
        *,
        user_id: UUID,
        values: Mapping[str, object],
    ) -> UserSettingsView | None:
        """验证合并后的默认连接/日历能力，再写入设置与 content-free 审计。"""
        user = await self.get_for_update(user_id=user_id)
        if user is None:
            return None
        if not user.is_active:
            # 认证可能早于最终匿名化；当前短事务的 user 锁才决定是否仍可改默认值/审计。
            raise StateConflictError(error_code="user_inactive", message="User is inactive")
        merged_mail_connection_id = values.get(
            "default_mail_connection_id", user.default_mail_connection_id
        )
        merged_calendar_connection_id = values.get(
            "default_calendar_connection_id", user.default_calendar_connection_id
        )
        merged_calendar_id = values.get("default_calendar_id", user.default_calendar_id)
        if merged_mail_connection_id is not None:
            if not isinstance(merged_mail_connection_id, UUID):
                raise TypeError("default mail connection id must be UUID or None")
            await self._require_enabled_capability(
                user_id=user_id,
                connection_id=merged_mail_connection_id,
                capability=ConnectionCapability.MAIL_SEND,
            )
        if (merged_calendar_connection_id is None) != (merged_calendar_id is None):
            raise _connection_capability_disabled()
        if merged_calendar_connection_id is not None:
            if not isinstance(merged_calendar_connection_id, UUID) or not isinstance(
                merged_calendar_id, str
            ):
                raise TypeError("default calendar identity is invalid")
            await self._require_enabled_capability(
                user_id=user_id,
                connection_id=merged_calendar_connection_id,
                capability=ConnectionCapability.CALENDAR_WRITE,
            )
            calendar = await self._session.scalar(
                select(ProviderCalendarModel).where(
                    ProviderCalendarModel.user_id == user_id,
                    ProviderCalendarModel.connection_id == merged_calendar_connection_id,
                    ProviderCalendarModel.provider_calendar_id == merged_calendar_id,
                    ProviderCalendarModel.can_write.is_(True),
                )
            )
            if calendar is None:
                raise _connection_capability_disabled()

        persisted_values = dict(values)
        if "working_hours" in persisted_values:
            patch = persisted_values["working_hours"]
            if not isinstance(patch, Mapping):
                raise TypeError("working hours patch must be a mapping")
            # 用户行已经锁定；先验证数据库中的七天基线，再合并部分 PATCH 并重新通过
            # 完整值对象，保证响应和 JSONB 永远覆盖七天且不会丢失未提交日期。
            merged_working_hours = WeeklyWorkingHours.from_mapping(user.working_hours).to_mapping()
            merged_working_hours.update(patch)
            persisted_values["working_hours"] = WeeklyWorkingHours.from_mapping(
                merged_working_hours
            ).to_mapping()

        for key, value in persisted_values.items():
            setattr(user, key, value)
        self._session.add(
            AuditEventModel(
                user_id=user_id,
                task_id=None,
                event_type="settings.updated",
                actor_type="user",
                actor_id=str(user_id),
                event_metadata={"changed_fields": sorted(persisted_values)},
            )
        )
        await self._session.flush()
        return _settings_view(user)

    async def _require_enabled_capability(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capability: ConnectionCapability,
    ) -> None:
        """要求精确用户连接处于 connected 且指定能力 enabled。"""
        row = (
            await self._session.execute(
                select(OAuthConnectionModel.status, ConnectionCapabilityModel.status)
                .join(
                    ConnectionCapabilityModel,
                    (ConnectionCapabilityModel.connection_id == OAuthConnectionModel.id)
                    & (ConnectionCapabilityModel.user_id == OAuthConnectionModel.user_id),
                )
                .where(
                    OAuthConnectionModel.id == connection_id,
                    OAuthConnectionModel.user_id == user_id,
                    ConnectionCapabilityModel.capability == capability.value,
                )
            )
        ).one_or_none()
        if (
            row is None
            or row[0] != ConnectionStatus.CONNECTED.value
            or row[1] != CapabilityStatus.ENABLED.value
        ):
            raise _connection_capability_disabled()


def _settings_view(user: UserModel) -> UserSettingsView:
    """显式投影允许公开的设置字段，防止 ORM 新列意外进入 API。"""
    return UserSettingsView(
        timezone=user.timezone,
        locale=user.locale,
        brief_time=user.brief_time,
        email_body_retention_days=user.email_body_retention_days,
        source_metadata_retention_days=user.source_metadata_retention_days,
        workspace_history_retention_days=user.workspace_history_retention_days,
        default_mail_connection_id=user.default_mail_connection_id,
        default_calendar_connection_id=user.default_calendar_connection_id,
        default_calendar_id=user.default_calendar_id,
        working_hours={
            day: [list(interval) for interval in intervals]
            for day, intervals in user.working_hours.items()
        },
        meeting_buffer_minutes=user.meeting_buffer_minutes,
        updated_at=user.updated_at,
    )


def _connection_capability_disabled() -> StateConflictError:
    """构造默认连接或目录不可用时的稳定 409，不泄露其他用户资源。"""
    return StateConflictError(
        error_code="connection_capability_disabled",
        message="selected default connection capability is disabled",
    )


__all__ = ["SqlAlchemySettingsRepository"]
