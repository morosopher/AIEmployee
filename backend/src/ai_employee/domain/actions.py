"""定义 M2 可信写操作的纯领域状态、结果分类与确定性迁移规则。"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ai_employee.domain.errors import StateConflictError


class MailDraftStatus(StrEnum):
    """邮件草稿从本地编辑、精确审批到发送结果收敛的稳定状态。"""

    EDITING = "editing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    SENT = "sent"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


class CalendarProposalStatus(StrEnum):
    """日历提案从本地编辑、精确审批到写入结果收敛的稳定状态。"""

    EDITING = "editing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    APPLIED = "applied"
    STALE = "stale"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


class ToolExecutionStatus(StrEnum):
    """幂等 ToolExecution 从持久认领到自动或人工收敛的稳定状态。"""

    CLAIMED = "claimed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    CONFIRMED_FAILED = "confirmed_failed"
    RETRYABLE_FAILED = "retryable_failed"
    RECONCILING = "reconciling"
    NEEDS_ATTENTION = "needs_attention"


class ProviderWriteOutcomeKind(StrEnum):
    """供应商写请求经适配器规范化后的三态事实分类。"""

    CONFIRMED_APPLIED = "confirmed_applied"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"
    UNKNOWN = "unknown"


class ExecutionDirective(StrEnum):
    """应用层根据写结果可执行的唯一安全后续动作。"""

    COMPLETE = "complete"
    FAIL = "fail"
    RETRY_WRITE = "retry_write"
    RECONCILE = "reconcile"


class InvalidActionTransition(StateConflictError):
    """表示邮件草稿或日历提案不允许执行请求的状态迁移。"""

    def __init__(self, action_kind: str) -> None:
        """构造不回显业务载荷或未经验证状态值的稳定冲突。

        Args:
            action_kind: 已由调用函数固定的 ``mail_draft`` 或
                ``calendar_proposal`` 领域类别。
        """
        super().__init__(
            error_code="invalid_action_transition",
            message=f"{action_kind} state transition is not allowed",
        )


class ActionTransitionReasonRequired(StateConflictError):
    """表示返回可编辑态时缺少非空、可审计的机器原因。"""

    def __init__(self, action_kind: str) -> None:
        """构造不包含草稿、日程或自由文本内容的稳定冲突。

        Args:
            action_kind: 已由调用函数固定的领域类别。
        """
        super().__init__(
            error_code="action_transition_reason_required",
            message=f"{action_kind} transition to editing requires a non-empty reason",
        )


class InvalidProviderWriteOutcome(StateConflictError):
    """表示调用方没有传入受支持的供应商写结果枚举。"""

    def __init__(self) -> None:
        """构造不回显供应商原始结果的稳定冲突。"""
        super().__init__(
            error_code="invalid_provider_write_outcome",
            message="provider write outcome is not recognized",
        )


# 两层不可变结构确保运行期代码不能扩大邮件草稿状态图。
MAIL_DRAFT_ALLOWED_TRANSITIONS: Final[
    Mapping[MailDraftStatus, frozenset[MailDraftStatus]]
] = MappingProxyType(
    {
        MailDraftStatus.EDITING: frozenset(
            {MailDraftStatus.AWAITING_APPROVAL, MailDraftStatus.CANCELLED}
        ),
        MailDraftStatus.AWAITING_APPROVAL: frozenset(
            {
                MailDraftStatus.EXECUTING,
                MailDraftStatus.EDITING,
                MailDraftStatus.CANCELLED,
            }
        ),
        MailDraftStatus.EXECUTING: frozenset(
            {
                MailDraftStatus.SENT,
                MailDraftStatus.EDITING,
                MailDraftStatus.NEEDS_ATTENTION,
            }
        ),
        MailDraftStatus.SENT: frozenset(),
        MailDraftStatus.NEEDS_ATTENTION: frozenset(
            {MailDraftStatus.SENT, MailDraftStatus.EDITING}
        ),
        MailDraftStatus.CANCELLED: frozenset(),
    }
)

# 日历使用独立映射，避免邮件误用 ``applied`` 或日历误用 ``sent``。
CALENDAR_PROPOSAL_ALLOWED_TRANSITIONS: Final[
    Mapping[CalendarProposalStatus, frozenset[CalendarProposalStatus]]
] = MappingProxyType(
    {
        CalendarProposalStatus.EDITING: frozenset(
            {
                CalendarProposalStatus.AWAITING_APPROVAL,
                CalendarProposalStatus.CANCELLED,
            }
        ),
        CalendarProposalStatus.AWAITING_APPROVAL: frozenset(
            {
                CalendarProposalStatus.EXECUTING,
                CalendarProposalStatus.EDITING,
                CalendarProposalStatus.CANCELLED,
            }
        ),
        CalendarProposalStatus.EXECUTING: frozenset(
            {
                CalendarProposalStatus.APPLIED,
                CalendarProposalStatus.EDITING,
                CalendarProposalStatus.STALE,
                CalendarProposalStatus.NEEDS_ATTENTION,
            }
        ),
        CalendarProposalStatus.APPLIED: frozenset(),
        CalendarProposalStatus.STALE: frozenset(
            {CalendarProposalStatus.EDITING, CalendarProposalStatus.CANCELLED}
        ),
        CalendarProposalStatus.NEEDS_ATTENTION: frozenset(
            {
                CalendarProposalStatus.APPLIED,
                CalendarProposalStatus.EDITING,
                CalendarProposalStatus.STALE,
            }
        ),
        CalendarProposalStatus.CANCELLED: frozenset(),
    }
)

def directive_for_outcome(
    outcome: ProviderWriteOutcomeKind,
    *,
    retryable: bool,
) -> ExecutionDirective:
    """把供应商写结果映射为唯一安全执行指令。

    ``unknown`` 表示请求可能已经到达供应商，因此无论调用方是否标记可重试，
    都只能进入只读核对。只有供应商能够明确证明操作未应用，且上层策略允许
    重试时，才返回再次写入指令。

    Args:
        outcome: 经供应商适配器规范化的三态写结果。
        retryable: 对明确未应用结果的持久重试策略判断。

    Returns:
        完成、失败、重试写入或只读核对中的一个稳定指令。

    Raises:
        TypeError: ``retryable`` 不是普通布尔值。
        InvalidProviderWriteOutcome: ``outcome`` 不是受支持的结果枚举。
    """
    if type(retryable) is not bool:
        raise TypeError("retryable must be a bool")
    if type(outcome) is not ProviderWriteOutcomeKind:
        raise InvalidProviderWriteOutcome

    if outcome is ProviderWriteOutcomeKind.CONFIRMED_APPLIED:
        return ExecutionDirective.COMPLETE
    if outcome is ProviderWriteOutcomeKind.UNKNOWN:
        return ExecutionDirective.RECONCILE
    if retryable:
        return ExecutionDirective.RETRY_WRITE
    return ExecutionDirective.FAIL


def _transition_action[ActionStatus: (MailDraftStatus, CalendarProposalStatus)](
    current: ActionStatus,
    target: ActionStatus,
    *,
    status_type: type[ActionStatus],
    editing_status: ActionStatus,
    allowed_transitions: Mapping[ActionStatus, frozenset[ActionStatus]],
    action_kind: str,
    reason: str | None,
) -> ActionStatus:
    """执行邮件与日历状态机共享的严格白名单校验。

    Args:
        current: 当前领域状态。
        target: 请求迁移到的目标状态。
        status_type: 当前状态机唯一允许的枚举类型。
        editing_status: 当前状态机的可编辑状态。
        allowed_transitions: 当前状态机的不可变迁移白名单。
        action_kind: 用于稳定错误消息的固定领域类别。
        reason: 返回编辑态时由应用层选择的非空机器原因。

    Returns:
        已通过类型、白名单和原因校验的目标状态。

    Raises:
        InvalidActionTransition: 状态类型混用、自迁移或迁移未获批准。
        ActionTransitionReasonRequired: 返回编辑态却没有非空原因。
    """
    # StrEnum 会与同值字符串或其他 StrEnum 相等，必须先检查精确类型，避免两个
    # 状态机因 ``editing`` 等共同字符串值而互相穿透。
    if type(current) is not status_type or type(target) is not status_type:
        raise InvalidActionTransition(action_kind)
    if target not in allowed_transitions[current]:
        raise InvalidActionTransition(action_kind)
    if target is editing_status and (type(reason) is not str or not reason.strip()):
        raise ActionTransitionReasonRequired(action_kind)
    return target


def transition_mail_draft(
    current: MailDraftStatus,
    target: MailDraftStatus,
    *,
    reason: str | None = None,
) -> MailDraftStatus:
    """校验并返回一次纯邮件草稿状态迁移。

    Args:
        current: 邮件草稿当前状态。
        target: 请求迁移到的邮件草稿目标状态。
        reason: 返回 ``editing`` 时必需的非空机器原因；其他迁移忽略该值。

    Returns:
        已通过邮件白名单校验的目标状态。

    Raises:
        InvalidActionTransition: 状态类型混用、自迁移或迁移未获批准。
        ActionTransitionReasonRequired: 返回编辑态却没有非空原因。
    """
    return _transition_action(
        current,
        target,
        status_type=MailDraftStatus,
        editing_status=MailDraftStatus.EDITING,
        allowed_transitions=MAIL_DRAFT_ALLOWED_TRANSITIONS,
        action_kind="mail_draft",
        reason=reason,
    )


def transition_calendar_proposal(
    current: CalendarProposalStatus,
    target: CalendarProposalStatus,
    *,
    reason: str | None = None,
) -> CalendarProposalStatus:
    """校验并返回一次纯日历提案状态迁移。

    Args:
        current: 日历提案当前状态。
        target: 请求迁移到的日历提案目标状态。
        reason: 返回 ``editing`` 时必需的非空机器原因；其他迁移忽略该值。

    Returns:
        已通过日历白名单校验的目标状态。

    Raises:
        InvalidActionTransition: 状态类型混用、自迁移或迁移未获批准。
        ActionTransitionReasonRequired: 返回编辑态却没有非空原因。
    """
    return _transition_action(
        current,
        target,
        status_type=CalendarProposalStatus,
        editing_status=CalendarProposalStatus.EDITING,
        allowed_transitions=CALENDAR_PROPOSAL_ALLOWED_TRANSITIONS,
        action_kind="calendar_proposal",
        reason=reason,
    )
