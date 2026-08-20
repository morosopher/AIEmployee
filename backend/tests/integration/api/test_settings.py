"""验证用户设置 API 的校验、CSRF 与调度读取闭环。"""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.infrastructure.db.database_url import (
    TestDatabaseUrl as ValidatedTestDatabaseUrl,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
)
from ai_employee.infrastructure.db.repositories.identity import SqlAlchemyActiveUserScheduleReader
from ai_employee.infrastructure.db.session import build_session_factory  # noqa: F401

from .conftest import AuthenticatedApiClients

pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """把设置 API 模块绑定到受生命周期保护的 Cycle 5 regular head。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """复用 regular fixture 已验证的迁移，禁止聚焦测试触碰 0018 anchor。"""
    del cycle5_regular_database_url
    yield


async def _seed_settings_connection(
    clients: AuthenticatedApiClients,
    *,
    user_id: UUID | None = None,
    capability_statuses: dict[ConnectionCapability, CapabilityStatus] | None = None,
    calendar_id: str | None = None,
    calendar_can_write: bool = True,
    connection_status: str = "connected",
) -> UUID:
    """写入不含凭据的合成连接、能力及可选日历目录，供默认值边界测试使用。"""
    owner_id = user_id or clients.owner_id
    connection_id = uuid4()
    statuses = capability_statuses or {}
    async with clients.session_factory.begin() as session:
        session.add(
            OAuthConnectionModel(
                id=connection_id,
                user_id=owner_id,
                provider="google",
                provider_account_id=f"task17-settings-{connection_id}",
                provider_tenant_id="",
                account_type="google",
                account_email="settings-connection@example.test",
                scopes=[],
                status=connection_status,
            )
        )
        # 先固定父连接，避免无 relationship 的 ORM 对象在 autoflush 时以不稳定顺序写入。
        await session.flush()
        session.add_all(
            ConnectionCapabilityModel(
                user_id=owner_id,
                connection_id=connection_id,
                capability=capability.value,
                status=status.value,
                actual_scopes=[f"synthetic:{capability.value}"],
                last_verified_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
            for capability, status in statuses.items()
        )
        if calendar_id is not None:
            session.add(
                ProviderCalendarModel(
                    user_id=owner_id,
                    connection_id=connection_id,
                    provider_calendar_id=calendar_id,
                    name="Synthetic settings calendar",
                    timezone="UTC",
                    is_primary=True,
                    access_role="owner" if calendar_can_write else "reader",
                    can_write=calendar_can_write,
                    provider_url="https://calendar.example.test/settings",
                )
            )
    return connection_id


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
    assert (
        updated.json()["source_metadata_retention_days"]
        == before.json()["source_metadata_retention_days"]
    )
    schedules = await SqlAlchemyActiveUserScheduleReader(clients.session_factory).list_active()
    owner_schedule = next(
        schedule for schedule in schedules if schedule.user_id == clients.owner_id
    )
    assert owner_schedule.timezone == "America/New_York"
    assert owner_schedule.brief_time.isoformat() == "09:45:00"


@pytest.mark.asyncio
async def test_partial_working_hours_patch_merges_to_normalized_seven_day_response(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """单日 PATCH 与现有七天基线合并，区间排序后连同缓冲一并持久化。"""
    clients = authenticated_api_clients
    response = await clients.owner.patch(
        "/api/v1/settings",
        headers={"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""},
        json={
            "working_hours": {"monday": [["13:00", "18:00"], ["09:00", "12:00"]]},
            "meeting_buffer_minutes": 15,
        },
    )

    expected = {
        "monday": [["09:00", "12:00"], ["13:00", "18:00"]],
        "tuesday": [["09:00", "18:00"]],
        "wednesday": [["09:00", "18:00"]],
        "thursday": [["09:00", "18:00"]],
        "friday": [["09:00", "18:00"]],
        "saturday": [],
        "sunday": [],
    }
    assert response.status_code == 200
    assert response.json()["working_hours"] == expected
    assert response.json()["meeting_buffer_minutes"] == 15
    async with clients.session_factory() as session:
        user = await session.scalar(select(UserModel).where(UserModel.id == clients.owner_id))
    assert user is not None
    assert user.working_hours == expected
    assert user.meeting_buffer_minutes == 15


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "working_hours",
    (
        {"monday": [["09:00", "12:00"], ["11:00", "18:00"]]},
        {"funday": [["09:00", "18:00"]]},
    ),
    ids=("overlap", "unknown-weekday"),
)
async def test_partial_working_hours_patch_rejects_invalid_days_or_intervals(
    authenticated_api_clients: AuthenticatedApiClients,
    working_hours: dict[str, list[list[str]]],
) -> None:
    """部分工作时间仍严格拒绝重叠区间与未知星期名，且不回显原始输入。"""
    clients = authenticated_api_clients
    response = await clients.owner.patch(
        "/api/v1/settings",
        headers={"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""},
        json={"working_hours": working_hours},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "request_validation_failed"


@pytest.mark.asyncio
async def test_patch_persists_enabled_owned_default_connections_and_writable_calendar(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """邮件与日历默认值只接受当前用户 enabled 能力和精确可写目录。"""
    clients = authenticated_api_clients
    mail_connection_id = await _seed_settings_connection(
        clients,
        capability_statuses={
            ConnectionCapability.MAIL_SEND: CapabilityStatus.ENABLED,
        },
    )
    calendar_connection_id = await _seed_settings_connection(
        clients,
        capability_statuses={
            ConnectionCapability.CALENDAR_WRITE: CapabilityStatus.ENABLED,
        },
        calendar_id="settings-primary",
    )
    payload = {
        "default_mail_connection_id": str(mail_connection_id),
        "default_calendar_connection_id": str(calendar_connection_id),
        "default_calendar_id": "settings-primary",
    }

    response = await clients.owner.patch(
        "/api/v1/settings",
        headers={"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""},
        json=payload,
    )

    assert response.status_code == 200
    assert {
        "default_mail_connection_id": response.json()["default_mail_connection_id"],
        "default_calendar_connection_id": response.json()["default_calendar_connection_id"],
        "default_calendar_id": response.json()["default_calendar_id"],
    } == payload
    async with clients.session_factory() as session:
        user = await session.scalar(select(UserModel).where(UserModel.id == clients.owner_id))
    assert user is not None
    assert user.default_mail_connection_id == mail_connection_id
    assert user.default_calendar_connection_id == calendar_connection_id
    assert user.default_calendar_id == "settings-primary"


@pytest.mark.asyncio
async def test_patch_accepts_explicit_null_to_clear_default_connections(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """显式 null 清空默认身份；省略字段仍保持 PATCH 的不变语义。"""
    clients = authenticated_api_clients
    mail_connection_id = await _seed_settings_connection(
        clients,
        capability_statuses={
            ConnectionCapability.MAIL_SEND: CapabilityStatus.ENABLED,
        },
    )
    calendar_connection_id = await _seed_settings_connection(
        clients,
        capability_statuses={
            ConnectionCapability.CALENDAR_WRITE: CapabilityStatus.ENABLED,
        },
        calendar_id="settings-clearable",
    )
    headers = {"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""}
    seeded = await clients.owner.patch(
        "/api/v1/settings",
        headers=headers,
        json={
            "default_mail_connection_id": str(mail_connection_id),
            "default_calendar_connection_id": str(calendar_connection_id),
            "default_calendar_id": "settings-clearable",
        },
    )
    assert seeded.status_code == 200

    cleared = await clients.owner.patch(
        "/api/v1/settings",
        headers=headers,
        json={
            "default_mail_connection_id": None,
            "default_calendar_connection_id": None,
            "default_calendar_id": None,
        },
    )

    assert cleared.status_code == 200
    assert cleared.json()["default_mail_connection_id"] is None
    assert cleared.json()["default_calendar_connection_id"] is None
    assert cleared.json()["default_calendar_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_default",
    (
        "mail-capability-disabled",
        "calendar-capability-missing",
        "calendar-read-only",
        "cross-user-mail",
        "calendar-connection-disconnected",
    ),
)
async def test_patch_rejects_unavailable_default_connection_with_stable_conflict(
    authenticated_api_clients: AuthenticatedApiClients,
    invalid_default: str,
) -> None:
    """归属、连接状态、enabled 能力或目录写权限缺一时均返回稳定 409。"""
    clients = authenticated_api_clients
    if invalid_default == "mail-capability-disabled":
        connection_id = await _seed_settings_connection(
            clients,
            capability_statuses={
                ConnectionCapability.MAIL_SEND: CapabilityStatus.DISABLED,
            },
        )
        payload: dict[str, object] = {"default_mail_connection_id": str(connection_id)}
    elif invalid_default == "calendar-capability-missing":
        connection_id = await _seed_settings_connection(
            clients,
            calendar_id="settings-missing-capability",
        )
        payload = {
            "default_calendar_connection_id": str(connection_id),
            "default_calendar_id": "settings-missing-capability",
        }
    elif invalid_default == "calendar-read-only":
        connection_id = await _seed_settings_connection(
            clients,
            capability_statuses={
                ConnectionCapability.CALENDAR_WRITE: CapabilityStatus.ENABLED,
            },
            calendar_id="settings-read-only",
            calendar_can_write=False,
        )
        payload = {
            "default_calendar_connection_id": str(connection_id),
            "default_calendar_id": "settings-read-only",
        }
    elif invalid_default == "cross-user-mail":
        connection_id = await _seed_settings_connection(
            clients,
            user_id=clients.other_id,
            capability_statuses={
                ConnectionCapability.MAIL_SEND: CapabilityStatus.ENABLED,
            },
        )
        payload = {"default_mail_connection_id": str(connection_id)}
    elif invalid_default == "calendar-connection-disconnected":
        connection_id = await _seed_settings_connection(
            clients,
            capability_statuses={
                ConnectionCapability.CALENDAR_WRITE: CapabilityStatus.ENABLED,
            },
            calendar_id="settings-disconnected",
            connection_status="disconnected",
        )
        payload = {
            "default_calendar_connection_id": str(connection_id),
            "default_calendar_id": "settings-disconnected",
        }
    else:  # pragma: no cover - 参数集合由本测试静态冻结。
        raise AssertionError("unknown invalid settings default variant")

    response = await clients.owner.patch(
        "/api/v1/settings",
        headers={"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""},
        json=payload,
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "connection_capability_disabled"
    async with clients.session_factory() as session:
        user = await session.scalar(select(UserModel).where(UserModel.id == clients.owner_id))
    assert user is not None
    assert user.default_mail_connection_id is None
    assert user.default_calendar_connection_id is None
    assert user.default_calendar_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {"timezone": "not/an-iana-zone"},
        {"locale": "invalid locale!"},
        {"brief_time": "9:45"},
        {"brief_time": "24:00"},
        {"email_body_retention_days": 0},
        {"email_body_retention_days": True},
        {"source_metadata_retention_days": 3651},
        {"source_metadata_retention_days": True},
        {"workspace_history_retention_days": 0},
        {"workspace_history_retention_days": True},
        {"meeting_buffer_minutes": True},
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
