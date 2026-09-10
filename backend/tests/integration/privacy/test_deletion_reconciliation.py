"""验证 inactive 删除只读核对的持久一次资格与供应商无关绑定。

所有对象与响应均为合成值；阶段故障让断言观察删除前的执行事实，绝不访问真实账号。
"""

from dataclasses import replace
from datetime import timedelta
from typing import Literal

import httpx
import pytest
from sqlalchemy import delete, select, update

from ai_employee.application.ports.trusted_actions import ProviderWriteOutcome
from ai_employee.application.use_cases.privacy import (
    PrivacyDeletionBinding,
    PrivacyReconciliationTarget,
)
from ai_employee.application.use_cases.task_execution import TaskLeaseMode
from ai_employee.domain.actions import ProviderWriteOutcomeKind
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    MailDraftModel,
    MailDraftVersionModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.action_views import SqlAlchemyActionViewRepository
from ai_employee.infrastructure.db.repositories.privacy_reconciliation import (
    SqlAlchemyPrivacyReconciliationStore,
)
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepository,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.privacy import PrivacyProviderReader
from ai_employee.workers.retention import RetentionCleanupWorker
from tests.integration.privacy.test_all_data_deletion import (
    BARRIER_NOW,
    BARRIER_TASK_ID,
    BARRIER_USER_ID,
    _barrier_lease,
    _DeletionClock,
    _PhaseCrashWorker,
    _seed_barrier_task,
)
from tests.integration.retention.test_m2_action_retention import (
    LifecycleSeed,
    seed_lifecycle_action,
)


async def _readable_claim(
    sessions, cipher: AeadCipher, *, provider: str, kind: Literal["mail", "calendar"]
) -> LifecycleSeed:
    """合成当前access/read能力及精确provider绑定；始终保留refresh以检测隐式降级。"""
    seed = await seed_lifecycle_action(
        sessions, kind=kind, execution_status="executing", user_id=BARRIER_USER_ID
    )
    async with sessions.begin() as session:
        if provider == "microsoft":
            await session.execute(
                update(OAuthConnectionModel)
                .where(OAuthConnectionModel.id == seed.connection_id)
                .values(
                    provider=provider,
                    provider_tenant_id="synthetic-tenant",
                    account_type="work_school",
                )
            )
            await session.execute(
                update(ToolExecutionModel)
                .where(ToolExecutionModel.id == seed.execution_id)
                .values(provider=provider)
            )
        for token_kind in ("access_token", "refresh_token"):
            encrypted = cipher.encrypt(
                b"synthetic-token",
                f"{seed.user_id}:{seed.connection_id}:{token_kind}".encode("ascii"),
            )
            session.add(
                EncryptedCredentialModel(
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    credential_kind=token_kind,
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    token_expires_at=BARRIER_NOW + timedelta(minutes=5),
                )
            )
        session.add(
            ConnectionCapabilityModel(
                user_id=seed.user_id,
                connection_id=seed.connection_id,
                capability="mail.read" if kind == "mail" else "calendar.read",
                status="enabled",
                actual_scopes=[],
            )
        )
    return seed


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "microsoft"])
@pytest.mark.parametrize(
    "negative",
    [
        "no_locator",
        "unsafe_locator",
        "missing_access",
        "expired_access",
        "disabled_capability",
        "missing_capability",
        "disconnected",
        "provider_mismatch",
        "wrong_hash",
        "wrong_version",
        "missing_version",
    ],
)
async def test_task27e_privacy_reader_missing_facts_are_zero_network_unknown(
    database_url: str,
    provider: str,
    negative: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """定位、能力、access及冻结绑定任一不足均不访问网络，不借refresh或解密审批补全。"""
    sessions, cipher = build_session_factory(database_url), AeadCipher(b"p" * 32)
    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        seed = await _readable_claim(sessions, cipher, provider=provider, kind="mail")
        async with sessions.begin() as session:
            access = (
                EncryptedCredentialModel.connection_id == seed.connection_id,
                EncryptedCredentialModel.credential_kind == "access_token",
            )
            if negative in ("no_locator", "unsafe_locator"):
                await session.execute(
                    update(ToolExecutionModel)
                    .where(ToolExecutionModel.id == seed.execution_id)
                    .values(provider_resource_id=None if negative == "no_locator" else "..")
                )
            elif negative == "missing_access":
                await session.execute(delete(EncryptedCredentialModel).where(*access))
            elif negative == "expired_access":
                await session.execute(
                    update(EncryptedCredentialModel)
                    .where(*access)
                    .values(token_expires_at=BARRIER_NOW)
                )
            elif negative == "disabled_capability":
                await session.execute(
                    update(ConnectionCapabilityModel)
                    .where(ConnectionCapabilityModel.connection_id == seed.connection_id)
                    .values(status="action_required")
                )
            elif negative == "missing_capability":
                await session.execute(
                    delete(ConnectionCapabilityModel).where(
                        ConnectionCapabilityModel.connection_id == seed.connection_id
                    )
                )
            elif negative == "disconnected":
                await session.execute(
                    update(OAuthConnectionModel)
                    .where(OAuthConnectionModel.id == seed.connection_id)
                    .values(status="disconnected")
                )
            elif negative == "provider_mismatch":
                await session.execute(
                    update(ToolExecutionModel)
                    .where(ToolExecutionModel.id == seed.execution_id)
                    .values(provider="microsoft" if provider == "google" else "google")
                )
            elif negative == "wrong_hash":
                await session.execute(
                    update(ApprovalRequestModel)
                    .where(ApprovalRequestModel.id == seed.approval_id)
                    .values(payload_hash="e" * 64)
                )
            elif negative == "wrong_version":
                await session.execute(
                    update(MailDraftModel)
                    .where(MailDraftModel.id == seed.action_id)
                    .values(current_version=2)
                )
            else:
                await session.execute(
                    delete(MailDraftVersionModel).where(MailDraftVersionModel.id == seed.content_id)
                )

        def forbidden_http(request: httpx.Request) -> httpx.Response:
            """没有正确本地事实时，任意OAuth/供应商调用都是失败，HTTP地址不进入断言输出。"""
            del request
            raise AssertionError("privacy missing facts must perform zero network calls")

        decrypt = cipher.decrypt

        def access_only(value, aad: bytes):
            """若误读refresh或可信命令立即失败，不能把解密失败伪装为正常UNKNOWN。"""
            assert aad == f"{seed.user_id}:{seed.connection_id}:access_token".encode("ascii")
            return decrypt(value, aad)

        monkeypatch.setattr(cipher, "decrypt", access_only)
        reader = _ObservedPrivacyReader(
            store=SqlAlchemyPrivacyReconciliationStore(sessions),
            cipher=cipher,
            clock=_DeletionClock(),
            transport=httpx.MockTransport(forbidden_http),
        )
        reader.outcomes = []
        worker = _PhaseCrashWorker(sessions, "reconciliation")
        worker._privacy_reader = reader
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(_barrier_lease())
        invalid_binding = negative in {
            "provider_mismatch",
            "wrong_hash",
            "wrong_version",
            "missing_version",
        }
        assert len(reader.outcomes) == (0 if invalid_binding else 1)
        assert all(
            outcome.kind is ProviderWriteOutcomeKind.UNKNOWN and not outcome.retryable
            for outcome in reader.outcomes
        )
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(_barrier_lease(recovery=True))
        assert len(reader.outcomes) == (0 if invalid_binding else 1)
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "microsoft"])
@pytest.mark.parametrize("operation", ["update", "restore"])
async def test_task27e_privacy_calendar_existing_target_get_uses_bound_scope(
    database_url: str,
    provider: str,
    operation: str,
) -> None:
    """update/restore只读冻结目标日历内的目标ID，不使用执行返回的其他资源或缺省日历。"""
    sessions, cipher = build_session_factory(database_url), AeadCipher(b"p" * 32)
    calls: list[str] = []
    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        seed = await _readable_claim(sessions, cipher, provider=provider, kind="calendar")
        async with sessions.begin() as session:
            execution = await session.get(ToolExecutionModel, seed.execution_id)
            assert execution is not None
            action = f"calendar.{operation}"
            execution.tool_name = action
            execution.idempotency_key = (
                f"{action}:{seed.task_id}:{seed.approval_id}:1:{execution.operation_id}"
            )
            await session.execute(
                update(CalendarChangeProposalModel)
                .where(CalendarChangeProposalModel.id == seed.action_id)
                .values(
                    operation_kind=operation,
                    calendar_id="synthetic-calendar",
                    target_event_id="synthetic-target",
                )
            )
            await session.execute(
                update(ApprovalRequestModel)
                .where(ApprovalRequestModel.id == seed.approval_id)
                .values(action=action, schema_version=f"calendar_{operation}.v1")
            )

        def handler(request: httpx.Request) -> httpx.Response:
            """只接受当前目标的单次GET，响应体仍禁止被读取。"""
            assert request.method == "GET"
            assert request.url.path.endswith(
                "/calendars/synthetic-calendar/events/synthetic-target"
            )
            calls.append(request.method)
            return httpx.Response(200, stream=_UnreadResponse())

        reader = _ObservedPrivacyReader(
            store=SqlAlchemyPrivacyReconciliationStore(sessions),
            cipher=cipher,
            clock=_DeletionClock(),
            transport=httpx.MockTransport(handler),
        )
        reader.outcomes = []
        worker = _PhaseCrashWorker(sessions, "reconciliation")
        worker._privacy_reader = reader
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(_barrier_lease())
        assert calls == ["GET"] and reader.outcomes[0].kind is ProviderWriteOutcomeKind.UNKNOWN
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_task27e_privacy_marker_survives_stale_retention_and_late_normal_outcome(
    database_url: str,
) -> None:
    """屏障前的retention用户快照和正常provider结果都不能覆盖已提交的一次读取资格。"""
    sessions = build_session_factory(database_url)
    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        seed = await seed_lifecycle_action(
            sessions, execution_status="executing", user_id=BARRIER_USER_ID
        )
        other = await seed_lifecycle_action(sessions, execution_status="executing")
        cipher = ActionPayloadCipher.from_key(b"a" * 32)
        async with sessions.begin() as session:
            stale_user = await session.get(UserModel, seed.user_id)
            execution = await session.get(ToolExecutionModel, seed.execution_id)
            assert (
                stale_user is not None
                and execution is not None
                and execution.operation_id is not None
            )
            before = await SqlAlchemyTrustedActionRepository(session, cipher).load_dispatch(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=execution.operation_id,
            )
            assert before is not None
        reader = _RecordingPrivacyReader()
        worker = _PhaseCrashWorker(sessions, "reconciliation")
        worker._privacy_reader = reader
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(_barrier_lease())
        await RetentionCleanupWorker(sessions)._clean_user(
            stale_user, now=BARRIER_NOW, batch_size=1
        )
        async with sessions.begin() as session:
            with pytest.raises(StateConflictError):
                await SqlAlchemyTrustedActionRepository(session, cipher).persist_provider_outcome(
                    snapshot=before,
                    outcome=ProviderWriteOutcome(
                        kind=ProviderWriteOutcomeKind.CONFIRMED_APPLIED,
                        retryable=False,
                        retry_after_seconds=None,
                        provider_resource_id="synthetic-late-result",
                        provider_request_id=None,
                        correlation_id="synthetic-late-result",
                        provider_url=None,
                        error_code=None,
                    ),
                    completed_at=BARRIER_NOW,
                    from_reconciliation=False,
                    may_retry_write=False,
                    lease_owner="synthetic-worker",
                )
        binding = PrivacyDeletionBinding(
            BARRIER_USER_ID, BARRIER_TASK_ID, "synthetic-request-1", "worker-before-barrier"
        )
        with pytest.raises(StateConflictError):
            await SqlAlchemyPrivacyReconciliationStore(sessions).qualify(
                binding=binding, task_id=other.task_id, now=BARRIER_NOW
            )
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(_barrier_lease(recovery=True))
        assert reader.calls == 1
        async with sessions() as session:
            execution = await session.get(ToolExecutionModel, seed.execution_id)
            assert (
                execution is not None and execution.error_code == "privacy_reconciliation_started"
            )
            assert execution.provider_resource_id == "synthetic-resource"
            other_execution = await session.get(ToolExecutionModel, other.execution_id)
            assert other_execution is not None and other_execution.status == "executing"
    finally:
        await sessions.dispose()


class _RecordingPrivacyReader:
    """记录内容无关的调用资格，允许在数据库测试中独立观察提交顺序。"""

    def __init__(self) -> None:
        """每次真实只读调用只记次数，不保存正文或凭据。"""
        self.calls = 0

    async def reconcile(self, *, binding: object, target: object) -> None:
        """只检查当前轮传入投影而不依赖新的实现类型，RED 针对旧 no-op 行为。"""
        del binding, target
        self.calls += 1


class _ObservedPrivacyReader(PrivacyProviderReader):
    """保留生产装配和 HTTP，只向测试暴露已经规范化的无内容结果。"""

    outcomes: list[ProviderWriteOutcome]

    async def reconcile(
        self, *, binding: PrivacyDeletionBinding, target: PrivacyReconciliationTarget
    ) -> ProviderWriteOutcome:
        """观察真实固定 reader 的结果；不改 route、access 准入或供应商语义。"""
        outcome = await super().reconcile(binding=binding, target=target)
        self.outcomes.append(outcome)
        return outcome


class _UnreadResponse(httpx.AsyncByteStream):
    """响应体若被消费立即失败，防止 fields 被供应商忽略时读入完整敏感响应。"""

    async def __aiter__(self):
        """本 reader 只能读状态和关闭流，不能调用 JSON 或读取响应字节。"""
        raise AssertionError("privacy GET must not read response content")
        yield b""  # pragma: no cover -- 异步生成器协议要求。


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "microsoft"])
@pytest.mark.parametrize("kind", ["mail", "calendar"])
async def test_task27e_privacy_qualifies_one_read_before_crash_and_recovery(
    database_url: str,
    provider: str,
    kind: Literal["mail", "calendar"],
) -> None:
    """单次读资格先提交；恢复和 ACK 重入不能再读，也不覆写已知 provider 结果。"""
    sessions = build_session_factory(database_url)
    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        seed = await seed_lifecycle_action(
            sessions,
            kind=kind,
            binding="event" if kind == "calendar" else "none",
            execution_status="executing",
            user_id=BARRIER_USER_ID,
        )
        other = await seed_lifecycle_action(sessions, execution_status="executing")
        async with sessions.begin() as session:
            if provider == "microsoft":
                await session.execute(
                    update(OAuthConnectionModel)
                    .where(
                        OAuthConnectionModel.id == seed.connection_id,
                    )
                    .values(
                        provider=provider,
                        provider_tenant_id="synthetic-tenant",
                        account_type="work_school",
                    )
                )
                await session.execute(
                    update(ToolExecutionModel)
                    .where(
                        ToolExecutionModel.id == seed.execution_id,
                    )
                    .values(provider=provider)
                )
        reader = _RecordingPrivacyReader()
        worker = _PhaseCrashWorker(sessions, "reconciliation")
        worker._privacy_reader = reader
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(_barrier_lease())
        assert reader.calls == 1
        async with sessions.begin() as session:
            execution = await session.get(ToolExecutionModel, seed.execution_id)
            task = await session.get(TaskRunModel, seed.task_id)
            assert execution is not None and task is not None
            assert execution.status == "needs_attention"
            assert execution.error_code == "privacy_reconciliation_started"
            assert execution.result_summary == {"check_url": "https://example.test/check"}
            assert execution.write_attempt_count == 1
            assert execution.reconciliation_attempt_count == 3
            assert execution.provider_resource_id == "synthetic-resource"
            assert task.lease_owner is None and task.lease_expires_at is None
            untouched = await session.get(ToolExecutionModel, other.execution_id)
            assert untouched is not None and untouched.status == "executing"
            winner = await session.get(TaskRunModel, BARRIER_TASK_ID)
            assert winner is not None
            winner.lease_expires_at = BARRIER_NOW - timedelta(seconds=1)
        lease = await SqlAlchemyTaskExecutionStore(sessions).acquire(
            task_id=BARRIER_TASK_ID,
            lease_owner="recovered",
            now=BARRIER_NOW,
            lease_expires_at=BARRIER_NOW + timedelta(minutes=5),
        )
        assert lease is not None and lease.lease_mode is TaskLeaseMode.INACTIVE_ALL_DATA_RECOVERY
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(lease)
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(replace(lease))
        assert reader.calls == 1
        async with sessions() as session:
            assert (await session.scalars(select(ToolExecutionModel.id))).all()
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "microsoft"])
@pytest.mark.parametrize("kind", ["mail", "calendar"])
@pytest.mark.parametrize("status_code", [200, 404, 401, 503])
async def test_task27e_privacy_real_reader_get_is_once_bounded_and_always_unknown(
    database_url: str,
    provider: str,
    kind: Literal["mail", "calendar"],
    status_code: int,
) -> None:
    """两供应商真实 reader 在已提交资格后只发一个最小 GET，任何状态都无写重试证明。"""
    sessions = build_session_factory(database_url)
    cipher = AeadCipher(b"p" * 32)
    requests: list[tuple[str, str]] = []
    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        seed = await seed_lifecycle_action(
            sessions, kind=kind, execution_status="executing", user_id=BARRIER_USER_ID
        )
        async with sessions.begin() as session:
            if provider == "microsoft":
                await session.execute(
                    update(OAuthConnectionModel)
                    .where(OAuthConnectionModel.id == seed.connection_id)
                    .values(
                        provider=provider,
                        provider_tenant_id="synthetic-tenant",
                        account_type="work_school",
                    )
                )
                await session.execute(
                    update(ToolExecutionModel)
                    .where(ToolExecutionModel.id == seed.execution_id)
                    .values(provider=provider)
                )
            encrypted = cipher.encrypt(
                b"synthetic-access",
                f"{seed.user_id}:{seed.connection_id}:access_token".encode("ascii"),
            )
            session.add(
                EncryptedCredentialModel(
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    credential_kind="access_token",
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    token_expires_at=BARRIER_NOW + timedelta(minutes=5),
                )
            )
            session.add(
                ConnectionCapabilityModel(
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    capability="mail.read" if kind == "mail" else "calendar.read",
                    status="enabled",
                    actual_scopes=[],
                )
            )

        async def handler(request: httpx.Request) -> httpx.Response:
            """HTTP mock 内用另一会话验证资格已经提交；任何 POST/分页/授权请求都会失败。"""
            assert request.method == "GET"
            assert request.url.host in {
                "gmail.googleapis.com",
                "www.googleapis.com",
                "graph.microsoft.com",
            }
            assert request.url.params.get("fields") in {"id", "id,etag"} or request.url.params.get(
                "$select"
            ) in {"id", "id,changeKey"}
            assert "synthetic-resource" in request.url.path
            async with sessions() as session:
                execution = await session.get(ToolExecutionModel, seed.execution_id)
                assert (
                    execution is not None
                    and execution.error_code == "privacy_reconciliation_started"
                )
            requests.append((request.method, request.url.host))
            return httpx.Response(status_code, stream=_UnreadResponse())

        reader = _ObservedPrivacyReader(
            store=SqlAlchemyPrivacyReconciliationStore(sessions),
            cipher=cipher,
            clock=_DeletionClock(),
            transport=httpx.MockTransport(handler),
        )
        reader.outcomes = []
        worker = _PhaseCrashWorker(sessions, "reconciliation")
        worker._privacy_reader = reader
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(_barrier_lease())
        assert len(requests) == 1
        assert len(reader.outcomes) == 1
        outcome = reader.outcomes[0]
        assert outcome.kind is ProviderWriteOutcomeKind.UNKNOWN
        assert outcome.retryable is False and outcome.retry_after_seconds is None
        assert outcome.provider_resource_id is None and outcome.provider_request_id is None
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(_barrier_lease(recovery=True))
        assert len(requests) == 1
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_task27e_late_authenticated_reopen_cannot_erase_privacy_read_marker(
    database_url: str,
) -> None:
    """认证先发生、mutation 后到达时，锁内 inactive 屏障仍须阻止重新核对覆盖 marker。"""
    sessions = build_session_factory(database_url)
    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        seed = await seed_lifecycle_action(
            sessions, execution_status="needs_attention", user_id=BARRIER_USER_ID
        )
        async with sessions.begin() as session:
            await session.execute(
                update(MailDraftModel)
                .where(MailDraftModel.id == seed.action_id)
                .values(status="needs_attention")
            )
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == seed.task_id)
                .values(status="needs_attention")
            )
        # 这个身份代表已经通过 Cookie 认证的请求；实际 mutation 故意延迟到屏障/marker 提交后。
        authenticated_user_id = seed.user_id
        reader = _RecordingPrivacyReader()
        worker = _PhaseCrashWorker(sessions, "reconciliation")
        worker._privacy_reader = reader
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(_barrier_lease())
        assert reader.calls == 1
        async with sessions.begin() as session:
            with pytest.raises(StateConflictError):
                await SqlAlchemyActionViewRepository(session).request_reconciliation(
                    user_id=authenticated_user_id,
                    task_id=seed.task_id,
                    requested_at=BARRIER_NOW,
                )
        async with sessions() as session:
            execution = await session.get(ToolExecutionModel, seed.execution_id)
            assert (
                execution is not None and execution.error_code == "privacy_reconciliation_started"
            )
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(_barrier_lease(recovery=True))
        assert reader.calls == 1
    finally:
        await sessions.dispose()
