"""用生产 registry 和真实加密提交事务验证 Microsoft 当前参会人的通知约束。"""

import base64
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
import respx
from sqlalchemy import func, select

from ai_employee.application.use_cases.trusted_actions import SubmitCalendarProposalUseCase
from ai_employee.config import Settings
from ai_employee.domain.calendar_actions import NotificationPolicy
from ai_employee.domain.connections import canonical_provider_identity_key
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import ApprovalRequestModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.registry import build_trusted_action_registry
from tests.integration.m2.test_encrypted_approval_submission import (
    ACTION_CIPHER,
    CALENDAR_ID,
    NOW,
    USER_ID,
    _seed_calendar_proposal,
)
from tests.integration.m2.test_encrypted_approval_submission import (
    GOOGLE_CONNECTION_ID as CONNECTION_ID,
)

pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """仅复用受 provenance 保护的 regular 数据库，不升级或修改固定 anchor。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _migrated_database(cycle5_regular_database_url: ValidatedTestDatabaseUrl) -> Iterator[None]:
    """regular fixture 已经通过正式迁移入口完成隔离数据库准备。"""
    del cycle5_regular_database_url
    yield


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("operation", ("update", "restore"))
@pytest.mark.parametrize("current_has_attendees", (False, True))
@pytest.mark.parametrize("policy", (NotificationPolicy.NONE, NotificationPolicy.ALL))
async def test_submission_uses_current_meeting_facts_for_notification_policy(
    database_url: str,
    tmp_path: Path,
    operation: Literal["update", "restore"],
    current_has_attendees: bool,
    policy: NotificationPolicy,
) -> None:
    """清空最后参会人时在审批落库前拒绝 none，个人 none 与明确 all 正常冻结。

    真实 repository 创建新的不可变 desired 版本；registry 从同一用户/连接/日历/事件
    的当前缓存取得证明，不把 desired 或历史恢复目标误当成当前参会人事实。
    """
    proposal_id = await _seed_calendar_proposal(database_url, operation_kind=operation)
    sessions = build_session_factory(database_url)
    root_file = tmp_path / "master"
    root_file.write_text(base64.urlsafe_b64encode(b"x" * 32).decode("ascii"))
    tenant = "11111111-2222-3333-4444-555555555555"
    account = f"{tenant}:00000000-0000-0000-0000-000000000701"
    settings = Settings(
        _env_file=None,
        app_env="staging",
        external_writes_enabled=True,
        microsoft_writes_enabled=True,
        app_master_key_file=root_file,
        write_test_account_allowlist=[
            canonical_provider_identity_key("microsoft", tenant, account)
        ],
    )
    encrypted = AeadCipher(b"x" * 32).encrypt(
        b"synthetic-access",
        f"{USER_ID}:{CONNECTION_ID}:access_token".encode("ascii"),
    )
    try:
        async with sessions.begin() as session:
            connection = await session.get(OAuthConnectionModel, CONNECTION_ID)
            assert connection is not None
            connection.provider, connection.account_type = "microsoft", "work_school"
            connection.provider_tenant_id, connection.provider_account_id = tenant, account
            event = await session.scalar(select(CalendarEventModel))
            assert event is not None
            event.attendees = [{"email": "old@example.test"}] if current_has_attendees else []
            session.add(
                EncryptedCredentialModel(
                    user_id=USER_ID,
                    connection_id=CONNECTION_ID,
                    credential_kind="access_token",
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                )
            )
            repository = SqlAlchemyCalendarProposalRepository(session, ACTION_CIPHER)
            current = await repository.get_current(user_id=USER_ID, proposal_id=proposal_id)
            assert current is not None
            content = dict(current.desired_snapshot.content)
            content.update(
                attendees=[], notification_policy=policy.value, changed_fields=["attendees"]
            )
            saved = await repository.save_next_version(
                snapshot_id=uuid4(),
                user_id=USER_ID,
                proposal_id=proposal_id,
                expected_version=1,
                desired_state=content,
                retain_until=NOW + timedelta(days=365),
            )
            assert saved is not None and saved.current_version == 2
        registry = build_trusted_action_registry(session_factory=sessions, settings=settings)
        use_case = SubmitCalendarProposalUseCase(
            transactions=SqlAlchemyTrustedActionRepositoryFactory(sessions, ACTION_CIPHER),
            preflights=registry,
            write_policy=settings,
            command_cipher=ACTION_CIPHER,
        )
        rejected = current_has_attendees and policy is NotificationPolicy.NONE
        if rejected:
            with pytest.raises(StateConflictError) as raised:
                await use_case.execute(
                    user_id=USER_ID,
                    proposal_id=proposal_id,
                    expected_version=2,
                    idempotency_key="synthetic-notification-submission",
                    now=NOW,
                )
            assert raised.value.error_code == "calendar_notification_mapping_unsupported"
        else:
            await use_case.execute(
                user_id=USER_ID,
                proposal_id=proposal_id,
                expected_version=2,
                idempotency_key="synthetic-notification-submission",
                now=NOW,
            )
        async with sessions() as session:
            assert await session.scalar(
                select(func.count()).select_from(ApprovalRequestModel)
            ) == int(not rejected)
            assert await session.scalar(select(func.count()).select_from(TaskRunModel)) == int(
                not rejected
            )
        assert len(respx.calls) == 0
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch", ("none", "user", "connection", "provider", "calendar", "event")
)
async def test_current_notification_facts_are_scoped_to_exact_source(
    database_url: str,
    mismatch: str,
) -> None:
    """当前名单证明不可跨用户、连接、供应商、日历或事件借用。"""
    await _seed_calendar_proposal(database_url, operation_kind="update")
    sessions = build_session_factory(database_url)
    try:
        async with sessions() as session:
            facts = await SqlAlchemyCalendarSyncRepository(session).get_notification_facts(
                user_id=uuid4() if mismatch == "user" else USER_ID,
                connection_id=uuid4() if mismatch == "connection" else CONNECTION_ID,
                provider="microsoft" if mismatch == "provider" else "google",
                calendar_id="other-calendar" if mismatch == "calendar" else CALENDAR_ID,
                provider_event_id="other-event"
                if mismatch == "event"
                else "synthetic-provider-event",
            )
        if mismatch == "none":
            assert facts is not None and facts.has_attendees is False
            assert facts.etag == 'W/"etag-current"'
        else:
            assert facts is None
    finally:
        await sessions.dispose()
