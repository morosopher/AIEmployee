"""全数据删除的固定供应商撤销：受控单token、本地先提交和崩溃后零重放。

OAuth HTTP 只接到 respx 合成 transport；Microsoft 使用实际 UNSUPPORTED 端口，
任何未声明网络请求都会使测试失败。检查数据库时不输出完整凭据或正文。
"""

from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
import respx
from sqlalchemy import select, update

from ai_employee.application.ports.oauth import OAuthRevocationStatus
from ai_employee.application.use_cases.task_execution import TaskLeaseMode
from ai_employee.infrastructure.db.models.sources import (
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.google.oauth import GOOGLE_REVOKE_URL, GoogleOAuthAdapter
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter
from ai_employee.workers.privacy import AllDataDeletionCompleted
from tests.integration.privacy.test_all_data_deletion import (
    BARRIER_NOW,
    BARRIER_REQUEST_ID,
    BARRIER_TASK_ID,
    BARRIER_USER_ID,
    _barrier_lease,
    _PhaseCrashWorker,
    _seed_barrier_task,
)
from tests.integration.retention.test_m2_action_retention import seed_lifecycle_action


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "microsoft"])
@pytest.mark.parametrize("phase", ["none", "credentials_deleted", "revoke_attempted"])
@pytest.mark.parametrize("has_refresh", [False, True])
async def test_task27e_revocation_commits_credentials_first_and_never_repeats_after_crash(
    database_url: str,
    provider: str,
    phase: str,
    has_refresh: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """双provider×两种token选择×两个提交后故障，恢复只接管原赢家且不重取已删token。"""
    sessions = build_session_factory(database_url)
    connection_id = uuid4()
    decrypted: list[bytes] = []
    calls: list[OAuthRevocationStatus] = []
    invoked = 0

    class ObservedCipher(AeadCipher):
        """只记录AAD身份；解密必须最多一次且精确到当前用户/连接/优先refresh。"""

        def decrypt(self, value, aad):
            decrypted.append(aad)
            return super().decrypt(value, aad)

    cipher = ObservedCipher(b"r" * 32)
    actual = (GoogleOAuthAdapter if provider == "google" else MicrosoftOAuthAdapter)(
        "synthetic-client",
        "synthetic-secret",
        "https://assistant.example.test/callback",
    )

    class ObserveRevocation:
        """HTTP前以独立真实事务证明全部本地凭据删除已提交，不用当前session假象。"""

        async def revoke(self, token):
            nonlocal invoked
            invoked += 1
            assert token == (
                "synthetic-private-refresh" if has_refresh else "synthetic-private-access"
            )
            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(EncryptedCredentialModel.id).where(
                            EncryptedCredentialModel.user_id == BARRIER_USER_ID,
                            EncryptedCredentialModel.connection_id == connection_id,
                        )
                    )
                    is None
                )
                assert await session.get(OAuthConnectionModel, connection_id) is not None
            result = await actual.revoke(token)
            calls.append(result.status)
            if provider == "microsoft":
                assert result.error_code == "microsoft_token_revoke_unsupported"
            return result

    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        other = await seed_lifecycle_action(sessions, retained=True)
        async with sessions.begin() as session:
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=BARRIER_USER_ID,
                    provider=provider,
                    provider_tenant_id="synthetic-tenant" if provider == "microsoft" else "",
                    account_type="work_school" if provider == "microsoft" else "google",
                    provider_account_id=str(connection_id),
                    account_email="synthetic@example.test",
                    scopes=[],
                    status="connected",
                )
            )
            await session.flush()
            for kind in ("access_token", "refresh_token") if has_refresh else ("access_token",):
                plain = (
                    b"synthetic-private-refresh"
                    if kind == "refresh_token"
                    else b"synthetic-private-access"
                )
                encrypted = cipher.encrypt(
                    plain, f"{BARRIER_USER_ID}:{connection_id}:{kind}".encode("ascii")
                )
                session.add(
                    EncryptedCredentialModel(
                        id=uuid4(),
                        user_id=BARRIER_USER_ID,
                        connection_id=connection_id,
                        credential_kind=kind,
                        ciphertext=encrypted.ciphertext,
                        nonce=encrypted.nonce,
                        key_version=encrypted.key_version,
                        token_expires_at=BARRIER_NOW + timedelta(hours=1)
                        if kind == "access_token"
                        else None,
                    )
                )
        worker = _PhaseCrashWorker(sessions, phase)
        worker._credential_cipher = cipher
        worker._oauth_adapters = {provider: ObserveRevocation()}
        with respx.mock(assert_all_called=False) as router:
            route = router.post(GOOGLE_REVOKE_URL).mock(return_value=httpx.Response(200))
            if phase == "none":
                with pytest.raises(AllDataDeletionCompleted):
                    await worker.execute(_barrier_lease())
            else:
                with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
                    await worker.execute(_barrier_lease())
                assert invoked == (1 if phase == "revoke_attempted" else 0)
                async with sessions.begin() as session:
                    await session.execute(
                        update(TaskRunModel)
                        .where(TaskRunModel.id == BARRIER_TASK_ID)
                        .values(lease_expires_at=BARRIER_NOW - timedelta(seconds=1))
                    )
                lease = await SqlAlchemyTaskExecutionStore(sessions).acquire(
                    task_id=BARRIER_TASK_ID,
                    lease_owner="synthetic-revocation-recovery",
                    now=BARRIER_NOW,
                    lease_expires_at=BARRIER_NOW + timedelta(minutes=1),
                )
                assert (
                    lease is not None
                    and lease.lease_mode is TaskLeaseMode.INACTIVE_ALL_DATA_RECOVERY
                )
                worker.phase = "none"
                with pytest.raises(AllDataDeletionCompleted):
                    await worker.execute(lease)
            expected_calls = 0 if phase == "credentials_deleted" else 1
            assert invoked == expected_calls
            assert route.call_count == (expected_calls if provider == "google" else 0)
        chosen = "refresh_token" if has_refresh else "access_token"
        assert decrypted == [f"{BARRIER_USER_ID}:{connection_id}:{chosen}".encode("ascii")]
        expected = (
            OAuthRevocationStatus.REVOKED
            if provider == "google"
            else OAuthRevocationStatus.UNSUPPORTED
        )
        assert calls == [expected] * expected_calls
        async with sessions() as session:
            assert await session.get(OAuthConnectionModel, connection_id) is None
            assert await session.get(OAuthConnectionModel, other.connection_id) is not None
            audits = (
                await session.scalars(
                    select(AuditEventModel).where(AuditEventModel.user_id == BARRIER_USER_ID)
                )
            ).all()
            assert len(audits) == 1 and audits[0].event_type == "privacy.deletion_completed"
            assert audits[0].event_metadata == {
                "operation": "all_data_deletion",
                "request_id": BARRIER_REQUEST_ID,
                "completed_at": BARRIER_NOW.isoformat(),
                "trace_id": None,
            }
        assert "synthetic-private-" not in caplog.text
    finally:
        await sessions.dispose()
