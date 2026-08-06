"""验证可信任务状态机只接受规格明确列出的迁移。"""

from collections.abc import MutableMapping, MutableSet
from typing import cast

import pytest

from ai_employee.domain.actions import ToolExecutionStatus
from ai_employee.domain.tasks import (
    ALLOWED_TRANSITIONS,
    ApprovalStatus,
    InvalidTaskTransition,
    StepStatus,
    TaskStatus,
    transition_task,
)

NORMAL_ALLOWED_TRANSITIONS = frozenset(
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
        (TaskStatus.RUNNING, TaskStatus.RECONCILING),
        (TaskStatus.WAITING_APPROVAL, TaskStatus.QUEUED),
        (TaskStatus.WAITING_APPROVAL, TaskStatus.CANCELLED),
        (TaskStatus.RETRY_SCHEDULED, TaskStatus.QUEUED),
        (TaskStatus.RETRY_SCHEDULED, TaskStatus.CANCELLED),
        (TaskStatus.RECONCILING, TaskStatus.SUCCEEDED),
        (TaskStatus.RECONCILING, TaskStatus.FAILED),
        (TaskStatus.RECONCILING, TaskStatus.NEEDS_ATTENTION),
        (TaskStatus.NEEDS_ATTENTION, TaskStatus.RECONCILING),
    }
)
MANUAL_ONLY_TRANSITIONS = frozenset(
    {
        (TaskStatus.NEEDS_ATTENTION, TaskStatus.SUCCEEDED),
        (TaskStatus.NEEDS_ATTENTION, TaskStatus.FAILED),
    }
)
EXPECTED_ALLOWED_TRANSITIONS = NORMAL_ALLOWED_TRANSITIONS | MANUAL_ONLY_TRANSITIONS
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
        "reconciling",
        "needs_attention",
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
        "invalidated",
    )


@pytest.mark.parametrize(("current", "target"), sorted(NORMAL_ALLOWED_TRANSITIONS))
def test_allowed_task_transitions(current: TaskStatus, target: TaskStatus) -> None:
    """规格列出的正常、恢复、终止和取消迁移都必须返回目标状态。"""
    assert transition_task(current, target) is target


@pytest.mark.parametrize(("current", "target"), sorted(NORMAL_ALLOWED_TRANSITIONS))
def test_manual_resolution_rejects_every_normal_transition(
    current: TaskStatus,
    target: TaskStatus,
) -> None:
    """人工结果确认标志只能授权两条专用收敛边，不能静默放宽普通迁移。"""
    with pytest.raises(InvalidTaskTransition) as captured:
        transition_task(current, target, manual_resolution=True)

    assert captured.value.error_code == "invalid_task_transition"


@pytest.mark.parametrize(("current", "target"), sorted(MANUAL_ONLY_TRANSITIONS))
def test_manual_result_resolution_requires_explicit_boundary(
    current: TaskStatus,
    target: TaskStatus,
) -> None:
    """needs_attention 只能由显式人工确认用例直接收敛为成功或失败。"""
    with pytest.raises(InvalidTaskTransition):
        transition_task(current, target)

    assert transition_task(current, target, manual_resolution=True) is target


@pytest.mark.parametrize(
    ("current", "target", "manual_resolution", "sensitive_fragment"),
    (
        ("created", TaskStatus.QUEUED, False, "created"),
        (TaskStatus.CREATED, "queued", False, "queued"),
        ("created", "queued", False, "created"),
        (
            ToolExecutionStatus.NEEDS_ATTENTION,
            TaskStatus.SUCCEEDED,
            True,
            "needs_attention",
        ),
        (
            TaskStatus.NEEDS_ATTENTION,
            ToolExecutionStatus.SUCCEEDED,
            True,
            "succeeded",
        ),
        ("unknown_task_status", TaskStatus.QUEUED, False, "unknown_task_status"),
        (TaskStatus.CREATED, "unknown_task_status", False, "unknown_task_status"),
        (object(), TaskStatus.QUEUED, False, None),
        (TaskStatus.CREATED, object(), False, None),
    ),
)
def test_task_transition_rejects_non_task_status_runtime_inputs(
    current: object,
    target: object,
    manual_resolution: bool,
    sensitive_fragment: str | None,
) -> None:
    """raw string、其他 StrEnum 与未知对象都必须在映射访问前稳定 fail closed。"""
    with pytest.raises(InvalidTaskTransition) as captured:
        transition_task(
            cast(TaskStatus, current),
            cast(TaskStatus, target),
            manual_resolution=manual_resolution,
        )

    assert captured.value.error_code == "invalid_task_transition"
    assert captured.value.message == "task state transition is not allowed"
    if sensitive_fragment is not None:
        assert sensitive_fragment not in captured.value.message


def test_invalid_task_transition_keeps_detailed_message_for_valid_task_statuses() -> None:
    """两个合法 TaskStatus 的非法边仍可使用不含业务数据的详细稳定消息。"""
    with pytest.raises(InvalidTaskTransition) as captured:
        transition_task(TaskStatus.CREATED, TaskStatus.SUCCEEDED)

    assert captured.value.message == "created cannot transition to succeeded"


@pytest.mark.parametrize(("current", "target"), REJECTED_TRANSITIONS)
def test_unlisted_and_self_transitions_are_rejected(
    current: TaskStatus,
    target: TaskStatus,
) -> None:
    """未列出的迁移与自迁移都必须以稳定冲突错误拒绝。"""
    with pytest.raises(InvalidTaskTransition) as captured:
        transition_task(current, target)

    assert captured.value.error_code == "invalid_task_transition"

    with pytest.raises(InvalidTaskTransition):
        transition_task(current, target, manual_resolution=True)


def test_needs_attention_cannot_requeue_or_restart_write_execution() -> None:
    """人工确认和只读核对都不能把未知写结果重新送回写入队列。"""
    with pytest.raises(InvalidTaskTransition):
        transition_task(TaskStatus.NEEDS_ATTENTION, TaskStatus.QUEUED)
    with pytest.raises(InvalidTaskTransition):
        transition_task(
            TaskStatus.NEEDS_ATTENTION,
            TaskStatus.RUNNING,
            manual_resolution=True,
        )


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
