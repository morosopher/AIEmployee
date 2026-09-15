"""从真实取消、连接撤权与开关扫描调用者验证普通事务的删除屏障。"""

import asyncio
from datetime import timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Select, func, select
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from ai_employee.application.ports.oauth import OAuthRevocationResult, OAuthRevocationStatus
from ai_employee.application.use_cases.connections import (
    ConnectionNotFoundError,
    ConnectionsUseCase,
)
from ai_employee.application.use_cases.task_views import CancelTaskUseCase
from ai_employee.domain.actions import ToolExecutionStatus
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import TransientProviderError
from ai_employee.infrastructure.db.models.actions import CalendarChangeProposalModel, MailDraftModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStoreFactory
from ai_employee.infrastructure.db.repositories.task_views import SqlAlchemyTaskViewStore
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRevocationStore,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from tests.integration.m2.test_capability_revocation_races import (
    _DisconnectRevokeAdapter,
    _FixedClock,
)
from tests.integration.m2.test_tool_execution_claim import (
    NOW,
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
    _Seed,
    _seed_action,
    _seed_calendar_action,
)
from tests.integration.privacy.inactive_barrier import (
    assert_facts_unchanged,
    commit_deletion_barrier,
    database_facts,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]


async def _seed_connection_secrets(sessions: ManagedAsyncSessionMaker, seed: _Seed) -> None:
    """写入合成真实AEAD凭据和targetless attempt，让断开的全部后续DML可被比较。

    密文只驻留测试数据库；此fixture不会建立真实OAuth连接，也不输出token或PKCE内容。
    """
    cipher = AeadCipher(b"r" * 32)
    async with sessions.begin() as session:
        for kind in ("access_token", "refresh_token"):
            value = cipher.encrypt(
                f"synthetic-{kind}".encode(),
                f"{seed.user_id}:{seed.connection_id}:{kind}".encode("ascii"),
            )
            session.add(
                EncryptedCredentialModel(
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    credential_kind=kind,
                    ciphertext=value.ciphertext,
                    nonce=value.nonce,
                    key_version=value.key_version,
                    token_expires_at=NOW + timedelta(hours=1),
                )
            )
        verifier = cipher.encrypt(b"synthetic-pkce", b"synthetic-attempt")
        session.add(
            OAuthAttemptModel(
                user_id=seed.user_id,
                provider="google",
                state_hash=sha256(seed.user_id.bytes + b"synthetic-state").digest(),
                encrypted_pkce_verifier=verifier.ciphertext,
                nonce=verifier.nonce,
                key_version=verifier.key_version,
                created_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                requested_capabilities=["mail.read"],
            )
        )


@pytest.mark.parametrize(
    "mode",
    (
        "cancel-mail",
        "cancel-calendar",
        "disable-unclaimed",
        "disable-claimed",
        "disable-empty",
        "disconnect-unclaimed",
        "disconnect-claimed",
        "disconnect-empty",
        "scan",
    ),
)
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_cancellation_and_revocation_callers_respect_deletion_barrier(
    database_url: str,
    mode: str,
    inactive: bool,
) -> None:
    """真实用例调用完整仓储事务；不能只在公共失效helper返回零后继续改连接或凭据。

    包含有/无ToolExecution及空候选连接，覆盖helper自身和调用者后续DML。删除始终
    经过真实Worker屏障；inactive比较全表并重投，active检查实际任务/审批/能力/凭据。
    """
    seed, sessions = _Seed(), build_session_factory(database_url)
    claimed = mode.endswith("-claimed")
    if mode == "cancel-calendar":
        await _seed_calendar_action(database_url, seed)
    else:
        await _seed_action(
            database_url,
            seed,
            existing_execution_status=ToolExecutionStatus.CLAIMED if claimed else None,
        )
    adapter = _DisconnectRevokeAdapter()
    connections = ConnectionsUseCase(
        SqlAlchemyConnectionStoreFactory(sessions),
        AeadCipher(b"r" * 32),
        {"google": adapter},
        _FixedClock(),
    )
    try:
        if mode.startswith(("disable-", "disconnect-")):
            await _seed_connection_secrets(sessions, seed)
        async with sessions.begin() as session:
            if mode.startswith("cancel-") or mode.endswith("-empty"):
                task = await session.get(TaskRunModel, seed.task_id)
                approval = await session.get(ApprovalRequestModel, seed.approval_id)
                assert task is not None and approval is not None
                task.lease_owner = None
                task.lease_expires_at = None
                task.status = "waiting_approval" if mode.startswith("cancel-") else "cancelled"
                approval.status = "pending" if mode.startswith("cancel-") else "invalidated"
                if mode.endswith("-empty"):
                    draft = await session.get(MailDraftModel, seed.draft_id)
                    assert draft is not None
                    draft.status = "editing"
        if inactive:
            await commit_deletion_barrier(sessions, user_id=seed.user_id)
        before = await database_facts(sessions)

        async def submit() -> object:
            """保持真实用例的None/404及扫描数量语义，不跳过调用者的后续事务步骤。"""
            if mode.startswith("cancel-"):
                return await CancelTaskUseCase(SqlAlchemyTaskViewStore(sessions)).execute(
                    task_id=seed.task_id,
                    user_id=seed.user_id,
                    now=NOW,
                )
            if mode.startswith("disable-"):
                return await connections.disable_capability(
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    capability=ConnectionCapability.MAIL_SEND,
                )
            if mode.startswith("disconnect-"):
                return await connections.disconnect(
                    user_id=seed.user_id, connection_id=seed.connection_id
                )
            return await SqlAlchemyTrustedActionRevocationStore(
                sessions,
            ).invalidate_disabled_provider_actions(providers=frozenset({"google"}), limit=1)

        for _ in range(2 if inactive else 1):
            result = None
            try:
                result = await submit()
            except ConnectionNotFoundError:
                assert inactive and mode.startswith(("disable-", "disconnect-"))
            if inactive:
                assert_facts_unchanged(before, await database_facts(sessions))
                assert result is None or result == 0
            elif mode == "scan":
                assert result == 1
        if not inactive:
            async with sessions() as session:
                task = await session.get(TaskRunModel, seed.task_id)
                approval = await session.get(ApprovalRequestModel, seed.approval_id)
                local = await session.get(
                    CalendarChangeProposalModel if mode == "cancel-calendar" else MailDraftModel,
                    seed.draft_id,
                )
                assert task is not None and approval is not None and local is not None
                assert task.status == ("reconciling" if claimed else "cancelled")
                assert approval.status == ("approved" if claimed else "invalidated")
                assert local.status == ("needs_attention" if claimed else "editing")
                if mode.startswith(("disable-", "disconnect-")):
                    disconnected = mode.startswith("disconnect-")
                    connection = await session.get(OAuthConnectionModel, seed.connection_id)
                    capability = await session.scalar(
                        select(ConnectionCapabilityModel).where(
                            ConnectionCapabilityModel.connection_id == seed.connection_id,
                            ConnectionCapabilityModel.capability == "mail.send",
                        )
                    )
                    assert connection is not None and capability is not None
                    assert connection.status == ("disconnected" if disconnected else "connected")
                    assert capability.status == ("revoked" if disconnected else "disabled")
                    assert await session.scalar(
                        select(func.count()).select_from(
                            EncryptedCredentialModel,
                        )
                    ) == (0 if disconnected else 2)
                    attempt = await session.scalar(
                        select(OAuthAttemptModel).where(
                            OAuthAttemptModel.user_id == seed.user_id,
                        )
                    )
                    assert attempt is not None
                    assert (attempt.invalidated_at is not None) is disconnected
        assert len(adapter.revoked_tokens) == (
            1 if mode.startswith("disconnect-") and not inactive else 0
        )
    finally:
        await sessions.dispose()


async def test_revocation_scan_filters_inactive_before_limit(database_url: str) -> None:
    """固定UUID排序的inactive前缀不能占用关闭开关扫描的唯一名额。"""
    inactive, active = _Seed(), _Seed()
    inactive.task_id, active.task_id = UUID(int=1), UUID(int=2)
    await _seed_action(database_url, inactive)
    await _seed_action(database_url, active)
    sessions = build_session_factory(database_url)
    try:
        await commit_deletion_barrier(sessions, user_id=inactive.user_id)
        store = SqlAlchemyTrustedActionRevocationStore(sessions)
        assert (
            await store.invalidate_disabled_provider_actions(
                providers=frozenset({"google"}), limit=1
            )
            == 1
        )
        async with sessions() as session:
            inactive_task = await session.get(TaskRunModel, inactive.task_id)
            active_task = await session.get(TaskRunModel, active.task_id)
            assert inactive_task is not None and active_task is not None
            assert inactive_task.status == "running"
            assert active_task.status == "cancelled"
        assert (
            await store.invalidate_disabled_provider_actions(
                providers=frozenset({"google"}), limit=1
            )
            == 0
        )
    finally:
        await sessions.dispose()


async def test_revocation_scan_rechecks_after_candidate_lock(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选查询已经锁定Task后提交真实barrier，公共失效入口仍必须同步用户行。"""
    seed, sessions = _Seed(), build_session_factory(database_url)
    await _seed_action(database_url, seed)
    selected, release = asyncio.Event(), asyncio.Event()
    original = AsyncSession.execute

    async def pause_candidates(
        session: AsyncSession,
        statement: Executable,
        *args: Any,
        **kwargs: Any,
    ) -> Result[Any]:
        """Any仅位于SQLAlchemy第三方重载边界；原查询及事务真实执行。"""
        result = await original(session, statement, *args, **kwargs)
        if isinstance(statement, Select) and not selected.is_set():
            locking = statement._for_update_arg
            if (
                locking is not None
                and locking.skip_locked
                and any(
                    column.get("entity") is TaskRunModel for column in statement.column_descriptions
                )
            ):
                selected.set()
                await release.wait()
        return result

    pending: asyncio.Task[int] | None = None
    try:
        monkeypatch.setattr(AsyncSession, "execute", pause_candidates)
        store = SqlAlchemyTrustedActionRevocationStore(sessions)
        pending = asyncio.create_task(
            store.invalidate_disabled_provider_actions(
                providers=frozenset({"google"}),
                limit=1,
            )
        )
        await asyncio.wait_for(selected.wait(), timeout=5)
        await asyncio.wait_for(commit_deletion_barrier(sessions, user_id=seed.user_id), timeout=5)
        before = await database_facts(sessions)
        release.set()
        invalidated = await asyncio.wait_for(pending, timeout=5)
        assert_facts_unchanged(before, await database_facts(sessions))
        assert invalidated == 0
    finally:
        release.set()
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await sessions.dispose()


class _PausedRevokeFailure:
    """只暂停合成窄revoke，覆盖分类失败、未分类异常与明确不支持的后续审计事务。"""

    provider = "google"

    def __init__(self, mode: str) -> None:
        """保存确定性故障和网络屏障，不保存传入token。"""
        self.mode, self.calls = mode, 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """本地断开已经提交；这里仅返回合成结果，不访问Google或Microsoft。"""
        del token
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.mode == "classified":
            raise TransientProviderError(error_code="synthetic_revoke_failure", message="synthetic")
        if self.mode == "exception":
            raise RuntimeError("synthetic revoke failure")
        return OAuthRevocationResult(OAuthRevocationStatus.UNSUPPORTED, "oauth_revoke_unsupported")


@pytest.mark.parametrize("mode", ("classified", "exception", "unsupported"))
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_disconnect_network_failure_audit_respects_deletion_barrier(
    database_url: str,
    mode: str,
    inactive: bool,
) -> None:
    """断开先提交、窄revoke在途时真实barrier提交；随后失败不得新增普通审计。"""
    seed, sessions = _Seed(), build_session_factory(database_url)
    await _seed_action(database_url, seed)
    await _seed_connection_secrets(sessions, seed)
    adapter = _PausedRevokeFailure(mode)
    use_case = ConnectionsUseCase(
        SqlAlchemyConnectionStoreFactory(sessions),
        AeadCipher(b"r" * 32),
        {"google": adapter},
        _FixedClock(),
    )

    async def disconnect() -> None:
        """保留真实用例的异常传播；固定合成消息仅用于确认测试选择的失败分支。"""
        try:
            await use_case.disconnect(user_id=seed.user_id, connection_id=seed.connection_id)
        except TransientProviderError as error:
            assert mode == "classified" and error.error_code == "synthetic_revoke_failure"
        except RuntimeError as error:
            assert mode == "exception" and str(error) == "synthetic revoke failure"
        except ConnectionNotFoundError:
            assert inactive

    pending = asyncio.create_task(disconnect())
    try:
        await asyncio.wait_for(adapter.entered.wait(), timeout=5)
        if inactive:
            await commit_deletion_barrier(sessions, user_id=seed.user_id)
        before = await database_facts(sessions)
        adapter.release.set()
        await asyncio.wait_for(pending, timeout=5)
        if inactive:
            assert_facts_unchanged(before, await database_facts(sessions))
            await disconnect()
            assert_facts_unchanged(before, await database_facts(sessions))
        else:
            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(AuditEventModel)
                        .where(
                            AuditEventModel.user_id == seed.user_id,
                            AuditEventModel.event_type == "oauth.revoke_unresolved",
                        )
                    )
                    == 1
                )
        assert adapter.calls == 1
    finally:
        adapter.release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await sessions.dispose()
