"""验证可信任务状态机只接受规格明确列出的迁移。"""

from collections.abc import MutableMapping, MutableSet
from typing import cast

import pytest

from ai_employee.domain.tasks import (
    ALLOWED_TRANSITIONS,
    ApprovalStatus,
    InvalidTaskTransition,
    StepStatus,
    TaskStatus,
    transition_task,
)

EXPECTED_ALLOWED_TRANSITIONS = frozenset(
    {
        (TaskStatus.CREATED, TaskStatus.QUEUED),
        (TaskStatus.CREATED, TaskStatus.CANCELLED),
        (TaskStatus.QUEUED, TaskStatus.RUNNING),
        (TaskStatus.QUEUED, TaskStatus.CANCELLED),
        (TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL),
        (TaskStatus.RUNNING, TaskStatus.RETRY_SCHEDULED),
        (TaskStatus.RUNNING, TaskStatus.SUCCEEDED),
        (TaskStatus.RUNNING, TaskStatus.FAILED),
        (TaskStatus.RUNNING, TaskStatus.CANCELLED),
        (TaskStatus.WAITING_APPROVAL, TaskStatus.QUEUED),
        (TaskStatus.WAITING_APPROVAL, TaskStatus.CANCELLED),
        (TaskStatus.RETRY_SCHEDULED, TaskStatus.QUEUED),
        (TaskStatus.RETRY_SCHEDULED, TaskStatus.CANCELLED),
    }
)
REJECTED_TRANSITIONS = tuple(
    (current, target)
    for current in TaskStatus
    for target in TaskStatus
    if (current, target) not in EXPECTED_ALLOWED_TRANSITIONS
)


def test_task_step_and_approval_status_values_are_stable() -> None:
    """领域枚举值必须保持可持久化和可传输的英文稳定字符串。"""
    assert tuple(status.value for status in TaskStatus) == (
        "created",
        "queued",
        "running",
        "waiting_approval",
        "retry_scheduled",
        "succeeded",
        "failed",
        "cancelled",
    )
    assert tuple(status.value for status in StepStatus) == (
        "pending",
        "running",
        "succeeded",
        "failed",
        "skipped",
    )
    assert tuple(status.value for status in ApprovalStatus) == (
        "pending",
        "approved",
        "rejected",
        "expired",
    )


@pytest.mark.parametrize(("current", "target"), sorted(EXPECTED_ALLOWED_TRANSITIONS))
def test_allowed_task_transitions(current: TaskStatus, target: TaskStatus) -> None:
    """规格列出的正常、恢复、终止和取消迁移都必须返回目标状态。"""
    assert transition_task(current, target) is target


@pytest.mark.parametrize(("current", "target"), REJECTED_TRANSITIONS)
def test_unlisted_and_self_transitions_are_rejected(
    current: TaskStatus,
    target: TaskStatus,
) -> None:
    """未列出的迁移与自迁移都必须以稳定冲突错误拒绝。"""
    with pytest.raises(InvalidTaskTransition) as captured:
        transition_task(current, target)

    assert captured.value.error_code == "invalid_task_transition"


@pytest.mark.parametrize(
    "terminal_status",
    [TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED],
)
def test_terminal_task_cannot_restart(terminal_status: TaskStatus) -> None:
    """成功、失败和取消都是不可重新入队或运行的终态。"""
    with pytest.raises(InvalidTaskTransition):
        transition_task(terminal_status, TaskStatus.QUEUED)
    with pytest.raises(InvalidTaskTransition):
        transition_task(terminal_status, TaskStatus.RUNNING)


def test_failed_task_has_no_retry_transition() -> None:
    """设计未授权失败任务重试，因此 FAILED 不得转入重试计划。"""
    with pytest.raises(InvalidTaskTransition):
        transition_task(TaskStatus.FAILED, TaskStatus.RETRY_SCHEDULED)


def test_allowed_transition_table_cannot_be_mutated() -> None:
    """状态机白名单及其目标集合必须同时不可变，避免运行期规则漂移。"""
    with pytest.raises(TypeError):
        cast(
            MutableMapping[TaskStatus, frozenset[TaskStatus]],
            ALLOWED_TRANSITIONS,
        )[TaskStatus.CREATED] = frozenset()

    with pytest.raises(AttributeError):
        cast(MutableSet[TaskStatus], ALLOWED_TRANSITIONS[TaskStatus.CREATED]).add(
            TaskStatus.SUCCEEDED
        )
