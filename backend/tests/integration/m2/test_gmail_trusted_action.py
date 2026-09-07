"""验证 Gmail adapter 接入固定可信动作 registry 后的幂等核对边界。"""

import os
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
import pytest
import respx

from ai_employee.application.ports.trusted_actions import ExecutionReference
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.mail_actions import MailMode, MailSendCommand
from ai_employee.integrations.google.gmail_write import GMAIL_SEND_URL, GmailWriteAdapter
from ai_employee.integrations.registry import ProviderAdapterRegistry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from ai_employee.application.ports.oauth_refresh import OAuthRefreshReady
    from tests.integration.m2.test_tool_execution_claim import _Seed

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ,
    reason="Gmail trusted-action integration requires TEST_DATABASE_URL",
)


def _command() -> MailSendCommand:
    """构造最小合成新邮件命令。"""
    from datetime import UTC, datetime

    return MailSendCommand(
        schema_version="mail_send.v1",
        action="mail.send",
        operation_id=UUID("00000000-0000-0000-0000-000000000011"),
        connection_id=UUID("00000000-0000-0000-0000-000000000012"),
        draft_id=UUID("00000000-0000-0000-0000-000000000013"),
        draft_version=1,
        message_date=datetime(2030, 1, 1, tzinfo=UTC),
        mode=MailMode.NEW,
        source_thread_id=None,
        source_message_id=None,
        to=("recipient@example.test",),
        cc=(),
        bcc=(),
        subject="Synthetic subject",
        body_text="Synthetic body",
        thread_headers=None,
    )


def _execution() -> ExecutionReference:
    """构造 unknown 恢复所需的内容无关执行投影。"""
    return ExecutionReference(
        execution_id=UUID("00000000-0000-0000-0000-000000000014"),
        task_id=UUID("00000000-0000-0000-0000-000000000015"),
        step_id=UUID("00000000-0000-0000-0000-000000000016"),
        approval_id=UUID("00000000-0000-0000-0000-000000000017"),
        operation_id=UUID("00000000-0000-0000-0000-000000000011"),
        provider="google",
        tool_name="mail.send",
        idempotency_key="synthetic-idempotency",
        request_payload_hash="a" * 64,
        status=ToolExecutionStatus.RECONCILING,
        result_summary=None,
        request_started_at=None,
        write_attempt_count=1,
        provider_resource_id=None,
        provider_request_id=None,
        correlation_id=None,
    )


@pytest.mark.asyncio
@respx.mock
async def test_registry_exposes_gmail_action_and_unknown_reentry_is_read_only() -> None:
    """固定 registry 可取出 Gmail adapter，核对重入不会再次调用 send。"""
    send = respx.post(GMAIL_SEND_URL).mock(return_value=httpx.Response(503))
    search = respx.get("https://gmail.googleapis.com/gmail/v1/users/me/messages").mock(
        return_value=httpx.Response(200, json={"messages": []})
    )
    adapter = GmailWriteAdapter(
        access_token="synthetic-access-token", account_email="sender@example.test"
    )
    registry = ProviderAdapterRegistry(google_mail_action=adapter)
    resolved = registry.trusted_action_adapter(provider="google", action="mail.send")

    first = await resolved.execute(_command())
    second = await resolved.reconcile(_command(), _execution())

    assert first.kind is ProviderWriteOutcomeKind.UNKNOWN
    # 单轮 Sent 无匹配不能证明请求未应用；上层必须继续有界只读核对或转人工关注。
    assert second.kind is ProviderWriteOutcomeKind.UNKNOWN
    assert send.call_count == 1
    assert search.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("expired_boundary", ["approval", "lease"])
async def test_connection_readiness_wait_rechecks_database_deadline_before_claim(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    expired_boundary: str,
) -> None:
    """新增本地凭据检查的等待跨过截止点时，必须重读 PostgreSQL 时间并保持零 claim。"""
    import asyncio
    from datetime import timedelta

    from sqlalchemy import func, select, update

    from ai_employee.application.use_cases.trusted_actions import TrustedActionExecutionUseCase
    from ai_employee.domain.errors import StateConflictError
    from ai_employee.infrastructure.db.models.tasks import (
        ApprovalRequestModel,
        TaskRunModel,
        ToolExecutionModel,
    )
    from ai_employee.infrastructure.db.repositories.trusted_actions import (
        SqlAlchemyTrustedActionRepositoryFactory,
    )
    from ai_employee.infrastructure.db.session import build_session_factory
    from ai_employee.integrations.registry import CredentialBoundProviderRegistry
    from tests.integration.m2.test_tool_execution_claim import (
        ACTION_CIPHER,
        NOW,
        _Seed,
        _seed_action,
        _settings,
    )

    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    sessions = build_session_factory(database_url)
    checks = 0
    try:
        async with sessions.begin() as session:
            database_now = await session.scalar(select(func.clock_timestamp()))
            cutoff = database_now + timedelta(milliseconds=500)
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == seed.task_id)
                .values(
                    lease_expires_at=cutoff
                    if expired_boundary == "lease"
                    else database_now + timedelta(minutes=1)
                )
            )
            await session.execute(
                update(ApprovalRequestModel)
                .where(ApprovalRequestModel.id == seed.approval_id)
                .values(
                    approved_execution_deadline_at=cutoff
                    if expired_boundary == "approval"
                    else database_now + timedelta(minutes=1)
                )
            )

        async def delayed_local_check(self, **binding) -> None:
            """只替代可用性读取的延时，仍用真实数据库时钟证明已经跨过被测边界。"""
            nonlocal checks
            checks += 1
            assert binding == {
                "user_id": seed.user_id,
                "connection_id": seed.connection_id,
                "provider": "google",
                "action": "mail.send",
            }
            async with sessions() as session:
                while await session.scalar(select(func.clock_timestamp())) <= cutoff:
                    await asyncio.sleep(0.01)

        # RED 时生产 claim 尚未调用此边界，因此不会等待；GREEN 必须既调用它，又在
        # 等待后重新取得权威时间。该 Fake 不解密命令、不读取 Secret，也不访问供应商。
        monkeypatch.setattr(
            CredentialBoundProviderRegistry,
            "validate_trusted_action_connection",
            delayed_local_check,
            raising=False,
        )
        settings = _settings()
        workflow = TrustedActionExecutionUseCase(
            transactions=SqlAlchemyTrustedActionRepositoryFactory(sessions, ACTION_CIPHER),
            adapters=CredentialBoundProviderRegistry(session_factory=sessions, settings=settings),
            write_policy=settings,
            clock=lambda: NOW,
        )
        with pytest.raises(StateConflictError) as rejected:
            await workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert checks == 1
        assert rejected.value.error_code == (
            "approval_execution_deadline_expired"
            if expired_boundary == "approval"
            else "trusted_action_unavailable"
        )
        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolExecutionModel)
                    .where(ToolExecutionModel.task_id == seed.task_id)
                )
                == 0
            )
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.respx(assert_all_called=False)
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "concurrent_refresh",
        "unknown",
        "cas_miss",
        "proof_crash",
        "ready_rollback",
        "unknown_rollback",
        "lease_lock_expiry",
        "access_lock_expiry",
        "revoked_after_refresh",
        "budget_exhausted",
    ],
)
async def test_production_registry_401_commits_oauth_proof_before_second_write(
    database_url: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    respx_mock: respx.MockRouter,
) -> None:
    """实际 Worker registry 的 401 必须先持久未应用和 refresh-confirmed，再重新走 request-start。"""
    import base64
    from datetime import timedelta
    from functools import partial

    from sqlalchemy import func, select

    from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
    from ai_employee.application.ports.oauth_refresh import OAuthRefreshReady, OAuthRefreshRequest
    from ai_employee.application.ports.trusted_actions import TrustedActionDispatchSnapshot
    from ai_employee.application.use_cases.trusted_actions import (
        TrustedActionAttemptAbandoned,
        TrustedActionExecutionUseCase,
    )
    from ai_employee.domain.connections import ConnectionCapability
    from ai_employee.domain.errors import StateConflictError, TransientProviderError
    from ai_employee.infrastructure.db.models.actions import MailDraftModel
    from ai_employee.infrastructure.db.models.sources import (
        ConnectionCapabilityModel,
        EncryptedCredentialModel,
        OAuthConnectionModel,
    )
    from ai_employee.infrastructure.db.models.tasks import (
        AuditEventModel,
        OutboxEventModel,
        TaskRunModel,
        ToolExecutionModel,
    )
    from ai_employee.infrastructure.db.repositories.credential_rotation import load_refresh_snapshot
    from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
        SqlAlchemyOAuthRefreshCoordinator,
    )
    from ai_employee.infrastructure.db.repositories.trusted_actions import (
        SqlAlchemyTrustedActionRepository,
        SqlAlchemyTrustedActionRepositoryFactory,
    )
    from ai_employee.infrastructure.db.session import build_session_factory
    from ai_employee.infrastructure.security.encryption import AeadCipher
    from ai_employee.integrations import registry as registry_module
    from ai_employee.integrations.google.oauth import GoogleOAuthAdapter
    from ai_employee.workers.trusted_actions import build_worker_trusted_action_registry
    from tests.integration.m2.test_oauth_refresh_coordinator import read_across_committed_refresh
    from tests.integration.m2.test_tool_execution_claim import (
        ACTION_CIPHER,
        NOW,
        _Seed,
        _seed_action,
        _settings,
    )

    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    sessions = build_session_factory(database_url)
    coordinator_now = NOW
    if fault == "access_lock_expiry":
        # 只替换协调器已有 Clock：短期 access 的 expiry 由真实刷新响应和唯一 writer
        # 生成，不改写任何 credential/proof 字节来制造本应匹配的 CAS。
        async with sessions() as session:
            coordinator_now = await session.scalar(select(func.clock_timestamp()))
    # 仅通过真实协调器已公开的 Clock 参数替换时间，Secret/身份构造和生产 registry
    # 仍使用完整实现，避免 credential expiry 与真实系统日期绑定。
    monkeypatch.setattr(
        registry_module,
        "SqlAlchemyOAuthRefreshCoordinator",
        partial(registry_module.SqlAlchemyOAuthRefreshCoordinator, clock=lambda: coordinator_now),
    )
    root_file, client_file = tmp_path / "master", tmp_path / "google"
    root_file.write_text(base64.urlsafe_b64encode(b"x" * 32).decode("ascii"))
    client_file.write_text("synthetic-client-secret")
    settings = _settings().model_copy(
        update={
            "app_master_key_file": root_file,
            "google_client_secret_file": client_file,
            "google_client_id": "synthetic-client",
            "google_redirect_uri": "https://app.example.test/callback",
        }
    )
    scopes = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ]
    cipher = AeadCipher(b"x" * 32)
    try:
        async with sessions.begin() as session:
            connection = await session.get(OAuthConnectionModel, seed.connection_id)
            connection.scopes = scopes
            capabilities = (
                await session.scalars(
                    select(ConnectionCapabilityModel).where(
                        ConnectionCapabilityModel.user_id == seed.user_id
                    )
                )
            ).all()
            for capability in capabilities:
                capability.actual_scopes = [scopes[capability.capability == "mail.send"]]
            for kind in ("access_token", "refresh_token"):
                encrypted = cipher.encrypt(
                    f"synthetic-{kind}".encode(),
                    f"{seed.user_id}:{seed.connection_id}:{kind}".encode(),
                )
                session.add(
                    EncryptedCredentialModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        credential_kind=kind,
                        ciphertext=encrypted.ciphertext,
                        nonce=encrypted.nonce,
                        key_version=encrypted.key_version,
                        token_expires_at=NOW + timedelta(minutes=5)
                        if kind == "access_token"
                        else None,
                    )
                )
        # 标记配置独立 fixture router；所有路由必须绑定它，未注册网络仍严格拒绝。
        send = respx_mock.post(GMAIL_SEND_URL).mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json={"id": "synthetic-sent", "threadId": "synthetic-thread"}),
            ]
        )

        async def inspect_durable_rejection(request):
            """grant 开始时前一次写请求已有明确未应用事实，且 coordinator started 已提交。"""
            nonlocal coordinator_now
            async with sessions() as session:
                execution = await session.scalar(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                )
                assert execution.result_summary == {
                    "kind": "confirmed_not_applied",
                    "retryable": False,
                }
                assert execution.write_attempt_count == 1
                assert (
                    await session.scalar(
                        select(AuditEventModel.id).where(
                            AuditEventModel.event_type == "oauth.refresh_started"
                        )
                    )
                    is not None
                )
                if fault == "access_lock_expiry":
                    coordinator_now = await session.scalar(select(func.clock_timestamp()))
            if fault in {"unknown", "unknown_rollback"}:
                raise httpx.ReadTimeout("synthetic lost OAuth response", request=request)
            if fault == "cas_miss":
                async with sessions.begin() as session:
                    connection = await session.get(OAuthConnectionModel, seed.connection_id)
                    connection.authorization_generation += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-ready-access",
                    "expires_in": 2 if fault == "access_lock_expiry" else 3600,
                    "scope": " ".join(scopes),
                },
            )

        refresh = respx_mock.post("https://oauth2.googleapis.com/token").mock(
            side_effect=inspect_durable_rejection
        )
        captured_ready: OAuthRefreshReady | None = None
        if fault in {"lease_lock_expiry", "access_lock_expiry"}:
            original_resolution = SqlAlchemyTrustedActionRepository.resolve_oauth_write_retry

            async def resolve_after_lock_wait(
                self: SqlAlchemyTrustedActionRepository,
                *,
                snapshot: TrustedActionDispatchSnapshot,
                lease_owner: str,
                ready: OAuthRefreshReady | None,
                error_code: str | None = None,
            ) -> None:
                """仅在真实 coordinator 返回 readiness 后插入数据库行锁屏障。"""
                nonlocal captured_ready
                assert ready is not None
                captured_ready = ready

                async def invoke() -> None:
                    """保留真实 resolver 全部锁、proof 读取和 mutation，不替代其时钟。"""
                    await original_resolution(
                        self,
                        snapshot=snapshot,
                        lease_owner=lease_owner,
                        ready=ready,
                        error_code=error_code,
                    )

                await _hold_credential_lock_until_expiry(
                    database_url,
                    seed,
                    caller_session=self._session,
                    ready=ready,
                    boundary=fault,
                    invoke=invoke,
                )

            monkeypatch.setattr(
                SqlAlchemyTrustedActionRepository,
                "resolve_oauth_write_retry",
                resolve_after_lock_wait,
            )
        if fault in {"proof_crash", "ready_rollback", "unknown_rollback"}:
            original = SqlAlchemyTrustedActionRepository.resolve_oauth_write_retry
            crashed = False

            async def crash_once(self, **values):
                """在指定真实事务边界中断，证明前序事实独立持久且结果三件套原子回滚。"""
                nonlocal crashed
                if not crashed:
                    crashed = True
                    if fault != "proof_crash":
                        await original(self, **values)
                        resolution_topic = (
                            "tool.needs_attention"
                            if fault == "unknown_rollback"
                            else "tool.oauth_refresh_confirmed"
                        )
                        # 此处仍处在应用用例拥有的真实事务中；缺少 Outbox 的旧实现
                        # 必须先在这里失败，不能把“什么也没写”冒充原子回滚的证明。
                        staged_audit_id = await self._session.scalar(
                            select(AuditEventModel.id).where(
                                AuditEventModel.task_id == seed.task_id,
                                AuditEventModel.event_type == resolution_topic,
                            )
                        )
                        staged_outbox = await self._session.scalar(
                            select(OutboxEventModel).where(
                                OutboxEventModel.aggregate_id == seed.task_id,
                                OutboxEventModel.topic == resolution_topic,
                            )
                        )
                        assert staged_audit_id is not None
                        assert staged_outbox is not None
                        assert staged_outbox.payload == {
                            "task_id": str(seed.task_id),
                            "audit_event_id": staged_audit_id,
                        }
                    raise RuntimeError("synthetic OAuth proof handoff crash")
                await original(self, **values)

            monkeypatch.setattr(
                SqlAlchemyTrustedActionRepository, "resolve_oauth_write_retry", crash_once
            )
        workflow = TrustedActionExecutionUseCase(
            transactions=SqlAlchemyTrustedActionRepositoryFactory(sessions, ACTION_CIPHER),
            adapters=build_worker_trusted_action_registry(
                session_factory=sessions, settings=settings
            ),
            write_policy=settings,
            clock=lambda: NOW,
        )
        arguments = {
            "task_id": seed.task_id,
            "approval_id": seed.approval_id,
            "operation_id": seed.operation_id,
            "expected_payload_hash": payload_hash,
            "lease_owner": seed.owner,
        }
        await workflow.claim(**arguments)
        if fault in {"lease_lock_expiry", "access_lock_expiry"}:
            # 同时捕获旧实现的 ready 错误与正确的状态冲突，让 RED 直接断言真实
            # 持久 retryable 值，而不是仅证明异常类型发生变化。
            with pytest.raises((StateConflictError, TransientProviderError)) as resolution:
                await workflow.execute_or_reconcile(**arguments)
            assert send.call_count == refresh.call_count == 1
            assert captured_ready is not None
            async with sessions() as session:
                execution = await session.scalar(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                )
                task = await session.get(TaskRunModel, seed.task_id)
                draft = await session.get(MailDraftModel, seed.draft_id)
                credentials, _, _ = await load_refresh_snapshot(
                    session,
                    OAuthRefreshRequest(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        capability=ConnectionCapability.MAIL_SEND,
                    ),
                    lock=False,
                )
                database_now = await session.scalar(select(func.clock_timestamp()))
                # 锁持有者没有更新任何凭据列；失败必须来自等待后的时间门禁，不能
                # 由不同 snapshot 或缺失历史 proof 偶然遮蔽时间采样缺陷。
                assert credentials == captured_ready.snapshot
                assert (
                    await session.scalar(
                        select(AuditEventModel.id).where(
                            AuditEventModel.user_id == seed.user_id,
                            AuditEventModel.event_type == "oauth.refresh_confirmed",
                        )
                    )
                    is not None
                )
                if fault == "lease_lock_expiry":
                    assert task.lease_expires_at <= database_now
                    assert credentials.access.token_expires_at > database_now
                else:
                    assert credentials.access.token_expires_at <= database_now
                    assert task.lease_expires_at > database_now
                assert execution.result_summary == {
                    "kind": "confirmed_not_applied",
                    "retryable": False,
                }
                assert execution.status == "retryable_failed" and execution.write_attempt_count == 1
                assert execution.error_code == "google_reauthorization_required"
                assert task.status == "running" and task.lease_owner == seed.owner
                assert task.error_code is None and task.finished_at is None
                assert draft.status == "executing"
                assert list(
                    await session.scalars(
                        select(AuditEventModel.event_type)
                        .where(AuditEventModel.task_id == seed.task_id)
                        .order_by(AuditEventModel.id)
                    )
                ) == ["tool.claimed", "tool.oauth_refresh_required"]
                assert sorted(
                    await session.scalars(
                        select(OutboxEventModel.topic).where(
                            OutboxEventModel.aggregate_id == seed.task_id
                        )
                    )
                ) == ["tool.claimed", "tool.oauth_refresh_required"]
            assert isinstance(resolution.value, StateConflictError)
            assert resolution.value.error_code == "trusted_action_unavailable"
            return
        if fault == "budget_exhausted":
            await workflow.execute_or_reconcile(**arguments, may_retry_write=False)
            await workflow.execute_or_reconcile(**arguments)
            assert send.call_count == 1 and refresh.call_count == 0
            async with sessions() as session:
                execution = await session.scalar(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                )
                assert execution.status == "confirmed_failed" and execution.write_attempt_count == 1
            return
        if fault in {"ready_rollback", "unknown_rollback"}:
            with pytest.raises(RuntimeError, match="synthetic OAuth proof handoff crash"):
                await workflow.execute_or_reconcile(**arguments)
            assert send.call_count == refresh.call_count == 1
            async with sessions() as session:
                execution = await session.scalar(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                )
                task = await session.get(TaskRunModel, seed.task_id)
                draft = await session.get(MailDraftModel, seed.draft_id)
                assert execution.status == "retryable_failed"
                assert execution.result_summary == {
                    "kind": "confirmed_not_applied",
                    "retryable": False,
                }
                assert execution.write_attempt_count == 1
                assert task.status == "running" and task.lease_owner == seed.owner
                assert task.lease_expires_at == NOW + timedelta(minutes=1)
                assert task.error_code is None and task.finished_at is None
                assert draft.status == "executing"
                for event_type in ("tool.oauth_refresh_confirmed", "tool.needs_attention"):
                    assert (
                        await session.scalar(
                            select(AuditEventModel.id).where(
                                AuditEventModel.task_id == seed.task_id,
                                AuditEventModel.event_type == event_type,
                            )
                        )
                        is None
                    )
                    assert (
                        await session.scalar(
                            select(OutboxEventModel.id).where(
                                OutboxEventModel.aggregate_id == seed.task_id,
                                OutboxEventModel.topic == event_type,
                            )
                        )
                        is None
                    )
                assert (
                    await session.scalar(
                        select(OutboxEventModel.id).where(
                            OutboxEventModel.aggregate_id == seed.task_id,
                            OutboxEventModel.topic == "tool.oauth_refresh_required",
                        )
                    )
                    is not None
                )
        if fault in {"unknown", "unknown_rollback", "cas_miss"}:
            with pytest.raises(TrustedActionAttemptAbandoned):
                await workflow.execute_or_reconcile(**arguments)
            for _ in range(2):
                await workflow.execute_or_reconcile(**arguments)
            assert send.call_count == refresh.call_count == 1
            async with sessions() as session:
                execution = await session.scalar(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                )
                assert execution.status == "needs_attention" and execution.write_attempt_count == 1
                assert execution.result_summary == {
                    "kind": "confirmed_not_applied",
                    "retryable": False,
                }
                assert (
                    await session.scalar(
                        select(AuditEventModel.id).where(
                            AuditEventModel.event_type == "oauth.refresh_confirmed"
                        )
                    )
                    is None
                )
            await _assert_oauth_lifecycle_delivery(
                database_url, seed, resolution_topic="tool.needs_attention"
            )
            assert send.call_count == refresh.call_count == 1
            return
        if fault == "proof_crash":
            with pytest.raises(RuntimeError, match="synthetic OAuth proof handoff crash"):
                await workflow.execute_or_reconcile(**arguments)
            # 新用例/registry 只读取相同执行派生的 attempt，不能把已提交 refresh 再发送。
            workflow = TrustedActionExecutionUseCase(
                transactions=SqlAlchemyTrustedActionRepositoryFactory(sessions, ACTION_CIPHER),
                adapters=build_worker_trusted_action_registry(
                    session_factory=sessions, settings=settings
                ),
                write_policy=settings,
                clock=lambda: NOW,
            )
        with pytest.raises(TransientProviderError):
            await workflow.execute_or_reconcile(**arguments)
        assert send.call_count == refresh.call_count == 1
        async with sessions() as session:
            assert (
                await session.scalar(
                    select(AuditEventModel.id).where(
                        AuditEventModel.event_type == "oauth.refresh_confirmed"
                    )
                )
                is not None
            )
        await _assert_oauth_lifecycle_delivery(
            database_url, seed, resolution_topic="tool.oauth_refresh_confirmed"
        )
        assert send.call_count == refresh.call_count == 1
        if fault == "revoked_after_refresh":
            async with sessions.begin() as session:
                capability = await session.scalar(
                    select(ConnectionCapabilityModel).where(
                        ConnectionCapabilityModel.connection_id == seed.connection_id,
                        ConnectionCapabilityModel.capability == "mail.send",
                    )
                )
                capability.status = "disabled"
            with pytest.raises(TrustedActionAttemptAbandoned):
                await workflow.execute_or_reconcile(**arguments)
            await workflow.execute_or_reconcile(**arguments)
            assert send.call_count == refresh.call_count == 1
            return
        if fault == "concurrent_refresh":

            async def concurrent_grant(request: httpx.Request) -> httpx.Response:
                """第二个真实 grant 只旋转连接凭据，原写仍持有已批准的未应用重试事实。"""
                del request
                async with sessions() as session:
                    execution = await session.scalar(
                        select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                    )
                    assert execution.status == "retryable_failed"
                    assert execution.result_summary == {
                        "kind": "confirmed_not_applied",
                        "retryable": True,
                    }
                    assert execution.write_attempt_count == 1
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(AuditEventModel)
                            .where(AuditEventModel.event_type == "oauth.refresh_started")
                        )
                        == 2
                    )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "synthetic-concurrent-access",
                        "refresh_token": "synthetic-concurrent-refresh",
                        "expires_in": 3600,
                        "scope": " ".join(scopes),
                    },
                )

            refresh.mock(side_effect=concurrent_grant)
            concurrent = SqlAlchemyOAuthRefreshCoordinator(
                session_factory=sessions,
                cipher=cipher,
                identity=OAuthRefreshIdentity(b"x" * 32, key_version=cipher.key_version),
                clock=lambda: coordinator_now,
            )
            concurrent_provider = GoogleOAuthAdapter(
                settings.google_client_id,
                "synthetic-client-secret",
                settings.google_redirect_uri,
            )
            try:
                await read_across_committed_refresh(
                    read=lambda: workflow.execute_or_reconcile(**arguments),
                    rotate=lambda: concurrent.refresh(
                        OAuthRefreshRequest(
                            user_id=seed.user_id,
                            connection_id=seed.connection_id,
                            capability=ConnectionCapability.MAIL_SEND,
                        ),
                        concurrent_provider,
                    ),
                )
            except TrustedActionAttemptAbandoned:
                # 保留真实持久错误，让下面的业务终态断言直接暴露误转 needs_attention。
                pass
        else:
            await workflow.execute_or_reconcile(**arguments)
        await workflow.execute_or_reconcile(**arguments)
        async with sessions() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            assert execution.status == "succeeded" and execution.write_attempt_count == 2
            if fault == "concurrent_refresh":
                task = await session.get(TaskRunModel, seed.task_id)
                draft = await session.get(MailDraftModel, seed.draft_id)
                assert task.status == "succeeded" and task.error_code is None
                assert draft.status == "sent"
                assert (
                    await session.scalar(
                        select(AuditEventModel.id).where(
                            AuditEventModel.task_id == seed.task_id,
                            AuditEventModel.event_type == "tool.needs_attention",
                        )
                    )
                    is None
                )
                assert (
                    await session.scalar(
                        select(OutboxEventModel.id).where(
                            OutboxEventModel.aggregate_id == seed.task_id,
                            OutboxEventModel.topic == "tool.needs_attention",
                        )
                    )
                    is None
                )
        assert send.call_count == 2 and refresh.call_count == (
            2 if fault == "concurrent_refresh" else 1
        )
        if fault == "concurrent_refresh":
            # 实际 adapter 必须重新取当前 access；read_current 的一致旧快照不授权旧 token。
            assert bool(
                send.calls[1].request.headers["Authorization"]
                == "Bearer synthetic-concurrent-access"
            )
    finally:
        await sessions.dispose()


async def _assert_oauth_lifecycle_delivery(
    database_url: str,
    seed: "_Seed",
    *,
    resolution_topic: str,
) -> None:
    """对实际 401 结果运行 PostgreSQL relay 与 Redis 发布，验证丢失确认后的精确重投。

    Args:
        database_url: 官方入口已准入的隔离数据库。
        seed: 真实可信写测试创建的合成归属与任务标识。
        resolution_topic: 此次恢复的确认结果或人工关注事件。

    只在 Redis 已收到末条审计 ID 后注入一次异常，模拟发布响应丢失；真实 relay 必须
    持久化安全退避，新 relay 随后只重投相同审计 ID。这里不清空 Redis、不写业务状态，
    更不能通过复制审计或直接手工发布来掩盖生产 allowlist 的遗漏。
    """
    from datetime import timedelta

    from redis.asyncio import Redis
    from sqlalchemy import select

    from ai_employee.application.use_cases.outbox import OutboxRelay
    from ai_employee.infrastructure.db.models.tasks import AuditEventModel, OutboxEventModel
    from ai_employee.infrastructure.db.repositories.outbox import SqlAlchemyOutboxStore
    from ai_employee.infrastructure.db.session import build_session_factory
    from ai_employee.infrastructure.events.publisher import TaskEventPublisher
    from ai_employee.infrastructure.queue.redis_url import validate_test_redis_url
    from tests.integration.m2.test_tool_execution_claim import NOW
    from tests.integration.workers.test_outbox_dispatch import RecordingEnqueuer

    redis_url = validate_test_redis_url(os.environ["TEST_REDIS_URL"]).value
    sessions = build_session_factory(database_url)
    client = Redis.from_url(str(redis_url), decode_responses=True)
    now = NOW
    try:
        async with sessions() as session:
            audits = tuple(
                (
                    await session.scalars(
                        select(AuditEventModel)
                        .where(AuditEventModel.task_id == seed.task_id)
                        .order_by(AuditEventModel.id)
                    )
                ).all()
            )
        assert [audit.event_type for audit in audits] == [
            "tool.claimed",
            "tool.oauth_refresh_required",
            resolution_topic,
        ]
        resolution_id = audits[-1].id

        class LostPublishResponse(TaskEventPublisher):
            """完整调用生产 Redis publisher，仅把末条通知的首次响应变为未知。"""

            failed = False

            async def publish(self, *, task_id: UUID, event_id: int) -> None:
                """在真实发布之后注入一次响应丢失，使 relay 走持久失败恢复路径。"""
                await super().publish(task_id=task_id, event_id=event_id)
                if event_id == resolution_id and not self.failed:
                    self.failed = True
                    raise OSError("synthetic publish response lost")

        enqueuer = RecordingEnqueuer()

        def relay(publisher: TaskEventPublisher) -> OutboxRelay:
            """每次重建真实 relay/store，确保恢复不依赖上一个进程的内存状态。"""
            return OutboxRelay(
                store=SqlAlchemyOutboxStore(sessions),
                enqueuer=enqueuer,
                event_publisher=publisher,
                clock=lambda: now,
                claim_ttl=timedelta(seconds=60),
                retry_base=timedelta(seconds=5),
                retry_max=timedelta(seconds=300),
            )

        async with client.pubsub() as subscription:
            await subscription.subscribe(TaskEventPublisher.channel(seed.task_id))
            assert await subscription.get_message(timeout=1) is not None
            publisher = LostPublishResponse(str(redis_url))
            published = await relay(publisher).relay_once()
            messages = [
                await subscription.get_message(ignore_subscribe_messages=True, timeout=0.5)
                for _ in audits
            ]
            # 旧实现只能发布 tool.claimed；本断言直接捕获未被正常 relay 认领的事件，
            # 同时证明 Redis 上的通知精确对应生产事务分配的 AuditEvent ID。
            assert [message["data"] if message else None for message in messages] == [
                str(audit.id) for audit in audits
            ]
            assert published == 2 and publisher.failed
            assert enqueuer.task_ids == []
            async with sessions() as session:
                pending = await session.scalar(
                    select(OutboxEventModel).where(
                        OutboxEventModel.aggregate_id == seed.task_id,
                        OutboxEventModel.topic == resolution_topic,
                    )
                )
                assert pending is not None and pending.published_at is None
                assert pending.attempt_count == 1
                assert pending.last_error == "task_event_publish_failed"
                assert pending.available_at == now + timedelta(seconds=5)
            recovered_relay = relay(TaskEventPublisher(str(redis_url)))
            assert await recovered_relay.relay_once() == 0
            now += timedelta(seconds=5)
            assert await recovered_relay.relay_once() == 1
            replayed = await subscription.get_message(ignore_subscribe_messages=True, timeout=1)
            assert replayed is not None and replayed["data"] == str(resolution_id)
            assert await recovered_relay.relay_once() == 0
            assert enqueuer.task_ids == []

        async with sessions() as session:
            events = tuple(
                (
                    await session.scalars(
                        select(OutboxEventModel).where(
                            OutboxEventModel.aggregate_id == seed.task_id
                        )
                    )
                ).all()
            )
            assert len(events) == 3
            for audit in audits:
                matching = [event for event in events if event.topic == audit.event_type]
                assert len(matching) == 1 and matching[0].published_at is not None
                assert matching[0].payload == {
                    "task_id": str(seed.task_id),
                    "audit_event_id": audit.id,
                }
            assert list(
                await session.scalars(
                    select(AuditEventModel.id)
                    .where(AuditEventModel.task_id == seed.task_id)
                    .order_by(AuditEventModel.id)
                )
            ) == [audit.id for audit in audits]
    finally:
        await client.aclose()
        await sessions.dispose()


async def _hold_credential_lock_until_expiry(
    database_url: str,
    seed: "_Seed",
    *,
    caller_session: "AsyncSession",
    ready: "OAuthRefreshReady",
    boundary: str,
    invoke: "Callable[[], Awaitable[None]]",
) -> None:
    """让真实 resolver 等待一个只读凭据锁，直到数据库时间越过唯一被测截止点。

    Args:
        database_url: 官方测试入口准入的隔离 PostgreSQL。
        seed: 同一次 401 的任务和连接标识。
        caller_session: 应用事务拥有的 resolver 会话，只读取其 backend PID。
        ready: 唯一刷新 writer 已提交并返回的完整 current-readiness。
        boundary: 租约或 access expiry；二者只缩短本次要验证的一项。
        invoke: 不含 Fake 时间或 SQL 替身的真实 resolver 调用。

    lease 场景先在独立 fixture 事务设置短租约；access 场景使用真实刷新生成的短 expiry。
    随后的阻塞事务仅持 access/refresh 行锁，绝不更新行。通过 pg_stat_activity 和
    pg_blocking_pids 双重确认等待，再按 PostgreSQL 时钟释放，避免固定 sleep 猜测交错。
    """
    import asyncio
    from contextlib import suppress
    from datetime import timedelta

    from sqlalchemy import func, select, update

    from ai_employee.infrastructure.db.models.sources import EncryptedCredentialModel
    from ai_employee.infrastructure.db.models.tasks import TaskRunModel
    from ai_employee.infrastructure.db.session import build_session_factory
    from tests.integration.m2.test_tool_execution_claim import _wait_for_postgres_lock_wait

    sessions = build_session_factory(database_url)
    pending: asyncio.Task[None] | None = None
    try:
        if boundary == "lease_lock_expiry":
            async with sessions.begin() as session:
                cutoff = await session.scalar(select(func.clock_timestamp())) + timedelta(seconds=2)
                await session.execute(
                    update(TaskRunModel)
                    .where(TaskRunModel.id == seed.task_id, TaskRunModel.user_id == seed.user_id)
                    .values(lease_expires_at=cutoff)
                )
        else:
            cutoff = ready.snapshot.access.token_expires_at
            assert cutoff is not None
        caller_pid = await caller_session.scalar(select(func.pg_backend_pid()))
        assert isinstance(caller_pid, int)
        async with sessions.begin() as blocker:
            blocker_pid = await blocker.scalar(select(func.pg_backend_pid()))
            locked_id = await blocker.scalar(
                select(EncryptedCredentialModel.id)
                .where(
                    EncryptedCredentialModel.user_id == seed.user_id,
                    EncryptedCredentialModel.connection_id == seed.connection_id,
                    EncryptedCredentialModel.credential_kind
                    == ("access_token" if boundary == "lease_lock_expiry" else "refresh_token"),
                )
                .with_for_update()
            )
            assert locked_id is not None
            pending = asyncio.create_task(invoke())
            await _wait_for_postgres_lock_wait(database_url, caller_pid)
            async with sessions() as observer:
                assert blocker_pid in await observer.scalar(
                    select(func.pg_blocking_pids(caller_pid))
                )
                assert await observer.scalar(select(func.clock_timestamp())) < cutoff
                async with asyncio.timeout(5):
                    while await observer.scalar(select(func.clock_timestamp())) <= cutoff:
                        await asyncio.sleep(0.01)
            assert not pending.done()
        # 行锁到这里才释放。snapshot 全字节保持不变，正确实现必须在 mutation 前
        # 再读 PostgreSQL 时钟，并让异常回到应用事务边界以整体回滚。
        await pending
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        await sessions.dispose()
