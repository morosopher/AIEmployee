"""验证可信动作 LangGraph 仅编排持久事实且 checkpoint 不含解密命令。"""

from dataclasses import dataclass
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ai_employee.agents.trusted_actions.graph import TrustedActionGraph
from ai_employee.agents.trusted_actions.state import TrustedActionState
from ai_employee.application.ports.trusted_actions import (
    ApprovalPreflightResult,
    ExecutionReference,
    ProviderWriteOutcome,
)
from ai_employee.application.use_cases.trusted_actions import TrustedActionGraphFacts
from ai_employee.domain.actions import ProviderWriteOutcomeKind
from ai_employee.domain.errors import StateConflictError
from ai_employee.integrations.registry import ProviderAdapterRegistry

TASK_ID = UUID("00000000-0000-0000-0000-000000001901")
APPROVAL_ID = UUID("00000000-0000-0000-0000-000000001902")
OPERATION_ID = UUID("00000000-0000-0000-0000-000000001903")
PAYLOAD_HASH = "a" * 64
SENSITIVE_MARKER = "synthetic-sensitive-command-body"


class _Adapter:
    """实现可信动作窄端口的合成 adapter，不访问网络。"""

    def __init__(self, provider: str) -> None:
        """绑定固定 provider，并初始化执行/核对计数。"""
        self.provider = provider
        self.execute_calls = 0
        self.reconcile_calls = 0

    def validate_for_approval(self, command: object) -> ApprovalPreflightResult:
        """审批前只验证命令对象存在，不产生任何副作用。"""
        assert command is not None
        return ApprovalPreflightResult()

    async def execute(self, command: object) -> ProviderWriteOutcome:
        """返回合成成功结果。"""
        assert command is not None
        self.execute_calls += 1
        return ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.CONFIRMED_APPLIED,
            retryable=False,
            retry_after_seconds=None,
            provider_resource_id="synthetic-resource",
            provider_request_id="synthetic-request",
            correlation_id="synthetic-correlation",
            provider_url=None,
            error_code=None,
        )

    async def reconcile(
        self,
        command: object,
        execution: ExecutionReference,
    ) -> ProviderWriteOutcome:
        """返回与精确 ToolExecution 绑定的合成成功核对。"""
        assert command is not None and execution.task_id == TASK_ID
        self.reconcile_calls += 1
        return await self.execute(command)


@dataclass
class _RecordingWorkflow:
    """模拟只返回标识与决定的应用用例，并记录节点调用次序。"""

    decision: str | None = None

    def __post_init__(self) -> None:
        """初始化空调用记录，便于断言拒绝分支没有越过人工审批。"""
        self.calls: list[str] = []

    async def load_graph_facts(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
    ) -> TrustedActionGraphFacts:
        """返回当前 PostgreSQL 决定投影，不接受 resume 值作为授权事实。"""
        assert (task_id, approval_id, operation_id) == (TASK_ID, APPROVAL_ID, OPERATION_ID)
        assert expected_payload_hash in {"", PAYLOAD_HASH}
        self.calls.append("load")
        return TrustedActionGraphFacts(
            payload_hash=PAYLOAD_HASH,
            decision=self.decision,
        )

    async def claim(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
        lease_owner: str,
    ) -> None:
        """记录批准后认领，并验证租约 owner 不进入图状态。"""
        assert (task_id, approval_id, operation_id) == (TASK_ID, APPROVAL_ID, OPERATION_ID)
        assert expected_payload_hash == PAYLOAD_HASH
        assert lease_owner == "worker-a"
        self.calls.append("claim")

    async def execute_or_reconcile(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
        lease_owner: str,
    ) -> None:
        """记录持久认领后的执行节点；测试假实现不携带命令内容。"""
        del task_id, approval_id, operation_id, expected_payload_hash, lease_owner
        self.calls.append("execute_or_reconcile")

    async def finalize(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
        decision: str,
        lease_owner: str,
    ) -> None:
        """记录终结节点，拒绝和批准都必须经过同一固定尾节点。"""
        del task_id, approval_id, operation_id, expected_payload_hash, lease_owner
        self.calls.append(f"finalize:{decision}")


def _initial_state() -> TrustedActionState:
    """构造只含计划允许字段的初始图状态。"""
    return {
        "task_id": str(TASK_ID),
        "approval_id": str(APPROVAL_ID),
        "operation_id": str(OPERATION_ID),
        "payload_hash": "",
        "decision": None,
        "messages": [],
    }


@pytest.mark.asyncio
async def test_approved_resume_claims_then_executes_without_checkpointing_command() -> None:
    """批准恢复必须按固定节点顺序执行，且状态里永远没有解密命令字段。"""
    workflow = _RecordingWorkflow()
    compiled = TrustedActionGraph(workflow=workflow, lease_owner="worker-a").compile(
        checkpointer=InMemorySaver()
    )
    config = {"configurable": {"thread_id": str(TASK_ID)}}

    interrupted = await compiled.ainvoke(_initial_state(), config=config)

    assert isinstance(interrupted, dict) and "__interrupt__" in interrupted
    assert workflow.calls == ["load", "load"]

    workflow.decision = "approved"
    resumed = await compiled.ainvoke(Command(resume="untrusted-resume-value"), config=config)

    assert isinstance(resumed, dict)
    assert set(resumed) == set(TrustedActionState.__required_keys__)
    assert resumed["decision"] == "approved"
    assert workflow.calls == [
        "load",
        "load",
        "load",
        "claim",
        "execute_or_reconcile",
        "finalize:approved",
    ]
    assert SENSITIVE_MARKER not in repr(resumed)
    assert not {
        "command",
        "command_payload",
        "decrypted_command",
        "payload_ciphertext",
        "body_text",
        "subject",
        "attendees",
    }.intersection(resumed)


@pytest.mark.asyncio
async def test_rejected_resume_never_claims_or_dispatches_adapter() -> None:
    """拒绝决定只走固定 finalize，不能创建 ToolExecution 或调用供应商。"""
    workflow = _RecordingWorkflow()
    compiled = TrustedActionGraph(workflow=workflow, lease_owner="worker-a").compile(
        checkpointer=InMemorySaver()
    )
    config = {"configurable": {"thread_id": str(TASK_ID)}}
    await compiled.ainvoke(_initial_state(), config=config)

    workflow.decision = "rejected"
    resumed = await compiled.ainvoke(Command(resume="approved"), config=config)

    assert isinstance(resumed, dict)
    assert resumed["decision"] == "rejected"
    assert workflow.calls == ["load", "load", "load", "finalize:rejected"]


def test_state_schema_is_identifier_only_and_content_free() -> None:
    """静态状态协议必须精确冻结为六个内容无关字段。"""
    assert TrustedActionState.__required_keys__ == frozenset(
        {
            "task_id",
            "approval_id",
            "operation_id",
            "payload_hash",
            "decision",
            "messages",
        }
    )


def test_provider_write_outcome_rejects_retry_after_outside_safe_not_applied_case() -> None:
    """只有明确未应用且可重试的结果可以携带 Retry-After。"""
    with pytest.raises(ValueError, match="retry_after_seconds"):
        ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.UNKNOWN,
            retryable=True,
            retry_after_seconds=30,
            provider_resource_id=None,
            provider_request_id=None,
            correlation_id="synthetic-correlation",
            provider_url=None,
            error_code="provider_timeout",
        )

    with pytest.raises(ValueError, match="retry_after_seconds"):
        ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
            retryable=False,
            retry_after_seconds=30,
            provider_resource_id=None,
            provider_request_id=None,
            correlation_id="synthetic-correlation",
            provider_url=None,
            error_code="provider_rejected",
        )

    safe = ProviderWriteOutcome(
        kind=ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
        retryable=True,
        retry_after_seconds=30,
        provider_resource_id=None,
        provider_request_id="synthetic-request",
        correlation_id="synthetic-correlation",
        provider_url=None,
        error_code="provider_busy",
    )
    assert safe.retry_after_seconds == 30


def test_registry_exposes_only_fixed_trusted_action_combinations() -> None:
    """注册表必须拒绝动态 provider/action 名称和 provider 不匹配 adapter。"""
    google_mail = _Adapter("google")
    google_calendar = _Adapter("google")
    registry = ProviderAdapterRegistry(
        google_mail_action=google_mail,
        google_calendar_action=google_calendar,
    )

    assert registry.trusted_action_adapter(provider="google", action="mail.send") is google_mail
    assert (
        registry.trusted_action_adapter(provider="google", action="calendar.restore")
        is google_calendar
    )

    for provider, action in (
        ("dynamic", "mail.send"),
        ("google", "mail.forward"),
        ("google", "calendar.delete"),
    ):
        with pytest.raises(StateConflictError) as raised:
            registry.trusted_action_adapter(provider=provider, action=action)
        assert raised.value.error_code == "provider_action_unavailable"

    mismatched = ProviderAdapterRegistry(google_mail_action=_Adapter("microsoft"))
    with pytest.raises(StateConflictError) as raised:
        mismatched.trusted_action_adapter(provider="google", action="mail.send")
    assert raised.value.error_code == "provider_action_unavailable"
