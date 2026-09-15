"""为故障演练提供受限 Redis 测试库，绝不回退到开发或共享实例。"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy import update

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.ports.trusted_actions import ApprovalPreflightResult
from ai_employee.application.use_cases.trusted_actions import SubmitCalendarProposalUseCase
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel
from ai_employee.infrastructure.db.models.tasks import ApprovalRequestModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.queue.redis_url import (
    InvalidTestRedisUrl,
    RedisTestUrl,
    validate_test_redis_url,
)
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry


@pytest.fixture
def redis_url() -> RedisTestUrl:
    """返回已验证的 loopback Redis DB 15 连接供 Redis 丢失演练使用。

    故障测试需要真实 ``FLUSHDB``，因此必须将环境变量校验限制为独立容器的保留 DB，
    避免任何测试代码将清库操作扩散到开发或生产 Redis。
    """
    value = os.environ.get("TEST_REDIS_URL")
    if value is None:
        raise pytest.UsageError("Redis integration tests require TEST_REDIS_URL")
    try:
        return validate_test_redis_url(value).value
    except InvalidTestRedisUrl as error:
        raise pytest.UsageError(str(error)) from None


@dataclass(frozen=True)
class M2FaultAction:
    """保存本例经完整严格 Schema 冻结的审批引用；故障不修改命令及哈希。"""

    user_id: UUID
    connection_id: UUID
    task_id: UUID
    approval_id: UUID
    operation_id: UUID
    payload_hash: str
    cipher: ActionPayloadCipher
    provider: str

    def execution_arguments(self, owner: str) -> dict[str, object]:
        """返回生产 claim/dispatch 共用的标识符参数，不让明文进入故障计划。"""
        return {
            "task_id": self.task_id,
            "approval_id": self.approval_id,
            "operation_id": self.operation_id,
            "expected_payload_hash": self.payload_hash,
            "lease_owner": owner,
        }


async def seed_m2_fault_action(
    database_url: str,
    *,
    action: str,
    provider: str,
) -> M2FaultAction:
    """复用既有加密审批 fixture，四动作都保留真实数据库约束与修改前快照。

    邮件沿 Task19 的已批准骨架；日程先经真正 SubmitCalendarProposalUseCase 冻结，再在
    fixture 中设置已完成的人工批准与运行租约。该前提不计为浏览器审批或真实账户证据。
    """
    from tests.integration.m2 import test_encrypted_approval_submission as calendar_fixture
    from tests.integration.m2 import test_tool_execution_claim as mail_fixture

    if action == "mail.send":
        seed = mail_fixture._Seed()
        seed.provider = provider
        seed.account_type = "google" if provider == "google" else "personal"
        seed.provider_tenant_id = "" if provider == "google" else "synthetic-tenant"
        seed.provider_account_id = "synthetic-account" if provider == "google" else "synthetic-tenant:synthetic-account"
        payload_hash = await mail_fixture._seed_action(database_url, seed)
        return M2FaultAction(
            seed.user_id, seed.connection_id, seed.task_id, seed.approval_id,
            seed.operation_id, payload_hash, mail_fixture.ACTION_CIPHER, provider,
        )
    operation: Literal["create", "update", "restore"] = {
        "calendar.create": "create", "calendar.update": "update", "calendar.restore": "restore",
    }[action]
    proposal_id = await calendar_fixture._seed_calendar_proposal(
        database_url, operation_kind=operation,
    )
    factory = build_session_factory(database_url)

    class Preflight:
        """只声明本例精确供应商，业务命令与审批仍由正式 Submit 用例冻结。"""

        def __init__(self) -> None:
            self.provider = provider

        def validate_for_approval(self, command: TrustedCommand) -> ApprovalPreflightResult:
            """纯预检不消费故障场景或调用供应商。"""
            assert command.action == action
            return ApprovalPreflightResult()

    try:
        # 必须先设置 Microsoft 的真实字段形状再冻结审批，不能在批准后篡改连接供应商。
        async with factory.begin() as session:
            await session.execute(update(OAuthConnectionModel).where(
                OAuthConnectionModel.id == calendar_fixture.GOOGLE_CONNECTION_ID,
                OAuthConnectionModel.user_id == calendar_fixture.USER_ID,
            ).values(provider=provider, account_type="google" if provider == "google" else "personal",
                     provider_tenant_id="" if provider == "google" else "synthetic-tenant",
                     provider_account_id="synthetic-account" if provider == "google" else "synthetic-tenant:synthetic-account"))
        result = await SubmitCalendarProposalUseCase(
            transactions=SqlAlchemyTrustedActionRepositoryFactory(factory, calendar_fixture.ACTION_CIPHER),
            preflights=ProviderAdapterRegistry(**{f"{provider}_calendar_preflight": Preflight()}),
            write_policy=mail_fixture._settings(provider=provider, tenant="" if provider == "google" else "synthetic-tenant", account="synthetic-account" if provider == "google" else "synthetic-tenant:synthetic-account"),
            command_cipher=calendar_fixture.ACTION_CIPHER,
        ).execute(user_id=calendar_fixture.USER_ID, proposal_id=proposal_id, expected_version=1,
                  idempotency_key=f"synthetic-fault-{action}-{provider}", now=calendar_fixture.NOW)
        task_id, approval_id, operation_id = result.task_id, result.approval_id, result.operation_id
        async with factory.begin() as session:
            approval = await session.get(ApprovalRequestModel, approval_id)
            assert approval is not None
            payload_hash = approval.payload_hash
            approval.status = "approved"
            approval.decided_at = mail_fixture.NOW
            approval.decided_by_user_id = calendar_fixture.USER_ID
            approval.approved_execution_deadline_at = mail_fixture.NOW + timedelta(minutes=5)
            await session.execute(
                update(TaskRunModel).where(TaskRunModel.id == task_id).values(
                    status="running", lease_owner="worker-a",
                    lease_expires_at=mail_fixture.NOW + timedelta(minutes=1),
                    started_at=mail_fixture.NOW,
                )
            )
    finally:
        await factory.dispose()
    return M2FaultAction(
        calendar_fixture.USER_ID, calendar_fixture.GOOGLE_CONNECTION_ID, task_id, approval_id,
        operation_id, payload_hash, calendar_fixture.ACTION_CIPHER, provider,
    )


@pytest.fixture(scope="module", name="m2_fault_database_url")
def m2_fault_database_url(cycle5_regular_database_url: TestDatabaseUrl) -> TestDatabaseUrl:
    """让新故障矩阵使用已有 provenance/lease/anchor 的临时库生命周期。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", name="m2_fault_migrated_database")
def m2_fault_migrated_database(cycle5_regular_database_url: TestDatabaseUrl) -> Iterator[None]:
    """由官方 fixture 完成迁移，不对固定 roles-absent anchor 做写入。"""
    del cycle5_regular_database_url
    yield
