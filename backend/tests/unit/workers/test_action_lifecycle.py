"""验证内容失效收敛的完成事实幂等性，不连接数据库或读取任何敏感内容。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_employee.infrastructure.db.models.actions import MailDraftModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.workers.action_lifecycle import ActionLifecycleCleanup

FIRST_EXPIRY = datetime(2030, 1, 1, tzinfo=UTC)
REPEATED_EXPIRY = FIRST_EXPIRY + timedelta(days=366)


@pytest.mark.parametrize("already_expired", [False, True])
def test_unclaimed_expiry_preserves_only_the_same_completed_expiry(
    already_expired: bool,
) -> None:
    """首次失效仍取消已有终态；同一失效重投保留第一次完成时间并清理调度。"""
    task = TaskRunModel(
        id=uuid4(),
        status="cancelled" if already_expired else "failed",
        error_code="action_content_expired" if already_expired else "provider_unavailable",
        finished_at=FIRST_EXPIRY,
        scheduled_for=REPEATED_EXPIRY,
        retry_recovery_at=REPEATED_EXPIRY,
        approval_checkpoint_recovery_at=REPEATED_EXPIRY,
        lease_owner=str(uuid4()),
        lease_expires_at=REPEATED_EXPIRY + timedelta(minutes=5),
    )
    approval = ApprovalRequestModel(task_id=task.id, status="approved")
    action = MailDraftModel(status="cancelled" if already_expired else "awaiting_approval")

    ActionLifecycleCleanup._converge([task], [approval], [], action, now=REPEATED_EXPIRY)

    assert task.finished_at == (FIRST_EXPIRY if already_expired else REPEATED_EXPIRY)
    assert task.status == action.status == "cancelled"
    assert task.error_code == "action_content_expired"
    assert approval.status == "invalidated"
    assert all(
        value is None
        for value in (
            task.scheduled_for,
            task.retry_recovery_at,
            task.approval_checkpoint_recovery_at,
            task.lease_owner,
            task.lease_expires_at,
        )
    )


def test_terminal_execution_keeps_the_original_completion_fact() -> None:
    """终态执行始终保留成功结论与完成时间，不能因为内容到期伪装成取消。"""
    task = TaskRunModel(id=uuid4(), status="succeeded", finished_at=FIRST_EXPIRY)
    execution = ToolExecutionModel(task_id=task.id, status="succeeded")
    approval = ApprovalRequestModel(task_id=task.id, status="consumed")
    action = MailDraftModel(status="sent")

    ActionLifecycleCleanup._converge([task], [approval], [execution], action, now=REPEATED_EXPIRY)

    assert task.finished_at == FIRST_EXPIRY
    assert task.status == execution.status == "succeeded"
    assert approval.status == "consumed"
    assert action.status == "sent"
