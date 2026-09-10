"""连接真实 M2 retention writer 与认证 Task26 reader，验证清理后的完整历史视图。

复用同一持久 fixture 与真实 Cookie 登录。测试只播种待保留的合成内容，不手工清空
任何密文字段；邮件、日程 desired/before 和审批必须由生产 Worker 清理后再读取。
"""

from datetime import timedelta
from typing import Literal
from uuid import uuid4

import pytest
from sqlalchemy import select

from ai_employee.application.use_cases.trusted_actions import validate_provider_url
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeSnapshotModel,
    MailDraftVersionModel,
)
from ai_employee.infrastructure.db.models.tasks import ApprovalRequestModel, ToolExecutionModel
from ai_employee.workers.retention import RetentionCleanupWorker
from tests.integration.api import test_connections as connection_cases
from tests.integration.api.test_connections import AuthenticatedApiClients
from tests.integration.retention.test_m2_action_retention import NOW, OLD, seed_lifecycle_action

authenticated_api_clients = connection_cases.authenticated_api_clients


def _execution_facts(execution: ToolExecutionModel) -> tuple[object, ...]:
    """保留与清理结论无关的全部执行定位、次数、时间和人工事实，用于前后精确比较。"""
    return (
        execution.id,
        execution.task_id,
        execution.step_id,
        execution.operation_id,
        execution.tool_name,
        execution.idempotency_key,
        execution.request_payload_hash,
        execution.provider,
        execution.provider_resource_id,
        execution.provider_request_id,
        execution.correlation_id,
        execution.result_summary,
        execution.claimed_at,
        execution.request_started_at,
        execution.completed_at,
        execution.write_attempt_count,
        execution.reconciliation_attempt_count,
        execution.last_reconciled_at,
        execution.manual_resolution,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mail", "calendar"])
@pytest.mark.parametrize("execution_status", [None, "claimed", "succeeded"])
async def test_task27e_real_retention_then_authenticated_action_api(
    authenticated_api_clients: AuthenticatedApiClients,
    kind: Literal["mail", "calendar"],
    execution_status: str | None,
) -> None:
    """生产清理原子删除敏感组后，API 返回 redacted/null，未知结果和既定结论完整保留。"""
    clients = authenticated_api_clients
    sessions = clients.session_factory
    seed = await seed_lifecycle_action(
        sessions,
        user_id=clients.owner_id,
        kind=kind,
        binding="event" if kind == "calendar" else "none",
        execution_status=execution_status,
    )
    provider_url = f"https://provider.example.test/{kind}/synthetic"
    assert validate_provider_url(provider_url) == provider_url
    original_facts: tuple[object, ...] | None = None
    async with sessions.begin() as session:
        approval = await session.get(ApprovalRequestModel, seed.approval_id)
        assert approval is not None and approval.payload_ciphertext is not None
        # 纯 writer seed 无需预览风险列；认证 API fixture 必须补齐真实 M2 审批投影。
        approval.risk_level = "high" if kind == "mail" else "medium"
        if kind == "mail":
            version = await session.get(MailDraftVersionModel, seed.content_id)
            assert version is not None and version.body_ciphertext is not None
        else:
            desired = await session.get(CalendarChangeSnapshotModel, seed.content_id)
            assert desired is not None and desired.content_ciphertext is not None
            # update 的补偿 before 与 desired 都在同一聚合，不能只证明 desired 被抹除。
            session.add(
                CalendarChangeSnapshotModel(
                    id=uuid4(),
                    user_id=seed.user_id,
                    proposal_id=seed.action_id,
                    version=1,
                    snapshot_kind="before",
                    content_ciphertext=b"synthetic-before-content",
                    content_nonce=bytes(range(12)),
                    content_key_version=1,
                    canonical_hash="b" * 64,
                    retain_until=NOW - timedelta(seconds=1),
                    created_at=OLD,
                )
            )
        if execution_status is not None:
            execution = await session.get(ToolExecutionModel, seed.execution_id)
            assert execution is not None
            execution.result_summary = {"provider_url": provider_url}
            original_facts = _execution_facts(execution)

    await RetentionCleanupWorker(sessions).execute(now=NOW, batch_size=1)

    async with sessions() as session:
        approval = await session.get(ApprovalRequestModel, seed.approval_id)
        assert approval is not None
        assert all(
            value is None
            for value in (
                approval.payload_ciphertext,
                approval.payload_nonce,
                approval.payload_key_version,
            )
        )
        assert approval.payload_hash == "d" * 64
        if kind == "mail":
            version = await session.get(MailDraftVersionModel, seed.content_id)
            assert version is not None
            assert all(
                value is None
                for value in (
                    version.body_ciphertext,
                    version.body_nonce,
                    version.body_key_version,
                )
            )
        else:
            snapshots = (
                await session.scalars(
                    select(CalendarChangeSnapshotModel).where(
                        CalendarChangeSnapshotModel.user_id == seed.user_id,
                        CalendarChangeSnapshotModel.proposal_id == seed.action_id,
                    )
                )
            ).all()
            assert {row.snapshot_kind for row in snapshots} == {"desired", "before"}
            assert len(snapshots) == 2
            for snapshot in snapshots:
                assert all(
                    value is None
                    for value in (
                        snapshot.content_ciphertext,
                        snapshot.content_nonce,
                        snapshot.content_key_version,
                    )
                )
                assert snapshot.canonical_hash == (
                    "b" * 64 if snapshot.snapshot_kind == "before" else "c" * 64
                )
        if execution_status is not None:
            execution = await session.get(ToolExecutionModel, seed.execution_id)
            assert execution is not None and _execution_facts(execution) == original_facts

    response = await clients.owner.get(f"/api/v1/actions/{seed.task_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["task_id"] == str(seed.task_id)
    assert payload["local_action"]["id"] == str(seed.action_id)
    assert payload["approval"]["id"] == str(seed.approval_id)
    assert payload["approval"]["payload_hash"] == "d" * 64
    assert payload["approval"]["content_status"] == "redacted"
    assert payload["approval"]["preview"] is None
    if execution_status is None:
        assert payload["approval"]["status"] == "invalidated"
        assert payload["status"] == payload["local_action"]["status"] == "cancelled"
        assert payload["error_code"] == "action_content_expired"
        assert payload["execution"] is None
    else:
        expected_status = "succeeded" if execution_status == "succeeded" else "needs_attention"
        expected_error = None if execution_status == "succeeded" else "action_content_expired"
        assert payload["status"] == expected_status
        assert payload["error_code"] == expected_error
        assert payload["provider_url"] == provider_url
        assert payload["reconciliation_attempt_count"] == 3
        assert payload["execution"] == {
            "id": str(seed.execution_id),
            "status": expected_status,
            "write_attempt_count": 1,
            "reconciliation_attempt_count": 3,
            "error_code": expected_error,
            "claimed_at": OLD.isoformat().replace("+00:00", "Z"),
            "request_started_at": OLD.isoformat().replace("+00:00", "Z"),
            "completed_at": None,
            "manual_resolution": None,
        }
    # 认证和用户隔离仍由 Task26 路由执行，不能因内容已被清理而放宽读取归属。
    foreign = await clients.other.get(f"/api/v1/actions/{seed.task_id}")
    assert foreign.status_code == 404
