"""以真实 app/retention 会话复现 OAuth 与可信执行的 user/OAuth 锁顺序。

只暂停已经执行的生产锁入口，或真实 FK INSERT 前的原调用边界；不替换锁、审批策略、
事务结果或供应商重试判断。失败仅保留 SQLSTATE、稳定错误码及 PID，禁止回显 SQL 参数。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
from ai_employee.application.ports.oauth_refresh import OAuthRefreshError, OAuthRefreshRequest
from ai_employee.application.ports.trusted_actions import RequestStartDisposition
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
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, ToolExecutionModel
from ai_employee.infrastructure.db.repositories import oauth_refresh_coordinator
from ai_employee.infrastructure.db.repositories.connections import (
    SqlAlchemyConnectionStore,
    SqlAlchemyConnectionStoreFactory,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepository,
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.workers import retention as retention_module
from tests.integration.m2.test_credential_rotation_repository import (
    FakeRefreshProvider,
    RecoveryClock,
    RecoveryOAuthAdapter,
    token_response,
)
from tests.integration.m2.test_tool_execution_claim import (
    ACTION_CIPHER,
    NOW,
    _load_dispatch_snapshot_for_test,
    _RecordingAdapter,
    _Seed,
    _seed_action,
    _settings,
    _workflow,
)
from tests.integration.m2.test_tool_execution_deletion_races import (
    _backend_pid,
    _prove_exact_blocker,
)
from tests.integration.retention.oauth_cases import _assert_privileges


@dataclass(frozen=True)
class _TransactionOutcome:
    """只允许稳定状态进入 pytest 断言，不保存异常对象、SQL 或凭据。"""

    phase: str
    result: str
    sqlstate: str | None = None


class _ActionRefreshProvider(FakeRefreshProvider):
    """让网络 Fake 声明与同连接 OAuth adapter 相同的四项 scope，不放宽生产校验。"""

    def __init__(self, oauth: RecoveryOAuthAdapter) -> None:
        """复用既有合成响应和网络计数，明确覆盖已启用写能力的完整 scope。"""
        super().__init__(replace(token_response(), granted_scopes=oauth.response.granted_scopes))
        self._oauth = oauth

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """仅声明 Fake 的真实能力映射，required/permitted 判断仍由 coordinator 完成。"""
        return self._oauth.scopes_for(capabilities)


class _LockRace:
    """暂停持锁者，证明另一生产事务确实等待该 PID，再让 PostgreSQL 处理交错。"""

    def __init__(self) -> None:
        """每个场景独占事件与 PID；测试结束必须释放所有等待者。"""
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.waiter_ready = asyncio.Event()
        self.holder_pid: int | None = None
        self.waiter_pid: int | None = None

    async def hold(self, session: AsyncSession) -> None:
        """只在第一次原锁入口完成后暂停；后续同事务检查保持原样。"""
        if self.holder_pid is None:
            self.holder_pid = await _backend_pid(session)
            self.held.set()
            await self.release.wait()

    async def observe_waiter(self, session: AsyncSession) -> None:
        """在真实 claim/request-start 的原方法前记录物理会话，绝不先代拿 user 锁。"""
        if self.waiter_pid is None:
            self.waiter_pid = await _backend_pid(session)
            self.waiter_ready.set()

    async def run(
        self,
        *,
        sessions: ManagedAsyncSessionMaker,
        holder: Callable[[], Awaitable[str]],
        waiter: Callable[[], Awaitable[str]],
        scenario: str,
        phase: str,
        capsys: pytest.CaptureFixture[str],
    ) -> tuple[_TransactionOutcome, _TransactionOutcome]:
        """对精确阻塞边作新鲜查询；真实死锁作为安全失败结果返回，不自动重试事务。"""
        jobs: list[asyncio.Task[_TransactionOutcome]] = []

        async def observe(
            name: str, operation: Callable[[], Awaitable[str]]
        ) -> _TransactionOutcome:
            """异常对象可能带敏感绑定参数，因此仅提取 SQLSTATE 或领域稳定分类码。"""
            try:
                return _TransactionOutcome(name, await operation())
            except DBAPIError as error:
                code = getattr(error.orig, "sqlstate", None)
                return _TransactionOutcome(name, "database_error", code)
            except (DomainError, OAuthRefreshError) as error:
                return _TransactionOutcome(name, error.error_code)

        try:
            async with asyncio.timeout(20):
                jobs.append(asyncio.create_task(observe(scenario, holder)))
                held_wait = asyncio.create_task(self.held.wait())
                try:
                    await asyncio.wait((jobs[0], held_wait), return_when=asyncio.FIRST_COMPLETED)
                    assert self.held.is_set(), await jobs[0]
                finally:
                    held_wait.cancel()
                    await asyncio.gather(held_wait, return_exceptions=True)
                jobs.append(asyncio.create_task(observe(phase, waiter)))
                await self.waiter_ready.wait()
                assert self.holder_pid is not None and self.waiter_pid is not None
                await _prove_exact_blocker(
                    sessions, holder_pid=self.holder_pid, waiter_pid=self.waiter_pid
                )
                assert all(not job.done() for job in jobs)
                self.release.set()
                results = await asyncio.gather(*jobs)
            # 即使测试通过也留下本轮实际 PID/状态证据；没有参数、账户或异常 repr。
            with capsys.disabled():
                print(
                    f"QI4_LOCK_RACE scenario={scenario} phase={phase} "
                    f"holder_pid={self.holder_pid} waiter_pid={self.waiter_pid} "
                    f"exact_blocker=1 outcomes={results}"
                )
            assert all(result.sqlstate is None for result in results), results
            assert all(result.result != "database_error" for result in results), results
            return results[0], results[1]
        finally:
            self.release.set()
            await asyncio.gather(*jobs, return_exceptions=True)


async def _attach_oauth(
    sessions: ManagedAsyncSessionMaker, seed: _Seed
) -> tuple[ConnectionsUseCase, RecoveryOAuthAdapter]:
    """给已批准动作的同一连接补完整 OAuth 双行及四项真实 scope 能力。

    保留原审批/命令/动作 fixture；仅 OAuth 网络由既有 adapter 替换。所有能力行预先存在，
    渐进 INSERT 的竞争可以精确归因到 OAuthAttempt FK，而不是新增 capability 的 FK。
    """
    cipher = AeadCipher(bytes(range(32)), key_version=1)
    adapter = RecoveryOAuthAdapter(token_response(), account_id=seed.provider_account_id)
    adapter.response = replace(
        adapter.response,
        refresh_token=uuid4().hex,
        granted_scopes=adapter.scopes_for(frozenset(ConnectionCapability)),
    )
    async with sessions.begin() as session:
        connection = await session.get(OAuthConnectionModel, seed.connection_id)
        assert connection is not None
        connection.scopes = sorted(adapter.response.granted_scopes)
        for capability in ConnectionCapability:
            row = await session.scalar(
                select(ConnectionCapabilityModel).where(
                    ConnectionCapabilityModel.user_id == seed.user_id,
                    ConnectionCapabilityModel.connection_id == seed.connection_id,
                    ConnectionCapabilityModel.capability == capability.value,
                )
            )
            if row is None:
                session.add(
                    ConnectionCapabilityModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        capability=capability.value,
                        status="disabled",
                        actual_scopes=[],
                    )
                )
            else:
                row.actual_scopes = sorted(adapter.scopes_for(frozenset({capability})))
        assert adapter.response.refresh_token is not None
        for kind, plaintext in (
            ("access_token", adapter.response.access_token),
            ("refresh_token", adapter.response.refresh_token),
        ):
            encrypted = cipher.encrypt(
                plaintext.encode("utf-8"),
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
                    token_expires_at=NOW + timedelta(minutes=5) if kind == "access_token" else None,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    identity = OAuthRefreshIdentity(bytes(range(32)), key_version=1)
    coordinator = oauth_refresh_coordinator.SqlAlchemyOAuthRefreshCoordinator(
        session_factory=sessions, cipher=cipher, identity=identity, clock=lambda: NOW
    )
    return (
        ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions),
            cipher,
            {"google": adapter},
            RecoveryClock(),
            identity=identity,
            coordinator=coordinator,
        ),
        adapter,
    )


def _pause_store_method(
    monkeypatch: pytest.MonkeyPatch, race: _LockRace, method_name: str, *, before: bool = False
) -> None:
    """包装指定真实仓储方法，原输入/输出/查询全部保留；仅暂停其已持锁边界。"""
    original = getattr(SqlAlchemyConnectionStore, method_name)

    async def observed(store: SqlAlchemyConnectionStore, **kwargs: object) -> object:
        """before 专用于已有连接锁的 attempt INSERT；callback 在原连接锁完成后暂停。"""
        if before:
            await race.hold(store._session)
        result = await original(store, **kwargs)
        if not before:
            await race.hold(store._session)
        return result

    monkeypatch.setattr(SqlAlchemyConnectionStore, method_name, observed)


async def _seed_disconnected_residue(sessions: ManagedAsyncSessionMaker, seed: _Seed) -> UUID:
    """同用户另建断开的连接、AEAD 双行及游标残留；动作仍归原 connected 连接。"""
    connection_id = uuid4()
    async with sessions.begin() as session:
        session.add(
            OAuthConnectionModel(
                id=connection_id,
                user_id=seed.user_id,
                provider="google",
                provider_account_id=str(connection_id),
                account_email="disconnected@example.test",
                status="disconnected",
                scopes=[],
            )
        )
        await session.flush()
        cipher = AeadCipher(bytes(range(32)), key_version=1)
        for kind in ("access_token", "refresh_token"):
            encrypted = cipher.encrypt(
                uuid4().bytes, f"{seed.user_id}:{connection_id}:{kind}".encode()
            )
            session.add(
                EncryptedCredentialModel(
                    user_id=seed.user_id,
                    connection_id=connection_id,
                    credential_kind=kind,
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    token_expires_at=NOW if kind == "access_token" else None,
                )
            )
        session.add(
            SyncCursorModel(
                connection_id=connection_id,
                resource_kind="mail",
                scope_key="mailbox",
                last_success_at=NOW,
                last_attempt_at=NOW,
            )
        )
    return connection_id


async def assert_oauth_execution_lock_order(
    *,
    app_url: str,
    retention_url: str,
    scenario: str,
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """真实入口的锁交错应串行结束；回调一次、grant 一次、可信写零次且持久结果准确。"""
    sessions, retention = build_session_factory(app_url), build_session_factory(retention_url)
    seed, writer, race = _Seed(), _RecordingAdapter(), _LockRace()
    workflow = _workflow(
        app_url,
        writer,
        settings=_settings(tenant=seed.provider_tenant_id, account=seed.provider_account_id),
    )
    try:
        await _assert_privileges(sessions, retention)
        payload_hash = await _seed_action(app_url, seed)
        use_case, oauth = await _attach_oauth(sessions, seed)

        async def claim() -> str:
            """完整生产 claim 保留精确哈希、审批、租约及三层真实写策略。"""
            await workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
            return "claimed"

        execution = claim
        if phase == "request_start":
            await claim()
            snapshot = await _load_dispatch_snapshot_for_test(app_url, seed)

            async def request_start() -> str:
                """使用原事务工厂和原 authorizer 提交请求开始；此测试不调用供应商写接口。"""
                transactions = SqlAlchemyTrustedActionRepositoryFactory(sessions, ACTION_CIPHER)
                async with transactions() as transaction:
                    result = await transaction.mark_request_started(
                        snapshot=snapshot,
                        lease_owner=seed.owner,
                        authorize=workflow._request_start_authorization_error,
                    )
                return result.disposition.value

            execution = request_start

        method = "lock_execution" if phase == "claim" else "mark_request_started"
        original_execution = getattr(SqlAlchemyTrustedActionRepository, method)

        async def observe_execution(
            repository: SqlAlchemyTrustedActionRepository, **kwargs: object
        ) -> object:
            """登记实际事务 PID 后立即委托原锁序，不能提前制造 user 锁或审批成功结果。"""
            await race.observe_waiter(repository._session)
            return await original_execution(repository, **kwargs)

        monkeypatch.setattr(SqlAlchemyTrustedActionRepository, method, observe_execution)
        callback_state: str | None = None
        disconnected_id: UUID | None = None
        refresh_provider = _ActionRefreshProvider(oauth)
        refresh_request = OAuthRefreshRequest(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
            capability=ConnectionCapability.MAIL_READ,
            attempt_id=uuid4(),
        )

        if scenario.startswith("callback_"):
            if scenario == "callback_unbound":
                await use_case.start(
                    user_id=seed.user_id,
                    provider="google",
                    capabilities=frozenset({ConnectionCapability.MAIL_READ}),
                )
                first_lock = "validate_unfenced_identity_for_callback"
            else:
                await use_case.start_capability_enable(
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    capability=ConnectionCapability.CALENDAR_READ,
                )
                first_lock = "ensure_bound_connection_for_callback"
            assert oauth.request is not None
            callback_state = oauth.request.state
            _pause_store_method(monkeypatch, race, first_lock)

            async def oauth_operation() -> str:
                """授权码交换只发生一次；暂停点在随后本地保存事务的真实连接锁之后。"""
                assert callback_state is not None
                result = await use_case.callback(code=uuid4().hex, state=callback_state)
                assert result == seed.connection_id
                return "callback_saved"

        elif scenario in {"authorization_failed", "recovery_unsatisfied"}:
            if scenario == "recovery_unsatisfied":

                async def unknown_response() -> None:
                    """只在真实 started 提交后的网络边界制造未知，保留 unresolved fence。"""
                    raise TransientProviderError(
                        error_code="provider_temporarily_unavailable",
                        message="Response unavailable",
                    )

                refresh_provider.before_response = unknown_response
                assert use_case._coordinator is not None
                with pytest.raises(OAuthRefreshError):
                    await use_case._coordinator.refresh(refresh_request, refresh_provider)
                assert refresh_provider.calls == 1
            await use_case.start_capability_enable(
                user_id=seed.user_id,
                connection_id=seed.connection_id,
                capability=ConnectionCapability.CALENDAR_READ,
            )
            assert oauth.request is not None
            callback_state = oauth.request.state
            _pause_store_method(monkeypatch, race, "mark_progressive_authorization_failed")

            async def oauth_operation() -> str:
                """失败回调仍按原事务消费 state、更新能力并追加一次失败或恢复关闭事实。"""
                assert callback_state is not None
                await use_case.callback_error(provider="google", state=callback_state)
                return scenario

        elif scenario == "progressive_attempt":
            _pause_store_method(monkeypatch, race, "create_attempt", before=True)

            async def oauth_operation() -> str:
                """能力更新、attempt INSERT 及用户 FK 检查均由原渐进用例执行。"""
                await use_case.start_capability_enable(
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    capability=ConnectionCapability.CALENDAR_READ,
                )
                return "attempt_created"

        elif scenario == "automatic_started":
            original_snapshot = oauth_refresh_coordinator.load_refresh_snapshot

            async def pause_snapshot(
                session: AsyncSession, request: OAuthRefreshRequest, *, lock: bool
            ) -> object:
                """真实 snapshot 已取得 connection/access/refresh 后，暂停在 started INSERT 前。"""
                result = await original_snapshot(session, request, lock=lock)
                if lock:
                    await race.hold(session)
                return result

            monkeypatch.setattr(oauth_refresh_coordinator, "load_refresh_snapshot", pause_snapshot)

            async def oauth_operation() -> str:
                """真实 coordinator 保留 session lease、started 先提交、单次 grant 与 CAS。"""
                assert use_case._coordinator is not None
                await use_case._coordinator.refresh(refresh_request, refresh_provider)
                return "refresh_confirmed"

        else:
            assert scenario == "disconnected_credentials"
            disconnected_id = await _seed_disconnected_residue(sessions, seed)
            original_identity = retention_module.lock_oauth_cleanup_identity

            async def pause_identity(
                session: AsyncSession, *, user_id: UUID, connection_id: UUID
            ) -> object:
                """只暂停两张原 EXCLUSIVE NOWAIT 表锁均成功后的真实 retention 事务。"""
                result = await original_identity(
                    session, user_id=user_id, connection_id=connection_id
                )
                assert result is not None
                await race.hold(session)
                return result

            monkeypatch.setattr(retention_module, "lock_oauth_cleanup_identity", pause_identity)
            worker = retention_module.RetentionCleanupWorker(retention)

            async def oauth_operation() -> str:
                """直接执行 Worker 的原断开清理阶段，避免其他生命周期阶段影响目标动作。"""
                await worker._clean_disconnected_credentials(seed.user_id, 1)
                return "credentials_cleaned"

        holder_result, execution_result = await race.run(
            sessions=sessions,
            holder=oauth_operation,
            waiter=execution,
            scenario=scenario,
            phase=phase,
            capsys=capsys,
        )
        assert (
            holder_result.result
            == {
                "callback_unbound": "callback_saved",
                "callback_bound": "callback_saved",
                "progressive_attempt": "attempt_created",
                "automatic_started": "refresh_confirmed",
                "disconnected_credentials": "credentials_cleaned",
                "authorization_failed": "authorization_failed",
                "recovery_unsatisfied": "recovery_unsatisfied",
            }[scenario]
        )
        expected_execution = (
            "connection_capability_disabled"
            if scenario == "progressive_attempt"
            else "connection_scope_missing"
            if scenario in {"authorization_failed", "recovery_unsatisfied"}
            else "claimed"
            if phase == "claim"
            else RequestStartDisposition.STARTED.value
        )
        assert execution_result.result == expected_execution
        assert writer.write_calls == writer.reconcile_calls == 0
        expected_code_calls = 1 if scenario.startswith("callback_") else 0
        assert oauth.calls == expected_code_calls
        assert refresh_provider.calls == (
            1 if scenario in {"automatic_started", "recovery_unsatisfied"} else 0
        )

        if callback_state is not None:
            with pytest.raises(OAuthStateRejectedError):
                await use_case.callback(code=uuid4().hex, state=callback_state)
            with pytest.raises(OAuthStateRejectedError):
                await use_case.callback_error(provider="google", state=callback_state)
            assert oauth.calls == expected_code_calls
        if scenario == "automatic_started":
            assert use_case._coordinator is not None
            await use_case._coordinator.refresh(refresh_request, refresh_provider)
            assert refresh_provider.calls == 1
        async with sessions() as session:
            executions = (
                await session.scalars(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                )
            ).all()
            assert len(executions) == (
                0
                if scenario
                in {"progressive_attempt", "authorization_failed", "recovery_unsatisfied"}
                else 1
            )
            if executions:
                assert executions[0].write_attempt_count == (1 if phase == "request_start" else 0)
                assert (executions[0].request_started_at is not None) == (phase == "request_start")
                assert executions[0].request_payload_hash == payload_hash
            attempts = (
                await session.scalars(
                    select(OAuthAttemptModel).where(OAuthAttemptModel.user_id == seed.user_id)
                )
            ).all()
            assert len(attempts) == (
                1 if callback_state is not None or scenario == "progressive_attempt" else 0
            )
            if attempts:
                assert (attempts[0].consumed_at is not None) == (callback_state is not None)
            if scenario == "automatic_started":
                count = await session.scalar(
                    select(func.count())
                    .select_from(AuditEventModel)
                    .where(
                        AuditEventModel.user_id == seed.user_id,
                        AuditEventModel.event_type.in_(
                            ("oauth.refresh_started", "oauth.refresh_confirmed")
                        ),
                    )
                )
                assert count == 2
            if scenario in {"authorization_failed", "recovery_unsatisfied"}:
                event_types = (
                    await session.scalars(
                        select(AuditEventModel.event_type).where(
                            AuditEventModel.user_id == seed.user_id,
                            AuditEventModel.event_type.in_(
                                (
                                    "oauth.authorization_failed",
                                    "oauth.refresh_started",
                                    "oauth.refresh_recovery_authorization_started",
                                    "oauth.refresh_recovery_unsatisfied",
                                )
                            ),
                        )
                    )
                ).all()
                assert sorted(event_types) == sorted(
                    ["oauth.authorization_failed"]
                    + (
                        [
                            "oauth.refresh_started",
                            "oauth.refresh_recovery_authorization_started",
                            "oauth.refresh_recovery_unsatisfied",
                        ]
                        if scenario == "recovery_unsatisfied"
                        else []
                    )
                )
            if disconnected_id is not None:
                cursor = await session.scalar(
                    select(SyncCursorModel).where(SyncCursorModel.connection_id == disconnected_id)
                )
                assert cursor is not None
                assert cursor.last_success_at is cursor.last_attempt_at is None
                assert cursor.cursor is cursor.last_error_code is None
                count = await session.scalar(
                    select(func.count())
                    .select_from(EncryptedCredentialModel)
                    .where(
                        EncryptedCredentialModel.user_id == seed.user_id,
                        EncryptedCredentialModel.connection_id == seed.connection_id,
                    )
                )
                assert count == 2
                disconnected_count = await session.scalar(
                    select(func.count())
                    .select_from(EncryptedCredentialModel)
                    .where(
                        EncryptedCredentialModel.user_id == seed.user_id,
                        EncryptedCredentialModel.connection_id == disconnected_id,
                    )
                )
                assert disconnected_count == 0
    finally:
        race.release.set()
        await workflow.dispose()
        await sessions.dispose()
        await retention.dispose()
