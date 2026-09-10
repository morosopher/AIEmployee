"""在真实业务入口复现认证/授权网络先行而本地写入晚于删除的交错。

所有身份、凭据和请求均为合成数据。只暂停明确的网络/事务边界，不依赖 sleep 猜测竞争。
"""

import asyncio
from datetime import datetime, time, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select

from ai_employee.api.deps import ApiProblem, handle_api_problem, require_csrf_authenticated_session
from ai_employee.api.routers.conversations import build_conversations_router
from ai_employee.application.use_cases.connections import ConnectionCredentialOwnershipError
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.identity import AuthenticatedSession, SessionRecord, UserIdentity
from ai_employee.infrastructure.db.models.briefs import ConversationModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import EncryptedCredentialModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.conversations import (
    SqlAlchemyConversationRepository,
)
from ai_employee.infrastructure.db.repositories.settings import SqlAlchemySettingsRepository
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepository
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.workers.privacy import AllDataDeletionCompleted
from ai_employee.workers.retention import RetentionCleanupWorker
from tests.integration.m2 import test_credential_rotation_repository as rotation_cases
from tests.integration.m2.test_credential_rotation_repository import (
    SCOPES,
    RecoveryOAuthAdapter,
    begin_recovery,
    recovery_use_case,
    token_response,
)
from tests.integration.privacy.test_all_data_deletion import (
    BARRIER_NOW,
    BARRIER_REQUEST_ID,
    BARRIER_TASK_ID,
    BARRIER_USER_ID,
    _barrier_lease,
    _PhaseCrashWorker,
    _seed_barrier_task,
)

# 注册已有官方隔离 fixture；不创建第二套数据库或绕过其 seed/连接池释放协议。
oauth_state = rotation_cases.oauth_state


@pytest.mark.asyncio
async def test_task27e_retention_started_before_privacy_cannot_append_after_completion(
    database_url: str,
) -> None:
    """真实 retention 活动用户快照在最终删除后恢复，不能给匿名 user 追加第二条审计。"""
    sessions = build_session_factory(database_url)
    audit_ready, resume_audit = asyncio.Event(), asyncio.Event()

    class DelayedAuditRetention(RetentionCleanupWorker):
        """仅暂停独立审计事务之前；实际查询、清理与审计写入均使用生产路径。"""

        async def _append_cleanup_audit(self, user_id: UUID, now: datetime) -> None:
            """让全数据最终事务先提交，再继续旧 retention 请求的真实审计入口。"""
            audit_ready.set()
            await resume_audit.wait()
            await super()._append_cleanup_audit(user_id, now)

    job: asyncio.Task[int] | None = None
    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        async with asyncio.timeout(15):
            job = asyncio.create_task(DelayedAuditRetention(sessions).execute(now=BARRIER_NOW))
            await audit_ready.wait()
            with pytest.raises(AllDataDeletionCompleted):
                await _PhaseCrashWorker(sessions, "never").execute(_barrier_lease())
            resume_audit.set()
            await job
        async with sessions() as session:
            assert (
                await session.scalars(
                    select(AuditEventModel.event_type).where(
                        AuditEventModel.user_id == BARRIER_USER_ID,
                    )
                )
            ).all() == ["privacy.deletion_completed"]
    finally:
        resume_audit.set()
        if job is not None:
            await asyncio.gather(job, return_exceptions=True)
        await sessions.dispose()


@pytest.mark.asyncio
async def test_task27e_ordinary_callback_cannot_recreate_deleted_credentials(oauth_state) -> None:
    """ordinary progressive callback 已消费 state 并等待响应时，屏障后的保存必须整体拒绝。"""
    sessions, _, _ = oauth_state
    async with sessions.begin() as session:
        session.add(
            TaskRunModel(
                id=BARRIER_TASK_ID,
                user_id=BARRIER_USER_ID,
                kind="privacy.delete_all_data",
                status="running",
                idempotency_key="privacy-callback-barrier",
                input_payload={"deletion_request_id": BARRIER_REQUEST_ID},
                started_at=BARRIER_NOW,
                attempt_count=1,
                lease_owner="worker-before-barrier",
                lease_expires_at=BARRIER_NOW + timedelta(minutes=5),
            )
        )

    async def barrier_and_credential_delete() -> None:
        """供应商返回前让另一真实事务提交屏障和凭据删除，保留连接检验晚到保存。"""
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await _PhaseCrashWorker(sessions, "credentials_deleted").execute(_barrier_lease())
        async with sessions() as session:
            assert (
                await session.scalar(select(func.count()).select_from(EncryptedCredentialModel))
                == 0
            )

    adapter = RecoveryOAuthAdapter(
        token_response("synthetic-new-refresh"),
        before_response=barrier_and_credential_delete,
    )
    adapter.response = type(adapter.response)(
        "synthetic-new-access",
        "synthetic-new-refresh",
        3600,
        SCOPES | {"https://www.googleapis.com/auth/gmail.send"},
    )
    use_case = recovery_use_case(oauth_state, adapter)
    state = await begin_recovery(use_case, adapter)
    with pytest.raises(ConnectionCredentialOwnershipError):
        await use_case.callback(code="synthetic-code", state=state)
    assert adapter.calls == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(EncryptedCredentialModel)) == 0
        assert (
            await session.scalar(
                select(AuditEventModel.id).where(
                    AuditEventModel.event_type == "oauth.refresh_started"
                )
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["settings", "task", "conversation"])
async def test_task27e_late_authenticated_mutation_cannot_repopulate_anonymous_user(
    database_url: str,
    mutation: str,
) -> None:
    """最终提交后仍存匿名 user，迟到的旧认证上下文不能改设置、新建任务或会话。"""
    sessions = build_session_factory(database_url)
    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        authenticated = AuthenticatedSession(
            UserIdentity(
                BARRIER_USER_ID, "barrier@example.test", "Synthetic", "UTC", "zh-CN", time(8)
            ),
            SessionRecord(
                uuid4(),
                BARRIER_USER_ID,
                b"synthetic",
                BARRIER_NOW,
                BARRIER_NOW + timedelta(minutes=5),
                BARRIER_NOW,
                None,
            ),
        )
        with pytest.raises(AllDataDeletionCompleted):
            await _PhaseCrashWorker(sessions, "never").execute(_barrier_lease())
        if mutation == "conversation":
            app = FastAPI()
            app.add_exception_handler(ApiProblem, handle_api_problem)
            app.state.auth_session_factory = sessions
            app.include_router(build_conversations_router())

            async def previously_authenticated(request: Request) -> AuthenticatedSession:
                """返回屏障前已经完成的身份快照，专门模拟依赖返回后才提交的路由。"""
                del request
                return authenticated

            app.dependency_overrides[require_csrf_authenticated_session] = previously_authenticated
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://testserver"
            ) as client:
                response = await client.post("/api/v1/conversations")
            assert response.status_code == 401
        else:
            async with sessions.begin() as session:
                with pytest.raises(StateConflictError):
                    if mutation == "settings":
                        await SqlAlchemySettingsRepository(session).update(
                            user_id=authenticated.user.id, values={"timezone": "Asia/Shanghai"}
                        )
                    else:
                        await SqlAlchemyTaskRepository(session).create_with_outbox(
                            user_id=authenticated.user.id,
                            kind="daily_brief",
                            input_payload={},
                            idempotency_key="late-new-key",
                        )
        async with sessions() as session:
            user = await session.get(UserModel, BARRIER_USER_ID)
            assert user is not None and user.timezone == "UTC" and not user.is_active
            assert await session.scalar(select(func.count()).select_from(TaskRunModel)) == 0
            assert await session.scalar(select(func.count()).select_from(ConversationModel)) == 0
            assert (await session.scalars(select(AuditEventModel.event_type))).all() == [
                "privacy.deletion_completed"
            ]
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_task27e_late_conversation_delete_cannot_append_audit_after_completion(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保留真实屏障前已读 ORM 快照，模拟 delete 在最终提交后继续，不得追加旧删除审计。"""
    sessions = build_session_factory(database_url)
    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        async with sessions.begin() as session:
            previous = ConversationModel(user_id=BARRIER_USER_ID, title="Synthetic")
            session.add(previous)
            await session.flush()
            conversation_id = previous.id
        async with sessions() as session:
            # 保留一个确实由原 repository 从数据库读出的快照，模拟旧请求已经完成的 lookup。
            previous = await SqlAlchemyConversationRepository(session).get(
                user_id=BARRIER_USER_ID,
                conversation_id=conversation_id,
            )
            assert previous is not None
        with pytest.raises(AllDataDeletionCompleted):
            await _PhaseCrashWorker(sessions, "never").execute(_barrier_lease())
        app = FastAPI()
        app.state.auth_session_factory = sessions
        app.add_exception_handler(ApiProblem, handle_api_problem)
        app.include_router(build_conversations_router())

        async def prior_lookup(self, *, user_id, conversation_id):
            """重放已有 lookup 的返回值，不伪造用户状态或删除事务权限。"""
            del self
            assert user_id == BARRIER_USER_ID and previous.id == conversation_id
            return previous

        async def prior_authentication(request: Request) -> AuthenticatedSession:
            """只替代已完成的认证边界，实际业务 route/事务/DELETE/审计全部保留。"""
            del request
            return AuthenticatedSession(
                UserIdentity(
                    BARRIER_USER_ID, "barrier@example.test", "Synthetic", "UTC", "zh-CN", time(8)
                ),
                SessionRecord(
                    uuid4(),
                    BARRIER_USER_ID,
                    b"synthetic",
                    BARRIER_NOW,
                    BARRIER_NOW + timedelta(minutes=5),
                    BARRIER_NOW,
                    None,
                ),
            )

        monkeypatch.setattr(SqlAlchemyConversationRepository, "get", prior_lookup)
        app.dependency_overrides[require_csrf_authenticated_session] = prior_authentication
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            response = await client.delete(f"/api/v1/conversations/{conversation_id}")
        assert response.status_code == 401
        async with sessions() as session:
            assert (await session.scalars(select(AuditEventModel.event_type))).all() == [
                "privacy.deletion_completed"
            ]
    finally:
        await sessions.dispose()
