"""从真实 OAuth callback 到生产读取/写入组合验证 Microsoft 两类账户的合成合同。

所有供应商网络都由 respx 拦截；账户类型从真实验签的 ID token 与 `/me` 响应派生，
随后从同一持久连接解密凭据。不能只替换 fixture 标签、邮箱或在 fake adapter 上计数。
"""

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import jwt
import pytest
import respx
from sqlalchemy import select

from ai_employee.application.use_cases.sync_calendar import SyncCalendarUseCase
from ai_employee.application.use_cases.sync_mail import SyncMailUseCase
from ai_employee.config import get_settings
from ai_employee.domain.actions import ProviderWriteOutcomeKind
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    EmailMessageModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyMailSyncRepository
from ai_employee.integrations.microsoft.calendar import MicrosoftCalendarAdapter
from ai_employee.integrations.microsoft.calendar_write import MicrosoftCalendarWriteAdapter
from ai_employee.integrations.microsoft.mail import MicrosoftMailAdapter
from ai_employee.integrations.microsoft.mail_write import (
    MICROSOFT_SEND_MAIL_URL,
    MICROSOFT_SENT_MESSAGES_URL,
    MicrosoftMailWriteAdapter,
)
from ai_employee.integrations.microsoft.oauth import (
    MICROSOFT_DISCOVERY_URL,
    MICROSOFT_GRAPH_ME_URL,
    MICROSOFT_JWKS_URL,
    MICROSOFT_TOKEN_URL,
)
from ai_employee.integrations.registry import (
    ProviderAdapterRegistry,
    build_oauth_security_services,
    build_trusted_action_registry,
)
from tests.contract.microsoft.account_cases import MICROSOFT_ACCOUNT_CASES, MicrosoftAccountCase
from tests.contract.microsoft.test_calendar_write_adapter import (
    _create,
    _current_event,
    _restore,
    _update,
)
from tests.contract.microsoft.test_calendar_write_adapter import (
    _execution as _calendar_execution,
)
from tests.contract.microsoft.test_calendar_write_adapter import (
    _fixture as _calendar_response,
)
from tests.contract.microsoft.test_mail_write_adapter import (
    _command as _mail_command,
)
from tests.contract.microsoft.test_mail_write_adapter import (
    _execution as _mail_execution,
)
from tests.contract.microsoft.test_mail_write_adapter import (
    _sent_payload,
)
from tests.integration.microsoft.test_mail_sync import INITIAL_PARAMS, _FixedClock
from tests.integration.microsoft.test_oauth_flow import MicrosoftOAuthContext, _signing_material
from tests.integration.microsoft.test_oauth_flow import (
    microsoft_oauth_context as microsoft_oauth_context,  # noqa: PLC0414 - 复用真实 OAuth/API fixture。
)

FIXTURES = Path(__file__).parents[2] / "contract" / "microsoft" / "fixtures"
NOW = datetime(2030, 1, 8, 12, tzinfo=UTC)
GRAPH = "https://graph.microsoft.com/v1.0"
MAIL_DELTA = f"{GRAPH}/me/mailFolders/synthetic-folder-inbox/messages/delta"
MAIL_CURSOR = f"{MAIL_DELTA}?$deltatoken=synthetic-delta-2"
CALENDAR_ID = "calendar-primary"
CALENDAR_DELTA = f"{GRAPH}/me/calendars/{CALENDAR_ID}/calendarView/delta"
CALENDAR_CURSOR = f"{CALENDAR_DELTA}?$deltatoken=synthetic-account-mode"


def _fixture(name: str) -> dict[str, object]:
    """复用固定脱敏供应商响应，保持分页路径与已有合同一致。"""
    value = json.loads((FIXTURES / name).read_text())
    assert isinstance(value, dict)
    return value


async def _authorize(
    context: MicrosoftOAuthContext,
    account: MicrosoftAccountCase,
    endpoint: str,
) -> None:
    """经真实 start/consume、RS256 验签与 Graph `/me` 完成一次最小渐进授权。"""
    csrf = context.client.cookies.get("ai_employee_csrf")
    assert csrf is not None
    started = await context.client.post(endpoint, headers={"X-CSRF-Token": csrf})
    assert started.status_code == 200
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    private_key, jwk = _signing_material()
    id_token = jwt.encode(
        {
            "iss": f"https://login.microsoftonline.com/{account.tenant}/v2.0",
            "aud": "synthetic-microsoft-client",
            "tid": account.tenant,
            "nonce": query["nonce"][0],
            "exp": 1893456000,
            "iat": 1780000000,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "integration-runtime-key"},
    )
    with respx.mock(assert_all_called=True) as mocked:
        mocked.post(MICROSOFT_TOKEN_URL).respond(
            200,
            json={
                "access_token": account.access_token,
                "refresh_token": "synthetic-contract-refresh",
                "expires_in": 3600,
                "scope": query["scope"][0],
                "id_token": id_token,
            },
        )
        mocked.get(MICROSOFT_DISCOVERY_URL).respond(200, json=_fixture("openid_configuration.json"))
        mocked.get(MICROSOFT_JWKS_URL).respond(200, json={"keys": [jwk]})
        me = mocked.get(MICROSOFT_GRAPH_ME_URL).respond(
            200,
            json={
                "id": account.graph_id,
                "mail": "sender@example.test",
                "userPrincipalName": "sender@example.test",
            },
        )
        completed = await context.client.get(
            "/api/v1/connections/microsoft/callback",
            params={"code": "synthetic-contract-code", "state": query["state"][0]},
        )
        assert completed.status_code == 200
        assert me.calls[0].request.headers["Authorization"] == f"Bearer {account.access_token}"


@pytest.mark.asyncio
@pytest.mark.parametrize("account", MICROSOFT_ACCOUNT_CASES, ids=lambda case: case.account_type)
async def test_account_mode_persists_identity_and_reaches_real_provider_contracts(
    microsoft_oauth_context: MicrosoftOAuthContext,
    account: MicrosoftAccountCase,
) -> None:
    """两类账户都从同一持久连接完成增量读取、四种写入及只读结果核对。

    先用实际渐进 OAuth 开启四项能力，再以相同连接加密凭据组成真实 reader/production
    registry。断言落库身份、来源归属、MIME From、Graph路径、If-Match与零重放，证明
    personal/work_school 差异贯穿执行组合而非只停留在 normalize 单测。
    """
    context = microsoft_oauth_context
    login = await context.client.post(
        "/api/v1/auth/login",
        json={"email": "microsoft-owner@example.test", "password": "synthetic-password"},
    )
    assert login.status_code == 200
    await _authorize(context, account, "/api/v1/connections/microsoft/start")
    async with context.queries() as session:
        connection = await session.scalar(select(OAuthConnectionModel))
        assert connection is not None
        # ORM 的 asyncpg UUID 是驱动子类；正式命令入口从 JSON 字符串解析为标准 UUID。
        # 测试直接构造领域命令也遵守该边界，不能把 ORM 对象传给严格 adapter。
        user_id, connection_id = UUID(str(connection.user_id)), UUID(str(connection.id))
    for capability in ("mail.send", "calendar.write"):
        await _authorize(
            context,
            account,
            f"/api/v1/connections/{connection_id}/capabilities/{capability}/enable",
        )
    async with context.queries() as session:
        connection = await session.scalar(select(OAuthConnectionModel))
        assert connection is not None and connection.id == connection_id
        assert connection.account_type == account.account_type
        assert connection.provider_tenant_id == account.tenant
        assert connection.provider_account_id == account.provider_account_id
        assert connection.account_email == "sender@example.test"
        capabilities = (await session.scalars(select(ConnectionCapabilityModel))).all()
        assert {row.capability for row in capabilities if row.status == "enabled"} == {
            "mail.read",
            "mail.send",
            "calendar.read",
            "calendar.write",
        }
        credentials = await SqlAlchemyMailSyncRepository(session).get_credentials(
            user_id=user_id,
            connection_id=connection_id,
            expected_provider="microsoft",
            required_capability="mail.read",
        )
        assert credentials is not None
    settings = get_settings()
    security = build_oauth_security_services(
        session_factory=context.queries,
        master_key_file=settings.app_master_key_file,
    )
    access = security.cipher.decrypt(
        credentials.access_token,
        f"{user_id}:{connection_id}:access_token".encode("ascii"),
    ).decode("utf-8")
    assert access == account.access_token
    mail_reader = MicrosoftMailAdapter(access_token=access)
    calendar_reader = MicrosoftCalendarAdapter(
        access_token=access, user_timezone="UTC", now=lambda: NOW
    )
    readers = ProviderAdapterRegistry(
        microsoft_mail=mail_reader, microsoft_calendar=calendar_reader
    )

    @asynccontextmanager
    async def mail_stores() -> AsyncIterator[SqlAlchemyMailSyncRepository]:
        """保持每次网络前/后独立短事务，与正式同步用例相同。"""
        async with context.queries.begin() as session:
            yield SqlAlchemyMailSyncRepository(session)

    @asynccontextmanager
    async def calendar_stores() -> AsyncIterator[SqlAlchemyCalendarSyncRepository]:
        """事件、游标与审计由同一真实仓储事务落库。"""
        async with context.queries.begin() as session:
            yield SqlAlchemyCalendarSyncRepository(session)

    with respx.mock(assert_all_called=True) as mocked:
        mocked.get(f"{GRAPH}/me/mailFolders").respond(
            200,
            json={
                "value": [
                    {
                        "id": "synthetic-folder-inbox",
                        "displayName": "Inbox",
                    }
                ]
            },
        )
        # Graph v1.0 目录没有 wellKnownName；固定别名只读取 ID，测试不伪造资源属性。
        for alias, identifier in (
            ("drafts", "synthetic-folder-drafts"),
            ("deleteditems", "synthetic-folder-deleted"),
            ("junkemail", "synthetic-folder-junk"),
            ("sentitems", "synthetic-folder-sent"),
        ):
            mocked.get(f"{GRAPH}/me/mailFolders/{alias}", params={"$select": "id"}).respond(
                200, json={"id": identifier}
            )
        folders = await mail_reader.list_sync_scopes()
        assert [folder.scope_key for folder in folders] == ["synthetic-folder-inbox"]
        async with mail_stores() as repository:
            await repository.mark_directory_success(
                user_id=user_id,
                connection_id=connection_id,
                completed_at=NOW,
                folder_scope_keys=("synthetic-folder-inbox",),
            )
        mocked.get(MAIL_DELTA, params=INITIAL_PARAMS).respond(
            200, json=_fixture("mail_delta_initial.json")
        )
        mocked.get(f"{MAIL_DELTA}?$skiptoken=synthetic-next-1").respond(
            200, json=_fixture("mail_delta_incremental.json")
        )
        increment = mocked.get(MAIL_CURSOR).respond(
            200, json=_fixture("mail_delta_incremental.json")
        )
        mail_sync = SyncMailUseCase(mail_stores, readers, security.cipher, clock=_FixedClock(NOW))
        first_mail = await mail_sync.execute(
            user_id=user_id, connection_id=connection_id, scope_key="synthetic-folder-inbox"
        )
        next_mail = await mail_sync.execute(
            user_id=user_id, connection_id=connection_id, scope_key="synthetic-folder-inbox"
        )
        assert first_mail.used_full_resync and not next_mail.used_full_resync
        assert increment.call_count == 1

        mocked.get(f"{GRAPH}/me/calendars").respond(
            200,
            json={
                "value": [
                    {
                        "id": CALENDAR_ID,
                        "name": "Synthetic calendar",
                        "isDefaultCalendar": True,
                        "canEdit": True,
                        "canShare": False,
                        "timeZone": "UTC",
                    }
                ]
            },
        )
        event = _current_event()
        event["organizer"] = {"emailAddress": {"address": "sender@example.test"}}
        mocked.get(
            CALENDAR_DELTA,
            params={
                "startDateTime": "2030-01-07T00:00:00+00:00",
                "endDateTime": "2030-02-07T00:00:00+00:00",
            },
        ).respond(200, json={"value": [event], "@odata.deltaLink": CALENDAR_CURSOR})
        calendar_increment = mocked.get(CALENDAR_CURSOR).respond(
            200,
            json={
                "value": [
                    event,
                    {"id": "synthetic-removed-event", "@removed": {"reason": "deleted"}},
                ],
                "@odata.deltaLink": CALENDAR_CURSOR,
            },
        )
        calendar_sync = SyncCalendarUseCase(calendar_stores, readers, security.cipher)
        await calendar_sync.execute(
            user_id=user_id, connection_id=connection_id, scope_key="directory"
        )
        await calendar_sync.execute(
            user_id=user_id, connection_id=connection_id, scope_key=CALENDAR_ID
        )
        assert calendar_increment.call_count == 1
        async with context.queries() as session:
            messages = (await session.scalars(select(EmailMessageModel))).all()
            events = (await session.scalars(select(CalendarEventModel))).all()
            cursors = (await session.scalars(select(SyncCursorModel))).all()
            assert len(messages) == 2 and all(
                row.user_id == user_id
                and row.connection_id == connection_id
                and row.body_ciphertext
                for row in messages
            )
            assert any(
                row.provider_event_id == "event-1"
                and row.connection_id == connection_id
                and row.user_id == user_id
                for row in events
            )
            assert any(
                row.scope_key == CALENDAR_ID and row.cursor == CALENDAR_CURSOR for row in cursors
            )
        registry = build_trusted_action_registry(session_factory=context.queries, settings=settings)
        mail_command = replace(_mail_command(), connection_id=connection_id)
        mail_adapter = await registry.resolve_trusted_action_adapter(
            user_id=user_id, provider="microsoft", command=mail_command
        )
        assert isinstance(mail_adapter, MicrosoftMailWriteAdapter)
        mail_adapter.validate_for_approval(mail_command)
        send = mocked.post(MICROSOFT_SEND_MAIL_URL).respond(202)
        sent = mocked.get(MICROSOFT_SENT_MESSAGES_URL).respond(200, json=_sent_payload())
        assert (await mail_adapter.execute(mail_command)).kind is ProviderWriteOutcomeKind.UNKNOWN
        assert (
            await mail_adapter.reconcile(mail_command, _mail_execution())
        ).kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
        mime = BytesParser(policy=policy.default).parsebytes(
            base64.b64decode(send.calls[0].request.content)
        )
        assert str(mime["From"]) == "sender@example.test"
        assert send.call_count == sent.call_count == 1

        for index, command in enumerate((_create(), _update(), _restore())):
            command = replace(command, connection_id=connection_id)
            adapter = await registry.resolve_trusted_action_adapter(
                user_id=user_id, provider="microsoft", command=command
            )
            assert isinstance(adapter, MicrosoftCalendarWriteAdapter)
            adapter.validate_for_approval(command)
            response = _calendar_response()
            if index == 0:
                written = mocked.post(f"{GRAPH}/me/calendars/{CALENDAR_ID}/events").respond(
                    201, json=response
                )
                checked = mocked.get(f"{GRAPH}/me/calendars/{CALENDAR_ID}/events/event-1").respond(
                    200, json=response
                )
            else:
                checked = mocked.get(f"{GRAPH}/me/calendars/{CALENDAR_ID}/events/event-1").mock(
                    side_effect=[
                        httpx.Response(200, json=_current_event()),
                        httpx.Response(200, json=response),
                    ]
                )
                written = mocked.patch(
                    f"{GRAPH}/me/calendars/{CALENDAR_ID}/events/event-1"
                ).respond(200, json=response)
            written_before, checked_before = written.call_count, checked.call_count
            outcome = await adapter.execute(command)
            assert outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
            checked_outcome = await adapter.reconcile(
                command, _calendar_execution(command, provider_resource_id="event-1")
            )
            assert checked_outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED
            assert written.call_count - written_before == 1
            assert checked.call_count - checked_before == (1 if index == 0 else 2)
            if index:
                assert written.calls[0].request.headers["If-Match"] == 'W/"etag-1"'
        graph_calls = [
            call for call in mocked.calls if call.request.url.host == "graph.microsoft.com"
        ]
        assert graph_calls and all(
            call.request.headers["Authorization"] == f"Bearer {account.access_token}"
            for call in graph_calls
        )
