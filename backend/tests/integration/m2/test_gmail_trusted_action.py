"""验证 Gmail adapter 接入固定可信动作 registry 后的幂等核对边界。"""

import os
from uuid import UUID

import httpx
import pytest
import respx

from ai_employee.application.ports.trusted_actions import ExecutionReference
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.mail_actions import MailMode, MailSendCommand
from ai_employee.integrations.google.gmail_write import GMAIL_SEND_URL, GmailWriteAdapter
from ai_employee.integrations.registry import ProviderAdapterRegistry

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
    ["none", "unknown", "cas_miss", "proof_crash", "revoked_after_refresh", "budget_exhausted"],
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

    from sqlalchemy import select

    from ai_employee.application.use_cases.trusted_actions import (
        TrustedActionAttemptAbandoned,
        TrustedActionExecutionUseCase,
    )
    from ai_employee.domain.errors import TransientProviderError
    from ai_employee.infrastructure.db.models.sources import (
        ConnectionCapabilityModel,
        EncryptedCredentialModel,
        OAuthConnectionModel,
    )
    from ai_employee.infrastructure.db.models.tasks import AuditEventModel, ToolExecutionModel
    from ai_employee.infrastructure.db.repositories.trusted_actions import (
        SqlAlchemyTrustedActionRepository,
        SqlAlchemyTrustedActionRepositoryFactory,
    )
    from ai_employee.infrastructure.db.session import build_session_factory
    from ai_employee.infrastructure.security.encryption import AeadCipher
    from ai_employee.integrations import registry as registry_module
    from ai_employee.workers.trusted_actions import build_worker_trusted_action_registry
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
    # 仅通过真实协调器已公开的 Clock 参数替换时间，Secret/身份构造和生产 registry
    # 仍使用完整实现，避免 credential expiry 与真实系统日期绑定。
    monkeypatch.setattr(
        registry_module,
        "SqlAlchemyOAuthRefreshCoordinator",
        partial(registry_module.SqlAlchemyOAuthRefreshCoordinator, clock=lambda: NOW),
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
            if fault == "unknown":
                raise httpx.ReadTimeout("synthetic lost OAuth response", request=request)
            if fault == "cas_miss":
                async with sessions.begin() as session:
                    connection = await session.get(OAuthConnectionModel, seed.connection_id)
                    connection.authorization_generation += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-ready-access",
                    "expires_in": 3600,
                    "scope": " ".join(scopes),
                },
            )

        refresh = respx_mock.post("https://oauth2.googleapis.com/token").mock(
            side_effect=inspect_durable_rejection
        )
        if fault == "proof_crash":
            original = SqlAlchemyTrustedActionRepository.resolve_oauth_write_retry
            crashed = False

            async def crash_once(self, **values):
                """真实 refresh 已确认后，在授予第二次写资格之前模拟进程中断。"""
                nonlocal crashed
                if not crashed:
                    crashed = True
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
        if fault in {"unknown", "cas_miss"}:
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
        await workflow.execute_or_reconcile(**arguments)
        await workflow.execute_or_reconcile(**arguments)
        assert send.call_count == 2 and refresh.call_count == 1
        async with sessions() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            assert execution.status == "succeeded" and execution.write_attempt_count == 2
    finally:
        await sessions.dispose()
