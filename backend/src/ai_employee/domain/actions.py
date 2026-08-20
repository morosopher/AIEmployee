"""定义 M2 可信写操作的纯领域状态、结果分类与确定性迁移规则。"""

from collections.abc import Mapping
from enum import StrEnum
from hmac import compare_digest
from types import MappingProxyType
from typing import Final, TypeGuard
from uuid import UUID

from ai_employee.domain.errors import StateConflictError

DURABLE_RETRY_BACKOFF_CAP_SECONDS: Final[int] = 300
"""可信写结果与耐久任务退避共同使用的最大 Retry-After 秒数。"""


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


def trusted_action_idempotency_key(
    *,
    action: str,
    task_id: UUID,
    approval_id: UUID,
    approval_version: int,
    operation_id: UUID,
) -> str:
    """生成冻结审批与单次操作共同绑定的稳定 ToolExecution 幂等键。

    Args:
        action: 四种已批准可信动作之一。
        task_id: 承载该动作的 TaskRun 标识。
        approval_id: 精确冻结命令的 ApprovalRequest 标识。
        approval_version: 已批准且不可变的审批版本。
        operation_id: 命令内绑定的单次操作标识。

    Returns:
        与 M2 规格一致的五段冒号分隔物理键。
    """
    return ":".join(
        (
            action,
            str(task_id),
            str(approval_id),
            str(approval_version),
            str(operation_id),
        )
    )


def trusted_execution_binding_matches(
    *,
    execution_task_id: object,
    expected_task_id: UUID,
    execution_step_id: object,
    expected_step_id: UUID,
    execution_operation_id: object,
    expected_operation_id: UUID,
    execution_provider: object,
    expected_provider: str,
    execution_tool_name: object,
    expected_action: str,
    execution_idempotency_key: object,
    approval_id: UUID,
    approval_version: int,
    execution_payload_hash: object,
    expected_payload_hash: str,
) -> bool:
    """验证 ToolExecution 仍精确绑定当前审批、步骤、命令哈希和供应商。

    该纯规则同时供应用层 dispatch 选择与锁内 Repository CAS 使用。两层都从各自
    已读取的事实重新计算幂等键，不能把一条仅状态名相同、但已换绑步骤、动作、
    provider、operation 或 payload 的行提升为真实写授权。

    Returns:
        全部绑定及规范 SHA-256 精确匹配时为 ``True``，否则 fail closed。
    """
    expected_idempotency_key = trusted_action_idempotency_key(
        action=expected_action,
        task_id=expected_task_id,
        approval_id=approval_id,
        approval_version=approval_version,
        operation_id=expected_operation_id,
    )
    return (
        execution_task_id == expected_task_id
        and execution_step_id == expected_step_id
        and execution_operation_id == expected_operation_id
        and execution_provider == expected_provider
        and execution_tool_name == expected_action
        and execution_idempotency_key == expected_idempotency_key
        and _canonical_sha256(execution_payload_hash)
        and _canonical_sha256(expected_payload_hash)
        and compare_digest(execution_payload_hash, expected_payload_hash)
    )


def durable_retry_summary_is_valid(summary: object) -> bool:
    """判断持久结果是否精确证明上次写入未应用且允许安全重试。

    JSONB 只接受 ``kind``、``retryable`` 与可选 ``retry_after_seconds`` 三个键；
    多余键、布尔伪装整数、负数或超过统一五分钟上限都会拒绝。该证明必须与
    ``retryable_failed`` 状态一起存在，单独的状态字符串不能授权再次写入。
    """
    if type(summary) is not dict:
        return False
    allowed_keys = {"kind", "retryable", "retry_after_seconds"}
    required_keys = {"kind", "retryable"}
    summary_keys = set(summary)
    if not summary_keys.issubset(allowed_keys) or not required_keys.issubset(summary_keys):
        return False
    if (
        summary["kind"] != ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED.value
        or summary["retryable"] is not True
    ):
        return False
    retry_after = summary.get("retry_after_seconds")
    return retry_after is None or (
        type(retry_after) is int and 0 <= retry_after <= DURABLE_RETRY_BACKOFF_CAP_SECONDS
    )


def _canonical_sha256(value: object) -> TypeGuard[str]:
    """只接受小写十六进制 SHA-256，供常量时间绑定比较前收窄。"""
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


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
MAIL_DRAFT_ALLOWED_TRANSITIONS: Final[Mapping[MailDraftStatus, frozenset[MailDraftStatus]]] = (
    MappingProxyType(
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
