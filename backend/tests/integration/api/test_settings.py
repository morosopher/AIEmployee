"""验证用户设置 API 的校验、CSRF 与调度读取闭环。"""

import pytest
from conftest import AuthenticatedApiClients

from ai_employee.infrastructure.db.repositories.identity import SqlAlchemyActiveUserScheduleReader


@pytest.mark.asyncio
async def test_get_and_partial_patch_require_csrf_and_are_persisted(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """GET 返回当前值；PATCH 只更新提交字段并写入可供后续扫描读取的持久值。"""
    clients = authenticated_api_clients
    before = await clients.owner.get("/api/v1/settings")
    denied = await clients.owner.patch("/api/v1/settings", json={"timezone": "UTC"})
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    updated = await clients.owner.patch(
        "/api/v1/settings",
        headers={"X-CSRF-Token": csrf},
        json={
            "timezone": "America/New_York",
            "locale": "en-us",
            "brief_time": "09:45",
            "email_body_retention_days": 45,
        },
    )
    assert before.status_code == 200
    assert denied.status_code == 403 and denied.json()["error_code"] == "csrf_rejected"
    assert updated.status_code == 200
    assert updated.json()["timezone"] == "America/New_York"
    assert updated.json()["locale"] == "en-US"
    assert updated.json()["brief_time"] == "09:45:00"
    assert updated.json()["email_body_retention_days"] == 45
    assert updated.json()["source_metadata_retention_days"] == before.json()["source_metadata_retention_days"]
    schedules = await SqlAlchemyActiveUserScheduleReader(clients.session_factory).list_active()
    owner_schedule = next(schedule for schedule in schedules if schedule.user_id == clients.owner_id)
    assert owner_schedule.timezone == "America/New_York"
    assert owner_schedule.brief_time.isoformat() == "09:45:00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {"timezone": "not/an-iana-zone"},
        {"locale": "invalid locale!"},
        {"brief_time": "9:45"},
        {"brief_time": "24:00"},
        {"email_body_retention_days": 0},
        {"source_metadata_retention_days": 3651},
        {"workspace_history_retention_days": 0},
        {"unexpected": "field"},
    ),
)
async def test_patch_rejects_invalid_or_unknown_settings_fields(
    authenticated_api_clients: AuthenticatedApiClients, payload: dict[str, object]
) -> None:
    """格式、范围、未知字段均在 API 边界返回脱敏的 422 Problem Details。"""
    clients = authenticated_api_clients
    response = await clients.owner.patch(
        "/api/v1/settings",
        headers={"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""},
        json=payload,
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "request_validation_failed"
