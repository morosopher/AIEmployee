"""以真实认证 HTTP start 与完整删除验证首次 OAuth attempt 的迟到持久化边界。

复用官方隔离数据库和 Cookie 登录 fixture；只在真实认证、CSRF 已完成后暂停请求。
OAuth adapter 与 HTTP 网络均受合成边界约束，不连接供应商账户，也不输出授权随机值。
"""

import asyncio
from dataclasses import replace
from datetime import time, timedelta
from typing import Annotated
from uuid import UUID

import httpx
import pytest
import respx
from fastapi import Depends, Request
from sqlalchemy import func, select

from ai_employee.api.deps import (
    CSRF_COOKIE_NAME,
    get_auth_token_hasher,
    get_authenticated_session,
    require_csrf_authenticated_session,
)
from ai_employee.application.use_cases.auth import TokenHasher
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.identity import AuthenticatedSession
from ai_employee.domain.settings import WeeklyWorkingHours
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import OAuthAttemptModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.workers.privacy import AllDataDeletionCompleted
from tests.integration.api import test_connections as connection_cases
from tests.integration.api.test_connections import AuthenticatedApiClients, FakeOAuthAdapter
from tests.integration.privacy.test_all_data_deletion import (
    BARRIER_NOW,
    BARRIER_REQUEST_ID,
    _barrier_lease,
    _DeletionClock,
    _PhaseCrashWorker,
)

# fixture 内的登录、会话摘要、CSRF 与应用组合根全部沿用生产实现。
authenticated_api_clients = connection_cases.authenticated_api_clients
COMPLETED_AT = BARRIER_NOW + timedelta(seconds=1)


class _InitialStartDeletionWorker(_PhaseCrashWorker):
    """复用真实完整删除，仅在屏障提交后显式推进 fixture 时钟，区分两次更新时间。"""

    async def _after_deletion_phase(self, *, phase: str) -> None:
        """所有持久阶段保持生产流程；最终时间由可替换 Clock 决定，不依赖宿主时间。"""
        await super()._after_deletion_phase(phase=phase)
        if phase == "barrier":
            self._clock = _DeletionClock(COMPLETED_AT)


async def _seed_authenticated_deletion(clients: AuthenticatedApiClients) -> LeasedTask:
    """为已真实登录的用户建立与既有删除 Worker fixture 精确匹配的 RUNNING 租约。"""
    lease = replace(_barrier_lease(), user_id=clients.owner_id)
    async with clients.session_factory.begin() as session:
        session.add(
            TaskRunModel(
                id=lease.task_id,
                user_id=lease.user_id,
                kind=lease.kind,
                status="running",
                idempotency_key="privacy-initial-oauth-start",
                input_payload=lease.input_payload,
                started_at=lease.started_at,
                attempt_count=1,
                lease_owner=lease.lease_owner,
                lease_expires_at=BARRIER_NOW + timedelta(minutes=5),
            )
        )
    return lease


async def _assert_anonymous_completion(sessions: ManagedAsyncSessionMaker, *, user_id: UUID) -> int:
    """逐字段验证匿名默认值及唯一完成审计，返回其 ID 用于证明迟到请求没有替换事实。"""
    async with sessions() as session:
        user = await session.get(UserModel, user_id)
        assert user is not None and not user.is_active
        assert user.email == f"deleted-{user_id}@invalid.local"
        assert user.display_name == "Deleted User"
        assert (
            user.password_hash,
            user.default_mail_connection_id,
            user.default_calendar_connection_id,
            user.default_calendar_id,
        ) == (None,) * 4
        assert (user.timezone, user.locale, user.brief_time) == ("UTC", "zh-CN", time(8))
        assert (
            user.email_body_retention_days,
            user.source_metadata_retention_days,
            user.workspace_history_retention_days,
        ) == (30, 180, 365)
        assert user.working_hours == WeeklyWorkingHours.default().to_mapping()
        assert user.meeting_buffer_minutes == 10 and user.updated_at == COMPLETED_AT
        events = (
            await session.scalars(select(AuditEventModel).where(AuditEventModel.user_id == user_id))
        ).all()
        assert len(events) == 1
        completion = events[0]
        assert completion.event_type == "privacy.deletion_completed"
        assert completion.task_id is None
        assert completion.actor_type == "system" and completion.actor_id is None
        assert completion.event_metadata == {
            "operation": "all_data_deletion",
            "request_id": BARRIER_REQUEST_ID,
            "completed_at": COMPLETED_AT.isoformat(),
            "trace_id": None,
        }
        return completion.id


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "microsoft"])
@pytest.mark.parametrize("start_first", [False, True], ids=["deletion-first", "start-first"])
async def test_task27e_initial_oauth_start_and_full_deletion_both_orders(
    authenticated_api_clients: AuthenticatedApiClients,
    provider: str,
    start_first: bool,
) -> None:
    """认证已完成的两供应商 start 要么先提交且被删除，要么在最终提交后稳定拒绝。"""
    clients = authenticated_api_clients
    lease = await _seed_authenticated_deletion(clients)
    adapters = {name: FakeOAuthAdapter(provider=name) for name in ("google", "microsoft")}
    clients.app.state.oauth_adapters = adapters
    assert not clients.app.state.auth_settings.app_test_mode
    authenticated, resume = asyncio.Event(), asyncio.Event()

    async def pause_after_authentication(
        request: Request,
        current: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
        token_hasher: Annotated[TokenHasher, Depends(get_auth_token_hasher)],
    ) -> AuthenticatedSession:
        """保留真实 Cookie/数据库认证及 CSRF，只在其事务已退出后让删除 Worker 先运行。"""
        checked = await require_csrf_authenticated_session(request, current, token_hasher)
        assert checked.user.id == clients.owner_id
        authenticated.set()
        await resume.wait()
        return checked

    clients.app.dependency_overrides[require_csrf_authenticated_session] = (
        pause_after_authentication
    )
    csrf = clients.owner.cookies.get(CSRF_COOKIE_NAME)
    assert csrf is not None
    request_task: asyncio.Task[httpx.Response] | None = None
    try:
        with respx.mock(assert_all_called=False) as network:
            async with asyncio.timeout(15):
                request_task = asyncio.create_task(
                    clients.owner.post(
                        f"/api/v1/connections/{provider}/start",
                        headers={"X-CSRF-Token": csrf},
                    )
                )
                await authenticated.wait()
                if start_first:
                    resume.set()
                    response = await request_task
                    assert response.status_code == 200
                    async with clients.session_factory() as session:
                        assert (
                            await session.scalar(
                                select(func.count())
                                .select_from(OAuthAttemptModel)
                                .where(OAuthAttemptModel.user_id == clients.owner_id)
                            )
                            == 1
                        )
                with pytest.raises(AllDataDeletionCompleted):
                    await _InitialStartDeletionWorker(clients.session_factory, "never").execute(
                        lease
                    )
                completion_id = await _assert_anonymous_completion(
                    clients.session_factory, user_id=clients.owner_id
                )
                if not start_first:
                    resume.set()
                    response = await request_task
                # 首先检查真实持久结果，使修复前 RED 明确证明 attempt 被重新创建。
                async with clients.session_factory() as session:
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(OAuthAttemptModel)
                            .where(OAuthAttemptModel.user_id == clients.owner_id)
                        )
                        == 0
                    )
                if not start_first:
                    assert response.status_code == 409
                    assert response.json()["error_code"] == "user_inactive"
                assert (
                    await _assert_anonymous_completion(
                        clients.session_factory, user_id=clients.owner_id
                    )
                    == completion_id
                )
                assert not network.calls
                assert all(adapter.revoke_calls == 0 for adapter in adapters.values())
    finally:
        resume.set()
        if request_task is not None:
            await asyncio.gather(request_task, return_exceptions=True)
        clients.app.dependency_overrides.pop(require_csrf_authenticated_session, None)
