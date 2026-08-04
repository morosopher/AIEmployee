"""暴露用户简报偏好设置，并以 CSRF 保护所有修改。"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_employee.api.deps import CsrfProtectedSession, CurrentSession
from ai_employee.domain.settings import (
    validate_brief_time,
    validate_locale,
    validate_retention,
    validate_timezone,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel


class SettingsPatch(BaseModel):
    """只允许本任务定义的六项用户偏好进行部分更新。"""
    model_config = ConfigDict(extra="forbid")
    timezone: str | None = None
    locale: str | None = None
    brief_time: str | None = None
    email_body_retention_days: int | None = Field(default=None)
    source_metadata_retention_days: int | None = Field(default=None)
    workspace_history_retention_days: int | None = Field(default=None)
    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value: str | None) -> str | None: return validate_timezone(value) if value is not None else value
    @field_validator("locale")
    @classmethod
    def locale_valid(cls, value: str | None) -> str | None: return validate_locale(value) if value is not None else value
    @field_validator("brief_time")
    @classmethod
    def time_valid(cls, value: str | None) -> str | None: validate_brief_time(value) if value is not None else None; return value
    @field_validator("email_body_retention_days", "source_metadata_retention_days", "workspace_history_retention_days")
    @classmethod
    def retention_valid(cls, value: int | None) -> int | None: return validate_retention(value) if value is not None else value


class SettingsResponse(BaseModel):
    """设置的公开投影。"""
    timezone: str
    locale: str
    brief_time: object
    email_body_retention_days: int
    source_metadata_retention_days: int
    workspace_history_retention_days: int
    updated_at: object


def build_settings_router() -> APIRouter:
    """构建 settings GET/PATCH 路由。"""
    router = APIRouter(prefix="/api/v1/settings", tags=["settings"])
    async def load(request: Request, user_id: object) -> UserModel:
        async with request.app.state.auth_session_factory() as session:
            value = await session.get(UserModel, user_id)
            if value is None: raise RuntimeError("authenticated user disappeared")
            return value
    @router.get("", response_model=SettingsResponse)
    async def get_settings(authenticated: CurrentSession, request: Request) -> SettingsResponse:
        """返回当前用户已持久化偏好。"""
        return SettingsResponse.model_validate(await load(request, authenticated.user.id), from_attributes=True)
    @router.patch("", response_model=SettingsResponse)
    async def patch_settings(payload: SettingsPatch, authenticated: CsrfProtectedSession, request: Request) -> SettingsResponse:
        """原子更新所提供字段，并写入不含值的设置审计事件。"""
        values = payload.model_dump(exclude_none=True)
        if not values: return SettingsResponse.model_validate(await load(request, authenticated.user.id), from_attributes=True)
        if "brief_time" in values: values["brief_time"] = validate_brief_time(values["brief_time"])
        async with request.app.state.auth_session_factory.begin() as session:
            user = await session.get(UserModel, authenticated.user.id, with_for_update=True)
            if user is None: raise RuntimeError("authenticated user disappeared")
            for key, value in values.items(): setattr(user, key, value)
            session.add(AuditEventModel(user_id=authenticated.user.id, task_id=None, event_type="settings.updated", actor_type="user", actor_id=str(authenticated.user.id), event_metadata={"changed_fields": sorted(values)}))
            await session.flush()
            result = SettingsResponse.model_validate(user, from_attributes=True)
        return result
    return router
