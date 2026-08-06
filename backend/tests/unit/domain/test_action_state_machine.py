"""验证 M2 邮件、日历与真实写结果的纯领域状态规则。"""

from collections.abc import MutableMapping, MutableSet
from typing import cast

import pytest

from ai_employee.domain.actions import (
    CALENDAR_PROPOSAL_ALLOWED_TRANSITIONS,
    MAIL_DRAFT_ALLOWED_TRANSITIONS,
    CalendarProposalStatus,
    ExecutionDirective,
    MailDraftStatus,
    ProviderWriteOutcomeKind,
    ToolExecutionStatus,
    directive_for_outcome,
    transition_calendar_proposal,
    transition_mail_draft,
)
from ai_employee.domain.errors import StateConflictError

EXPECTED_MAIL_TRANSITIONS = frozenset(
    {
        (MailDraftStatus.EDITING, MailDraftStatus.AWAITING_APPROVAL),
        (MailDraftStatus.EDITING, MailDraftStatus.CANCELLED),
        (MailDraftStatus.AWAITING_APPROVAL, MailDraftStatus.EXECUTING),
        (MailDraftStatus.AWAITING_APPROVAL, MailDraftStatus.EDITING),
        (MailDraftStatus.AWAITING_APPROVAL, MailDraftStatus.CANCELLED),
        (MailDraftStatus.EXECUTING, MailDraftStatus.SENT),
        (MailDraftStatus.EXECUTING, MailDraftStatus.EDITING),
        (MailDraftStatus.EXECUTING, MailDraftStatus.NEEDS_ATTENTION),
        (MailDraftStatus.NEEDS_ATTENTION, MailDraftStatus.SENT),
        (MailDraftStatus.NEEDS_ATTENTION, MailDraftStatus.EDITING),
    }
)
EXPECTED_CALENDAR_TRANSITIONS = frozenset(
    {
        (CalendarProposalStatus.EDITING, CalendarProposalStatus.AWAITING_APPROVAL),
        (CalendarProposalStatus.EDITING, CalendarProposalStatus.CANCELLED),
        (CalendarProposalStatus.AWAITING_APPROVAL, CalendarProposalStatus.EXECUTING),
        (CalendarProposalStatus.AWAITING_APPROVAL, CalendarProposalStatus.EDITING),
        (CalendarProposalStatus.AWAITING_APPROVAL, CalendarProposalStatus.CANCELLED),
        (CalendarProposalStatus.EXECUTING, CalendarProposalStatus.APPLIED),
        (CalendarProposalStatus.EXECUTING, CalendarProposalStatus.EDITING),
        (CalendarProposalStatus.EXECUTING, CalendarProposalStatus.STALE),
        (CalendarProposalStatus.EXECUTING, CalendarProposalStatus.NEEDS_ATTENTION),
        (CalendarProposalStatus.NEEDS_ATTENTION, CalendarProposalStatus.APPLIED),
        (CalendarProposalStatus.NEEDS_ATTENTION, CalendarProposalStatus.EDITING),
        (CalendarProposalStatus.NEEDS_ATTENTION, CalendarProposalStatus.STALE),
        (CalendarProposalStatus.STALE, CalendarProposalStatus.EDITING),
        (CalendarProposalStatus.STALE, CalendarProposalStatus.CANCELLED),
    }
)
REJECTED_MAIL_TRANSITIONS = tuple(
    (current, target)
    for current in MailDraftStatus
    for target in MailDraftStatus
    if (current, target) not in EXPECTED_MAIL_TRANSITIONS
)
REJECTED_CALENDAR_TRANSITIONS = tuple(
    (current, target)
    for current in CalendarProposalStatus
    for target in CalendarProposalStatus
    if (current, target) not in EXPECTED_CALENDAR_TRANSITIONS
)


def test_action_status_and_directive_values_are_stable() -> None:
    """所有持久化状态与跨层指令必须保持规格规定的英文稳定值。"""
    assert tuple(status.value for status in MailDraftStatus) == (
        "editing",
        "awaiting_approval",
        "executing",
        "sent",
        "needs_attention",
        "cancelled",
    )
    assert tuple(status.value for status in CalendarProposalStatus) == (
        "editing",
        "awaiting_approval",
        "executing",
        "applied",
        "stale",
        "needs_attention",
        "cancelled",
    )
    assert CalendarProposalStatus.STALE.value == "stale"
    assert tuple(status.value for status in ToolExecutionStatus) == (
        "claimed",
        "executing",
        "succeeded",
        "confirmed_failed",
        "retryable_failed",
        "reconciling",
        "needs_attention",
    )
    assert tuple(outcome.value for outcome in ProviderWriteOutcomeKind) == (
        "confirmed_applied",
        "confirmed_not_applied",
        "unknown",
    )
    assert tuple(directive.value for directive in ExecutionDirective) == (
        "complete",
        "fail",
        "retry_write",
        "reconcile",
    )


@pytest.mark.parametrize(
    ("outcome", "retryable", "expected"),
    (
        (ProviderWriteOutcomeKind.CONFIRMED_APPLIED, False, ExecutionDirective.COMPLETE),
        (ProviderWriteOutcomeKind.CONFIRMED_APPLIED, True, ExecutionDirective.COMPLETE),
        (ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED, False, ExecutionDirective.FAIL),
        (
            ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
            True,
            ExecutionDirective.RETRY_WRITE,
        ),
        (ProviderWriteOutcomeKind.UNKNOWN, False, ExecutionDirective.RECONCILE),
        (ProviderWriteOutcomeKind.UNKNOWN, True, ExecutionDirective.RECONCILE),
    ),
)
def test_provider_write_outcomes_choose_only_safe_directives(
    outcome: ProviderWriteOutcomeKind,
    retryable: bool,
    expected: ExecutionDirective,
) -> None:
    """未知写结果必须核对，只有明确未应用且可重试时才能再次写入。"""
    assert directive_for_outcome(outcome, retryable=retryable) is expected


def test_directive_rejects_non_boolean_retryable_marker() -> None:
    """动态调用边界不能用 truthy 字符串把明确失败误判为可重试写入。"""
    with pytest.raises(TypeError, match="retryable must be a bool"):
        directive_for_outcome(
            ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
            retryable=cast(bool, "true"),
        )


@pytest.mark.parametrize(("current", "target"), sorted(EXPECTED_MAIL_TRANSITIONS))
def test_allowed_mail_draft_transitions(
    current: MailDraftStatus,
    target: MailDraftStatus,
) -> None:
    """邮件草稿只接受规格列出的审批、执行、人工收敛与取消迁移。"""
    reason = "approval_rejected" if target is MailDraftStatus.EDITING else None

    assert transition_mail_draft(current, target, reason=reason) is target


@pytest.mark.parametrize(("current", "target"), sorted(EXPECTED_CALENDAR_TRANSITIONS))
def test_allowed_calendar_proposal_transitions(
    current: CalendarProposalStatus,
    target: CalendarProposalStatus,
) -> None:
    """日历提案只接受规格列出的审批、执行、ETag 冲突与人工收敛迁移。"""
    reason = "approval_rejected" if target is CalendarProposalStatus.EDITING else None

    assert transition_calendar_proposal(current, target, reason=reason) is target


def test_rejected_mail_approval_returns_draft_to_editing() -> None:
    """拒绝邮件审批必须携带稳定原因，解锁草稿后才能产生新版本。"""
    assert (
        transition_mail_draft(
            MailDraftStatus.AWAITING_APPROVAL,
            MailDraftStatus.EDITING,
            reason="approval_rejected",
        )
        is MailDraftStatus.EDITING
    )


@pytest.mark.parametrize(
    "current",
    (
        MailDraftStatus.AWAITING_APPROVAL,
        MailDraftStatus.EXECUTING,
        MailDraftStatus.NEEDS_ATTENTION,
    ),
)
@pytest.mark.parametrize("reason", (None, "", " \t"))
def test_every_mail_transition_to_editing_requires_nonempty_reason(
    current: MailDraftStatus,
    reason: str | None,
) -> None:
    """所有返回编辑态的路径都必须留下非空机器原因，避免审计语义丢失。"""
    with pytest.raises(StateConflictError) as captured:
        transition_mail_draft(current, MailDraftStatus.EDITING, reason=reason)

    assert captured.value.error_code == "action_transition_reason_required"


@pytest.mark.parametrize(
    "current",
    (
        CalendarProposalStatus.AWAITING_APPROVAL,
        CalendarProposalStatus.EXECUTING,
        CalendarProposalStatus.NEEDS_ATTENTION,
        CalendarProposalStatus.STALE,
    ),
)
@pytest.mark.parametrize("reason", (None, "", " \t"))
def test_every_calendar_transition_to_editing_requires_nonempty_reason(
    current: CalendarProposalStatus,
    reason: str | None,
) -> None:
    """日历提案重新编辑必须记录拒绝、明确未应用或 ETag 冲突等机器原因。"""
    with pytest.raises(StateConflictError) as captured:
        transition_calendar_proposal(current, CalendarProposalStatus.EDITING, reason=reason)

    assert captured.value.error_code == "action_transition_reason_required"


@pytest.mark.parametrize(("current", "target"), REJECTED_MAIL_TRANSITIONS)
def test_unlisted_and_self_mail_transitions_are_rejected(
    current: MailDraftStatus,
    target: MailDraftStatus,
) -> None:
    """邮件自迁移与未列出路径必须以稳定状态冲突拒绝。"""
    with pytest.raises(StateConflictError) as captured:
        transition_mail_draft(current, target, reason="synthetic_reason")

    assert captured.value.error_code == "invalid_action_transition"


@pytest.mark.parametrize(("current", "target"), REJECTED_CALENDAR_TRANSITIONS)
def test_unlisted_and_self_calendar_transitions_are_rejected(
    current: CalendarProposalStatus,
    target: CalendarProposalStatus,
) -> None:
    """日历自迁移与未列出路径必须以稳定状态冲突拒绝。"""
    with pytest.raises(StateConflictError) as captured:
        transition_calendar_proposal(current, target, reason="synthetic_reason")

    assert captured.value.error_code == "invalid_action_transition"


def test_mail_and_calendar_terminal_states_cannot_restart() -> None:
    """sent、applied 与两类 cancelled 都必须是绝对终态。"""
    for terminal in (MailDraftStatus.SENT, MailDraftStatus.CANCELLED):
        with pytest.raises(StateConflictError):
            transition_mail_draft(terminal, MailDraftStatus.EDITING, reason="restart")

    for terminal in (CalendarProposalStatus.APPLIED, CalendarProposalStatus.CANCELLED):
        with pytest.raises(StateConflictError):
            transition_calendar_proposal(
                terminal,
                CalendarProposalStatus.EDITING,
                reason="restart",
            )


def test_mail_cannot_become_applied_and_calendar_cannot_become_sent() -> None:
    """分离的状态机必须在运行时也拒绝跨领域终态，避免结果语义混淆。"""
    with pytest.raises(StateConflictError):
        transition_mail_draft(
            MailDraftStatus.EXECUTING,
            cast(MailDraftStatus, CalendarProposalStatus.APPLIED),
        )
    with pytest.raises(StateConflictError):
        transition_calendar_proposal(
            CalendarProposalStatus.EXECUTING,
            cast(CalendarProposalStatus, MailDraftStatus.SENT),
        )


def test_action_transition_tables_cannot_be_mutated() -> None:
    """邮件与日历白名单的外层映射和内层目标集合必须同时不可变。"""
    with pytest.raises(TypeError):
        cast(
            MutableMapping[MailDraftStatus, frozenset[MailDraftStatus]],
            MAIL_DRAFT_ALLOWED_TRANSITIONS,
        )[MailDraftStatus.EDITING] = frozenset()
    with pytest.raises(AttributeError):
        cast(
            MutableSet[MailDraftStatus],
            MAIL_DRAFT_ALLOWED_TRANSITIONS[MailDraftStatus.EDITING],
        ).add(MailDraftStatus.SENT)

    with pytest.raises(TypeError):
        cast(
            MutableMapping[CalendarProposalStatus, frozenset[CalendarProposalStatus]],
            CALENDAR_PROPOSAL_ALLOWED_TRANSITIONS,
        )[CalendarProposalStatus.EDITING] = frozenset()
    with pytest.raises(AttributeError):
        cast(
            MutableSet[CalendarProposalStatus],
            CALENDAR_PROPOSAL_ALLOWED_TRANSITIONS[CalendarProposalStatus.EDITING],
        ).add(CalendarProposalStatus.APPLIED)
