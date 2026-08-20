"""定义用户设置的类型化读取、校验与事务更新用例。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from typing import Protocol
from uuid import UUID

from ai_employee.domain.settings import (
    WEEKDAY_NAMES,
    WeeklyWorkingHours,
    validate_brief_time,
    validate_locale,
    validate_meeting_buffer,
    validate_retention,
    validate_timezone,
)


@dataclass(frozen=True, slots=True)
class UserSettingsView:
    """表示 API 可公开的完整设置投影，不含身份验证或凭据字段。"""

    timezone: str
    locale: str
    brief_time: time
    email_body_retention_days: int
    source_metadata_retention_days: int
    workspace_history_retention_days: int
    default_mail_connection_id: UUID | None
    default_calendar_connection_id: UUID | None
    default_calendar_id: str | None
    working_hours: dict[str, list[list[str]]]
    meeting_buffer_minutes: int
    updated_at: datetime


class SettingsRepository(Protocol):
    """定义一个短事务中读取和更新用户设置的持久化端口。"""

    async def get(self, *, user_id: UUID) -> UserSettingsView | None: ...

    async def update(
        self,
        *,
        user_id: UUID,
        values: Mapping[str, object],
    ) -> UserSettingsView | None: ...


class UserSettingsNotFoundError(Exception):
    """表示当前认证用户记录异常消失；API 不公开数据库细节。"""


class UpdateUserSettings:
    """读取或原子更新用户设置，并集中复用领域值校验。"""

    def __init__(self, repository: SettingsRepository) -> None:
        """绑定当前请求短事务内的用户设置仓储。"""
        self._repository = repository

    async def get(self, *, user_id: UUID) -> UserSettingsView:
        """读取完整规范设置；找不到认证用户时失败关闭。"""
        value = await self._repository.get(user_id=user_id)
        if value is None:
            raise UserSettingsNotFoundError
        return value

    async def update(
        self,
        *,
        user_id: UUID,
        values: Mapping[str, object],
    ) -> UserSettingsView:
        """验证 PATCH 字段后原子写入设置、能力绑定和审计事实。"""
        validated = self.validate(values)
        if not validated:
            return await self.get(user_id=user_id)
        value = await self._repository.update(user_id=user_id, values=validated)
        if value is None:
            raise UserSettingsNotFoundError
        return value

    @staticmethod
    def validate(values: Mapping[str, object]) -> dict[str, object]:
        """验证并规范化 PATCH 字段；未知字段由严格 API Schema 先行拒绝。"""
        validated = dict(values)
        if "timezone" in validated:
            raw_timezone = validated["timezone"]
            if type(raw_timezone) is not str:
                raise TypeError("timezone must be a string")
            validated["timezone"] = validate_timezone(raw_timezone)
        if "locale" in validated:
            raw_locale = validated["locale"]
            if type(raw_locale) is not str:
                raise TypeError("locale must be a string")
            validated["locale"] = validate_locale(raw_locale)
        if "brief_time" in validated:
            raw_brief_time = validated["brief_time"]
            if not isinstance(raw_brief_time, str):
                raise ValueError("brief_time must be HH:MM")
            validated["brief_time"] = validate_brief_time(raw_brief_time)
        for name in (
            "email_body_retention_days",
            "source_metadata_retention_days",
            "workspace_history_retention_days",
        ):
            if name in validated:
                raw_value = validated[name]
                if type(raw_value) is not int:
                    raise ValueError(f"{name} must be an integer")
                validated[name] = validate_retention(raw_value)
        if "working_hours" in validated:
            validated["working_hours"] = validate_working_hours_patch(validated["working_hours"])
        if "meeting_buffer_minutes" in validated:
            validated["meeting_buffer_minutes"] = validate_meeting_buffer(
                validated["meeting_buffer_minutes"]
            )
        return validated


def validate_working_hours_patch(value: object) -> dict[str, list[list[str]]]:
    """验证并规范化任意已知星期子集，不在应用边界猜测未提交日的值。

    API PATCH 允许只修改周一等少数日期；这里以空区间补齐未提交日期，仅借用完整
    ``WeeklyWorkingHours`` 值对象验证键名、时间格式、排序和日内不重叠，然后只返回
    原请求包含的日期。真正的七天合并必须在锁定用户行的仓储事务内完成，避免并发
    PATCH 基于陈旧读取覆盖彼此。

    Args:
        value: 请求中的 weekday 到区间列表映射，可只包含一到七个已知星期名。

    Returns:
        只含请求星期、且区间已确定性排序的规范映射。

    Raises:
        TypeError: 根映射或内部容器类型错误。
        ValueError: 出现未知星期、空 PATCH、坏时间或重叠区间。
    """
    if not isinstance(value, Mapping):
        raise TypeError("working hours patch must be a mapping")
    submitted_days = tuple(value)
    if not submitted_days:
        raise ValueError("working hours patch must contain at least one weekday")
    complete: dict[object, object] = {day: [] for day in WEEKDAY_NAMES}
    complete.update(value)
    normalized = WeeklyWorkingHours.from_mapping(complete).to_mapping()
    return {day: normalized[day] for day in WEEKDAY_NAMES if day in value}


__all__ = [
    "SettingsRepository",
    "UpdateUserSettings",
    "UserSettingsNotFoundError",
    "UserSettingsView",
    "validate_working_hours_patch",
]
