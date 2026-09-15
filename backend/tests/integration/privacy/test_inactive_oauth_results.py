"""以真实删除屏障覆盖已消费 state 后、供应商 HTTP 失败的独立结果事务。"""

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import select

from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
from ai_employee.application.ports.oauth import OAuthProvider, OAuthProviderAdapter
from ai_employee.application.ports.oauth_refresh import OAuthRefreshError
from ai_employee.application.use_cases.connections import (
    ConnectionsUseCase,
    OAuthStateRejectedError,
)
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import DomainError, TransientProviderError
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStoreFactory
from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
    SqlAlchemyOAuthRefreshCoordinator,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.google.oauth import GoogleOAuthAdapter
from ai_employee.integrations.microsoft.oauth import MICROSOFT_TOKEN_URL, MicrosoftOAuthAdapter
from tests.integration.m2.test_credential_rotation_repository import (
    CONNECTION_ID,
    USER_ID,
    FakeRefreshProvider,
    RecoveryClock,
    persisted_credentials,
    refresh_request,
    token_response,
)
from tests.integration.m2.test_credential_rotation_repository import (
    oauth_state as oauth_state,  # noqa: PLC0414 - 复用真实数据库 fixture，不复制凭据或协议 writer。
)
from tests.integration.privacy.test_all_data_deletion import (
    _PhaseCrashWorker,
    _seed_owned_deletion_lease,
)

type OAuthFixture = tuple[ManagedAsyncSessionMaker, AeadCipher, SqlAlchemyOAuthRefreshCoordinator]
type DatabaseFacts = tuple[tuple[tuple[object, ...], ...], ...]


async def _facts(sessions: ManagedAsyncSessionMaker) -> DatabaseFacts:
    """读取全部受影响列，拒绝时能力、凭据、state、审计、Outbox 和删除赢家均须精确不变。"""
    tables = (
        OAuthConnectionModel.__table__,
        ConnectionCapabilityModel.__table__,
        EncryptedCredentialModel.__table__,
        OAuthAttemptModel.__table__,
        AuditEventModel.__table__,
        OutboxEventModel.__table__,
        TaskRunModel.__table__,
    )
    async with sessions() as session:
        facts: list[tuple[tuple[object, ...], ...]] = []
        for table in tables:
            rows = await session.execute(select(table).order_by(*table.primary_key.columns))
            facts.append(tuple(tuple(row) for row in rows.all()))
        return tuple(facts)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ("google", "microsoft"))
@pytest.mark.parametrize(
    "recovery,stale_target",
    ((False, False), (True, False), (True, True)),
    ids=("progressive", "recovery", "stale-recovery"),
)
@pytest.mark.parametrize("delete_before_response", (False, True), ids=("active", "inactive"))
async def test_consumed_oauth_failure_respects_committed_privacy_barrier(
    oauth_state: OAuthFixture,
    provider: str,
    recovery: bool,
    stale_target: bool,
    delete_before_response: bool,
) -> None:
    """HTTP 已收到真实请求后才提交 inactive，不能借后续失败事务重写任何普通事实。

    普通和 recovery 都先经真实 start/consume；recovery 额外经自动 unknown fence 与
    shared lease。只替代供应商 HTTP，删除时序由真实 Worker 的已提交 barrier hook 给出。
    active 邻接仍收敛为 action_required，state 重投始终不再调用供应商。
    """
    sessions, cipher, coordinator = oauth_state
    if provider == "microsoft":
        scopes = sorted(
            {
                "openid",
                "profile",
                "email",
                "offline_access",
                "User.Read",
                "Mail.Read",
                "Calendars.Read",
            }
        )
        async with sessions.begin() as session:
            connection = await session.get(OAuthConnectionModel, CONNECTION_ID)
            assert connection is not None
            connection.provider = "microsoft"
            connection.provider_tenant_id = "11111111-2222-3333-4444-555555555555"
            connection.provider_account_id = (
                "11111111-2222-3333-4444-555555555555:00000000-0000-0000-0000-000000000701"
            )
            connection.account_type, connection.scopes = "work_school", scopes
            for capability in (await session.scalars(select(ConnectionCapabilityModel))).all():
                capability.actual_scopes = scopes

    if recovery:

        async def unknown_refresh() -> None:
            """只在自动刷新 HTTP 边界注入未知结果，真实 writer 负责持久化原始 fence。"""
            raise TransientProviderError(
                error_code="synthetic_refresh_unknown", message="Safe failure"
            )

        refresh = FakeRefreshProvider(token_response(), unknown_refresh)
        refresh.provider = OAuthProvider(provider)
        with pytest.raises(OAuthRefreshError):
            await coordinator.refresh(refresh_request(), refresh)
        assert refresh.calls == 1

    adapter: OAuthProviderAdapter = (
        GoogleOAuthAdapter(
            "synthetic-client", "synthetic-secret", "https://app.example.test/callback"
        )
        if provider == "google"
        else MicrosoftOAuthAdapter(
            "synthetic-client", "synthetic-secret", "https://app.example.test/callback"
        )
    )
    use_case = ConnectionsUseCase(
        SqlAlchemyConnectionStoreFactory(sessions),
        cipher,
        {provider: adapter},
        RecoveryClock(),
        identity=OAuthRefreshIdentity(bytes(range(32)), key_version=cipher.key_version),
        coordinator=coordinator,
    )
    started = await use_case.start_capability_enable(
        user_id=USER_ID, connection_id=CONNECTION_ID, capability=ConnectionCapability.MAIL_SEND
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    credentials_before = await persisted_credentials(sessions)
    lease = await _seed_owned_deletion_lease(
        sessions, user_id=USER_ID, request_id="synthetic-inflight-oauth-deletion"
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail_after_barrier(request: httpx.Request) -> httpx.Response:
        """请求进入真实 adapter 的 HTTP 边界后暂停，返回明确供应商拒绝而非未知网络结果。"""
        assert request.method == "POST"
        entered.set()
        await release.wait()
        return httpx.Response(400, json={"error": "invalid_grant"})

    url = "https://oauth2.googleapis.com/token" if provider == "google" else MICROSOFT_TOKEN_URL
    with respx.mock(assert_all_called=True) as mocked:
        endpoint = mocked.post(url).mock(side_effect=fail_after_barrier)
        callback = asyncio.create_task(use_case.callback(code="synthetic-code", state=state))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            async with sessions() as session:
                attempt = await session.scalar(select(OAuthAttemptModel))
                assert attempt is not None and attempt.consumed_at is not None
            if stale_target:
                # 新一次真实 start 推进 T，旧失败只能关闭旧恢复；inactive 后连这一普通
                # unsatisfied 审计也不得补写，不能仅依赖 current-T 的能力一致性检查挡住。
                await use_case.start_capability_enable(
                    user_id=USER_ID,
                    connection_id=CONNECTION_ID,
                    capability=ConnectionCapability.MAIL_SEND,
                )
            if delete_before_response:
                worker = _PhaseCrashWorker(sessions, "barrier")
                with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
                    await worker.execute(lease)
                assert worker.visited == ["barrier"]
            before = await _facts(sessions)
            release.set()
            with pytest.raises(DomainError):
                await asyncio.wait_for(callback, timeout=5)
            after = await _facts(sessions)
            if delete_before_response:
                unchanged = before == after
                assert unchanged, (
                    "late OAuth failure changed facts after the committed privacy barrier"
                )
            else:
                async with sessions() as session:
                    capabilities = (await session.scalars(select(ConnectionCapabilityModel))).all()
                    expected_status = "authorizing" if stale_target else "action_required"
                    assert all(item.status == expected_status for item in capabilities)
                    events = (await session.scalars(select(AuditEventModel.event_type))).all()
                    assert events.count("oauth.refresh_recovery_unsatisfied") == int(recovery)
            assert await persisted_credentials(sessions) == credentials_before
            with pytest.raises(OAuthStateRejectedError):
                await use_case.callback(code="synthetic-code", state=state)
            assert endpoint.call_count == 1
        finally:
            release.set()
            if not callback.done():
                callback.cancel()
            await asyncio.gather(callback, return_exceptions=True)
