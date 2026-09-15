"""暴露完整用户工作设置，并以 CSRF 保护所有修改。"""

from datetime import datetime, time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pydantic.json_schema import SkipJsonSchema

from ai_employee.api.deps import (
    CsrfProtectedSession,
    CurrentSession,
    get_settings_use_case,
)
from ai_employee.application.use_cases.settings import (
    UpdateUserSettings,
    UserSettingsView,
    validate_working_hours_patch,
)
from ai_employee.domain.settings import (
    validate_brief_time,
    validate_locale,
    validate_meeting_buffer,
    validate_retention,
    validate_timezone,
)


class SettingsPatch(BaseModel):
    """允许部分更新 M1 保留设置与 M2 默认连接、工作时间和会议缓冲。

    非 nullable 字段仍以 ``None`` 表示 PATCH 中的内部 omission 默认值，但通过
    ``SkipJsonSchema`` 从公开契约排除 null；显式 JSON null 则由前置 validator 拒绝。
    三个默认连接/日历字段保留普通 ``T | None``，因此契约与运行时都允许清空。
    会议缓冲的数值边界绑定到 ``StrictInt`` 分支，避免联合外层约束泄漏非标准 schema 键。
    """

    model_config = ConfigDict(extra="forbid")

    timezone: str | SkipJsonSchema[None] = None
    locale: str | SkipJsonSchema[None] = None
    brief_time: str | SkipJsonSchema[None] = None
    email_body_retention_days: StrictInt | SkipJsonSchema[None] = None
    source_metadata_retention_days: StrictInt | SkipJsonSchema[None] = None
    workspace_history_retention_days: StrictInt | SkipJsonSchema[None] = None
    default_mail_connection_id: UUID | None = None
    default_calendar_connection_id: UUID | None = None
    default_calendar_id: str | None = Field(default=None, min_length=1, max_length=512)
    working_hours: dict[str, list[list[str]]] | SkipJsonSchema[None] = None
    meeting_buffer_minutes: Annotated[StrictInt, Field(ge=0, le=120)] | SkipJsonSchema[None] = None

    @field_validator(
        "timezone",
        "locale",
        "brief_time",
        "email_body_retention_days",
        "source_metadata_retention_days",
        "workspace_history_retention_days",
        "working_hours",
        "meeting_buffer_minutes",
        mode="before",
    )
    @classmethod
    def non_nullable_setting_present(cls, value: object) -> object:
        """拒绝已提供字段的 JSON null，同时让真正省略的 PATCH 字段保持不更新。

        Pydantic 默认不会为未提供字段执行本 validator，因此模型仍可用 ``None`` 作为
        内部 omission 默认值；显式 JSON null 则在路由调用 application 前稳定映射为 422。
        三个允许清空的默认连接/日历字段未列入本校验器。
        """
        if value is None:
            raise ValueError("setting must not be null")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value: str | None) -> str | None:
        """验证显式 IANA 时区；字段省略时不会进入本 validator。"""
        return validate_timezone(value) if value is not None else value

    @field_validator("locale")
    @classmethod
    def locale_valid(cls, value: str | None) -> str | None:
        """规范 BCP47 风格 locale。"""
        return validate_locale(value) if value is not None else value

    @field_validator("brief_time")
    @classmethod
    def time_valid(cls, value: str | None) -> str | None:
        """只接受零填充 HH:MM；持久层会保存为墙上 ``time``。"""
        if value is not None:
            validate_brief_time(value)
        return value

    @field_validator(
        "email_body_retention_days",
        "source_metadata_retention_days",
        "workspace_history_retention_days",
    )
    @classmethod
    def retention_valid(cls, value: int | None) -> int | None:
        """限制三类保留期并拒绝 bool 等整数子类。"""
        if value is not None:
            if type(value) is not int:
                raise ValueError("retention must be an integer")
            return validate_retention(value)
        return value

    @field_validator("working_hours")
    @classmethod
    def working_hours_valid(
        cls,
        value: dict[str, list[list[str]]] | None,
    ) -> dict[str, list[list[str]]] | None:
        """允许任意已知星期子集，并规范化每个提交日内的不重叠区间。"""
        if value is None:
            return None
        return validate_working_hours_patch(value)

    @field_validator("meeting_buffer_minutes")
    @classmethod
    def meeting_buffer_valid(cls, value: int | None) -> int | None:
        """限制会议前后共同使用的缓冲为 0～120 分钟。"""
        return validate_meeting_buffer(value) if value is not None else value


class SettingsResponse(BaseModel):
    """设置的完整公开投影，工作时间固定覆盖星期一到星期日。"""

    model_config = ConfigDict(extra="forbid")

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


def _settings_response(value: UserSettingsView) -> SettingsResponse:
    """显式映射应用 DTO，防止未来设置字段未经审查自动公开。"""
    return SettingsResponse.model_validate(value, from_attributes=True)


def build_settings_router() -> APIRouter:
    """构建 settings GET/PATCH 路由。"""
    router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

    @router.get("", response_model=SettingsResponse)
    async def get_settings(
        authenticated: CurrentSession,
        use_case: Annotated[UpdateUserSettings, Depends(get_settings_use_case, scope="function")],
    ) -> SettingsResponse:
        """返回当前用户已持久化且规范化的完整工作设置。"""
        return _settings_response(await use_case.get(user_id=authenticated.user.id))

    @router.patch("", response_model=SettingsResponse)
    async def patch_settings(
        payload: SettingsPatch,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[UpdateUserSettings, Depends(get_settings_use_case, scope="function")],
    ) -> SettingsResponse:
        """原子更新提供字段，并写入只包含字段名的设置审计事件。"""
        value = await use_case.update(
            user_id=authenticated.user.id,
            values=payload.model_dump(exclude_unset=True),
        )
        return _settings_response(value)

    return router


__all__ = ["SettingsPatch", "SettingsResponse", "build_settings_router"]
