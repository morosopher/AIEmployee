"""定义 M2 可信操作提交所需的供应商中立端口与不可变事务 DTO。

本模块只描述应用层在审批前必须观察和一次性写入的事实。SQLAlchemy、供应商 SDK、
配置实现与密钥材料均不得穿透该边界；完整命令只以不可变领域值参与纯 preflight，
随后以记录绑定 AEAD 三元组进入提交记录。
"""

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hmac import compare_digest
from typing import Final, Protocol, TypeGuard, runtime_checkable
from uuid import NAMESPACE_URL, UUID, uuid5

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.oauth_refresh import OAuthRefreshReady
from ai_employee.domain.actions import (
    CalendarProposalStatus,
    MailDraftStatus,
    ProviderWriteOutcomeKind,
    ToolExecutionStatus,
)
from ai_employee.domain.calendar_actions import NotificationPolicy
from ai_employee.domain.connections import CapabilityStatus
from ai_employee.domain.mail_actions import MailMode, ReplyThreadHeaders
from ai_employee.domain.tasks import JsonValue, TaskStatus

TRUSTED_ACTION_RETRY_AFTER_CAP_SECONDS: Final[int] = 300
"""可信写结果允许持久化的最大 Retry-After 秒数。"""


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
    已读取的事实重新计算幂等键，不能把仅状态名相同、但已换绑步骤、动作、provider、
    operation 或 payload 的行提升为真实写授权。

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
    多余键、布尔伪装整数、负数或超过五分钟上限都会拒绝。该证明必须与
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
        type(retry_after) is int and 0 <= retry_after <= TRUSTED_ACTION_RETRY_AFTER_CAP_SECONDS
    )


def _canonical_sha256(value: object) -> TypeGuard[str]:
    """只接受小写十六进制 SHA-256，供常量时间绑定比较前收窄。"""
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class ApprovalWarningCode(StrEnum):
    """审批前供应商无损映射可产生的固定、无内容警告码。"""

    GOOGLE_SEND_UPDATES_NONE_EXTERNAL_SYNC = "google_send_updates_none_external_sync"


class TrustedActionRisk(StrEnum):
    """M2 结构化审批允许的两个风险等级。"""

    MEDIUM = "medium"
    HIGH = "high"


class RequestStartDisposition(StrEnum):
    """描述 request-start 锁内 CAS 的四种内容无关结论。"""

    STARTED = "started"
    ABANDONED = "abandoned"
    RECONCILE = "reconcile"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class RequestStartResult:
    """把开始、放弃、只读核对或安全失效与稳定错误码一起返回应用层。"""

    disposition: RequestStartDisposition
    error_code: str | None = None

    def __post_init__(self) -> None:
        """要求只有安全失效结果携带稳定错误码。"""
        if self.disposition is RequestStartDisposition.INVALIDATED:
            if type(self.error_code) is not str or not self.error_code.strip():
                raise ValueError("invalidated request-start result requires error_code")
        elif self.error_code is not None:
            raise ValueError("only invalidated request-start result accepts error_code")


@dataclass(frozen=True, slots=True)
class TrustedActionRequestStartAuthorization:
    """保存 request-start 固定锁序下读取的最新用户、连接与能力事实。"""

    user_is_active: bool
    provider: str
    provider_tenant_id: str
    provider_account_id: str
    connection_status: str
    read_capability_status: CapabilityStatus
    write_capability_status: CapabilityStatus
    write_capability_error_code: str | None
    calendar_can_write: bool | None


RequestStartAuthorizer = Callable[[TrustedActionRequestStartAuthorization], str | None]
"""在 Repository 持有全部固定行锁时调用的无 I/O 应用写策略。"""


@dataclass(frozen=True, slots=True)
class ApprovalPreflightResult:
    """纯 provider preflight 返回的固定警告集合。"""

    warnings: tuple[ApprovalWarningCode, ...] = ()


class TrustedActionObserver(Protocol):
    """只接收封闭的供应商/动作/结果，不允许观察器访问命令、用户或执行 ID。"""

    def record_provider_write(self, *, provider: str, action: str, outcome: str) -> None:
        """记录一次实际供应商写调用的结果；抛出异常时由调用方报告 unknown。"""

    def record_reconciliation(self, *, provider: str, action: str, outcome: str) -> None:
        """记录一次实际只读核对，不计入写调用。"""

    def record_calendar_version_conflict(self, *, provider: str) -> None:
        """记录已经规范化的日历版本冲突。"""

    def record_duplicate_provider_call_attempt(self, *, provider: str) -> None:
        """记录被请求开始 CAS 阻止的重复写企图。"""


class TrustedActionPreflight(Protocol):
    """验证供应商能否无损表达一条冻结命令的无副作用端口。"""

    provider: str

    def validate_for_approval(self, command: TrustedCommand) -> ApprovalPreflightResult:
        """只验证表达能力，不访问网络、不创建供应商草稿或任何写入。"""


@dataclass(frozen=True, slots=True)
class ProviderWriteOutcome:
    """供应商写入或只读核对返回的内容无关规范结果。

    ``retry_after_seconds`` 只允许出现在供应商明确证明未应用、且该失败允许安全重试
    的结果上。unknown、永久拒绝和成功结果必须保持为空，避免应用层从模糊失败中
    推导再次写入权限。
    """

    kind: ProviderWriteOutcomeKind
    retryable: bool
    retry_after_seconds: int | None
    provider_resource_id: str | None
    provider_request_id: str | None
    correlation_id: str
    provider_url: str | None
    error_code: str | None

    def __post_init__(self) -> None:
        """验证安全重试形状与内容无关标识边界。

        Raises:
            TypeError: 枚举、布尔或秒数字段类型不正确。
            ValueError: Retry-After 出现在不安全结果上，或标识含空白/换行。
        """
        if type(self.kind) is not ProviderWriteOutcomeKind:
            raise TypeError("kind must be ProviderWriteOutcomeKind")
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be bool")
        if self.retry_after_seconds is not None:
            if type(self.retry_after_seconds) is not int:
                raise TypeError("retry_after_seconds must be int or None")
            if self.retry_after_seconds < 0:
                raise ValueError("retry_after_seconds must be non-negative")
            if self.retry_after_seconds > TRUSTED_ACTION_RETRY_AFTER_CAP_SECONDS:
                raise ValueError("retry_after_seconds exceeds the durable retry cap")
            if (
                self.kind is not ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
                or not self.retryable
            ):
                raise ValueError("retry_after_seconds requires retryable confirmed_not_applied")
        if self.retryable and self.kind is not ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED:
            raise ValueError("retryable requires confirmed_not_applied")
        for field_name, value, maximum in (
            ("provider_resource_id", self.provider_resource_id, 512),
            ("provider_request_id", self.provider_request_id, 255),
            ("correlation_id", self.correlation_id, 255),
            ("error_code", self.error_code, 100),
        ):
            if value is None and field_name != "correlation_id":
                continue
            if (
                type(value) is not str
                or not value
                or value != value.strip()
                or "\r" in value
                or "\n" in value
                or len(value) > maximum
            ):
                raise ValueError(f"{field_name} is invalid")


@dataclass(frozen=True, slots=True)
class ExecutionReference:
    """传给只读核对 adapter 的最小持久 ToolExecution 事实。"""

    execution_id: UUID
    task_id: UUID
    step_id: UUID
    approval_id: UUID
    operation_id: UUID
    provider: str
    tool_name: str
    idempotency_key: str
    request_payload_hash: str
    status: ToolExecutionStatus
    result_summary: dict[str, JsonValue] | None
    request_started_at: datetime | None
    write_attempt_count: int
    provider_resource_id: str | None
    provider_request_id: str | None
    correlation_id: str | None
    reconciliation_attempt_count: int = 0
    error_code: str | None = None


def oauth_rejection_is_valid(outcome: ProviderWriteOutcome, *, provider: str) -> bool:
    """仅精确资源 401 的未应用结果可进入 OAuth 恢复；未知/限流/通用错误均不提升。"""
    return (
        outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
        and not outcome.retryable
        and outcome.retry_after_seconds is None
        and outcome.provider_resource_id is None
        and provider in {"google", "microsoft"}
        and outcome.error_code == f"{provider}_reauthorization_required"
    )


def oauth_retry_pending_is_valid(execution: ExecutionReference) -> bool:
    """读取严格持久 401 证明；retryable=false 尚不能通过原 request-start 授权。"""
    return (
        execution.status is ToolExecutionStatus.RETRYABLE_FAILED
        and execution.request_started_at is not None
        and execution.write_attempt_count > 0
        and execution.result_summary == {"kind": "confirmed_not_applied", "retryable": False}
        and execution.provider in {"google", "microsoft"}
        and execution.error_code == f"{execution.provider}_reauthorization_required"
        and execution.provider_resource_id is None
    )


def oauth_write_refresh_attempt_id(execution: ExecutionReference) -> UUID:
    """把一次已持久写尝试绑定到同一个 automatic refresh attempt，崩溃重入不另建 grant。"""
    if execution.write_attempt_count <= 0:
        raise ValueError("OAuth write recovery requires a started attempt")
    return uuid5(
        NAMESPACE_URL,
        f"AIEMPLOYEE/oauth/write-refresh/v1/{execution.execution_id}/{execution.write_attempt_count}",
    )


class TrustedActionAdapter(TrustedActionPreflight, Protocol):
    """固定 provider 的类型化真实写入与只读核对端口。"""

    async def execute(self, command: TrustedCommand) -> ProviderWriteOutcome:
        """执行一条已经过精确审批与持久 request-start 的命令。"""

    async def reconcile(
        self,
        command: TrustedCommand,
        execution: ExecutionReference,
    ) -> ProviderWriteOutcome:
        """只读核对一个可能已发送的精确 ToolExecution，绝不重放写请求。"""


class TrustedActionPreflightRegistry(Protocol):
    """按精确 provider/action 返回固定 preflight 的注册表端口。"""

    def trusted_action_preflight(
        self,
        *,
        provider: str,
        action: str,
    ) -> TrustedActionPreflight:
        """返回进程启动时已固定组装的精确动作适配器。"""


class TrustedActionAdapterRegistry(TrustedActionPreflightRegistry, Protocol):
    """按固定 provider/action 返回真实写入 adapter 的窄注册表。"""

    def trusted_action_adapter(
        self,
        *,
        provider: str,
        action: str,
    ) -> TrustedActionAdapter:
        """返回启动时冻结的精确 adapter，未知组合一律拒绝。"""


class TrustedActionWritePolicy(Protocol):
    """审批提交前的全局、供应商与环境账户写入门禁。"""

    def provider_writes_enabled(self, provider: str) -> bool:
        """判断全局及指定供应商真实写入开关是否同时开启。"""

    def write_account_allowed(self, provider_identity_key: str) -> bool:
        """判断精确 provider identity 是否通过当前环境账户限制。"""


class TrustedActionRevocationStore(Protocol):
    """提供不需要 Secret 或凭据的有界审批撤权事务。"""

    async def invalidate_disabled_provider_actions(
        self, *, providers: frozenset[str], limit: int
    ) -> int:
        """锁内重读精确供应商的未认领动作，并原子失效审批、任务与本地状态。"""


class TrustedActionCommandCipher(Protocol):
    """把标准 JSON 命令加密为记录绑定 AEAD 三元组的窄端口。"""

    def encrypt_json(
        self,
        payload: Mapping[str, object],
        *,
        user_id: UUID,
        record_id: UUID,
        content_kind: str,
        action: str,
        schema_version: str,
    ) -> EncryptedValue:
        """使用审批记录身份与动作协议构造 AAD 后加密命令。"""


@dataclass(frozen=True, slots=True)
class MailDraftSubmissionSnapshot:
    """冻结邮件命令前在同一用户事务内锁定的完整草稿投影。"""

    draft_id: UUID
    connection_id: UUID
    provider: str
    provider_identity_key: str
    current_version: int
    status: MailDraftStatus
    mode: MailMode
    source_thread_id: str | None
    source_message_id: str | None
    thread_headers: ReplyThreadHeaders | None
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body_text: str
    connection_status: str
    read_capability_status: CapabilityStatus
    write_capability_status: CapabilityStatus
    write_capability_error_code: str | None


@dataclass(frozen=True, slots=True)
class CalendarProposalSubmissionSnapshot:
    """冻结日历命令前锁定的提案、目录权限与当前事件版本投影。"""

    proposal_id: UUID
    connection_id: UUID
    provider: str
    provider_identity_key: str
    calendar_id: str
    operation_kind: str
    target_event_id: str | None
    base_etag: str | None
    before_snapshot_id: UUID | None
    current_provider_etag: str | None
    current_version: int
    status: CalendarProposalStatus
    title: str | None
    description: str | None
    location: str | None
    starts_at: str | None
    ends_at: str | None
    timezone: str | None
    all_day: bool | None
    attendees: tuple[str, ...]
    notification_policy: NotificationPolicy | None
    changed_fields: tuple[str, ...]
    submission_ready: bool
    connection_status: str
    read_capability_status: CapabilityStatus
    write_capability_status: CapabilityStatus
    write_capability_error_code: str | None
    calendar_can_write: bool
    target_event_can_edit: bool | None
    target_event_recurring: bool | None
    target_event_status: str | None


@dataclass(frozen=True, slots=True)
class ExistingTrustedActionSubmission:
    """同用户幂等键已绑定的可信任务及审批最小事实。"""

    task_id: UUID
    approval_id: UUID
    operation_id: UUID
    proposal_kind: str
    proposal_id: UUID
    proposal_version: int
    status: TaskStatus


@dataclass(frozen=True, slots=True)
class TrustedActionSubmission:
    """一次事务必须原子创建的任务、步骤、加密审批与本地状态事实。"""

    user_id: UUID
    task_id: UUID
    step_id: UUID
    approval_id: UUID
    operation_id: UUID
    idempotency_key: str
    action: str
    schema_version: str
    risk_level: TrustedActionRisk
    proposal_kind: str
    proposal_id: UUID
    proposal_version: int
    payload: dict[str, JsonValue]
    payload_hash: str
    encrypted_command: EncryptedValue
    warnings: tuple[ApprovalWarningCode, ...]
    expires_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class TrustedActionSubmissionResult:
    """提交 API 可返回的无敏感任务、审批与操作标识。"""

    task_id: UUID
    approval_id: UUID
    operation_id: UUID
    status: TaskStatus


@dataclass(frozen=True, slots=True)
class TrustedActionExecutionSnapshot:
    """claim 事务锁定后返回的审批、连接、能力与既有执行事实。"""

    user_id: UUID
    user_is_active: bool
    task_id: UUID
    task_status: TaskStatus
    task_lease_owner: str | None
    task_lease_expires_at: datetime | None
    database_now: datetime
    step_id: UUID
    approval_id: UUID
    approval_version: int
    approval_status: str
    approved_execution_deadline_at: datetime | None
    action: str
    schema_version: str
    payload_hash: str
    proposal_kind: str
    proposal_id: UUID
    proposal_version: int
    operation_id: UUID
    connection_id: UUID
    provider: str
    provider_tenant_id: str
    provider_account_id: str
    connection_status: str
    read_capability_status: CapabilityStatus
    write_capability_status: CapabilityStatus
    write_capability_error_code: str | None
    calendar_can_write: bool | None
    execution: ExecutionReference | None


@dataclass(frozen=True, slots=True)
class TrustedActionDispatchSnapshot:
    """claim 提交后选择 execute、reconcile 或终态复用所需的最小事实。"""

    user_id: UUID
    task_id: UUID
    step_id: UUID
    approval_id: UUID
    approval_version: int
    operation_id: UUID
    connection_id: UUID | None
    calendar_id: str | None
    action: str
    schema_version: str
    payload_hash: str
    proposal_kind: str
    proposal_id: UUID
    proposal_version: int
    provider: str
    execution: ExecutionReference


@runtime_checkable
class ConnectionBoundTrustedActionRegistry(Protocol):
    """生产固定 registry 的显式用户解析端口；测试静态 adapter 无需持有凭据工厂。"""

    async def validate_trusted_action_connection(
        self,
        *,
        user_id: UUID,
        provider: str,
        connection_id: UUID,
        action: str,
    ) -> None:
        """首次 claim 前只核对本地连接/凭据事实，不读取 Secret、命令明文或供应商。"""

    async def resolve_trusted_action_adapter(
        self,
        *,
        user_id: UUID,
        provider: str,
        command: TrustedCommand,
    ) -> TrustedActionAdapter:
        """在短只读边界加载精确连接凭据；返回实例不拥有长生命周期 session/client。"""

    async def refresh_after_rejection(
        self, snapshot: TrustedActionDispatchSnapshot
    ) -> OAuthRefreshReady:
        """仅针对持久未应用的精确写尝试执行/恢复唯一 coordinator attempt。"""


class TrustedActionSubmissionTransaction(Protocol):
    """协调一次提交所需的短事务端口。"""

    async def find_existing_submission(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
    ) -> ExistingTrustedActionSubmission | None:
        """串行化用户幂等键并在锁版本前读取既有提交，保证并发重放只消费一组 ID。"""

    async def lock_mail_draft(
        self,
        *,
        user_id: UUID,
        draft_id: UUID,
    ) -> MailDraftSubmissionSnapshot | None:
        """与精确连接撤权串行后锁定用户草稿，并返回当前授权及命令完整投影。"""

    async def lock_calendar_proposal(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> CalendarProposalSubmissionSnapshot | None:
        """与精确连接撤权串行后锁定用户日历提案，并返回当前授权及提交投影。"""

    async def proposal_version_is_consumed(
        self,
        *,
        user_id: UUID,
        proposal_kind: str,
        proposal_id: UUID,
        proposal_version: int,
    ) -> bool:
        """判断任一待处理或终态审批是否已经永久引用该版本。"""

    async def create_submission(self, submission: TrustedActionSubmission) -> None:
        """原子创建全部持久事实并把本地对象推进到 awaiting_approval。"""

    async def load_graph_facts(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
    ) -> tuple[str, str | None] | None:
        """读取 identifier-only Graph 所需的冻结哈希与审批决定。"""

    async def lock_execution(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
    ) -> TrustedActionExecutionSnapshot | None:
        """按 Task→Approval→ToolExecution 顺序锁定首次 claim 的全部事实。"""

    async def fail_unclaimed_action(
        self,
        *,
        snapshot: TrustedActionExecutionSnapshot,
        error_code: str,
        failed_at: datetime,
    ) -> None:
        """在没有 ToolExecution 时失效审批、失败任务并恢复本地编辑态。"""

    async def create_tool_claim(
        self,
        *,
        snapshot: TrustedActionExecutionSnapshot,
        execution_id: UUID,
        idempotency_key: str,
        claimed_at: datetime,
    ) -> None:
        """原子插入唯一 ToolExecution、推进本地状态并追加 audit/outbox。"""

    async def load_dispatch(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
    ) -> TrustedActionDispatchSnapshot | None:
        """读取已认领执行和加密命令绑定，不在此方法内解密。"""

    async def converge_pre_request_reconciliation(
        self,
        *,
        task_id: UUID,
    ) -> bool:
        """把撤权屏障留下的零请求核对事实安全收敛为明确未应用。"""

    async def reconciliation_connection_missing(
        self, *, snapshot: TrustedActionDispatchSnapshot
    ) -> bool:
        """只读确认精确连接已断开、只读能力不可用或本地凭据已不存在。"""

    async def load_command(
        self,
        *,
        user_id: UUID,
        approval_id: UUID,
    ) -> dict[str, object] | None:
        """认证解密并重新规范化一条精确审批命令。"""

    async def mark_request_started(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        lease_owner: str,
        authorize: RequestStartAuthorizer,
    ) -> RequestStartResult:
        """在最新门禁通过时提交 request-start，并类型化返回 CAS 结论。"""

    async def persist_provider_outcome(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        outcome: ProviderWriteOutcome,
        completed_at: datetime,
        from_reconciliation: bool,
        may_retry_write: bool,
        lease_owner: str,
        oauth_refresh_pending: bool = False,
    ) -> None:
        """按本轮持久重试预算原子保存结果、任务及本地对象状态。"""

    async def resolve_oauth_write_retry(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        lease_owner: str,
        ready: OAuthRefreshReady | None,
        error_code: str | None = None,
    ) -> None:
        """只在 matching refresh-result/current CAS 后授予重试；未知时原子收敛人工关注。"""

    async def abandon_started_attempt(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        lease_owner: str,
    ) -> bool:
        """以进程内 dispatch 冻结投影释放 request-start 租约并保留未决事实。"""

    async def fail_claimed_integrity(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        failed_at: datetime,
        lease_owner: str,
    ) -> None:
        """在 provider request 前把 AEAD/哈希失败收敛为零调用安全终态。"""

    async def finalize_rejected(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        lease_owner: str,
        finished_at: datetime,
    ) -> None:
        """把持久拒绝决定完成为无外部副作用的成功任务。"""


class TrustedActionSubmissionTransactionFactory(Protocol):
    """为每次提交创建自动提交或回滚的短事务。"""

    def __call__(self) -> AbstractAsyncContextManager[TrustedActionSubmissionTransaction]:
        """返回一次性事务上下文。"""


__all__ = [
    "ApprovalPreflightResult",
    "ApprovalWarningCode",
    "CalendarProposalSubmissionSnapshot",
    "ExecutionReference",
    "ExistingTrustedActionSubmission",
    "MailDraftSubmissionSnapshot",
    "ProviderWriteOutcome",
    "RequestStartAuthorizer",
    "RequestStartDisposition",
    "RequestStartResult",
    "TrustedActionAdapter",
    "TrustedActionAdapterRegistry",
    "TrustedActionCommandCipher",
    "TrustedActionDispatchSnapshot",
    "TrustedActionExecutionSnapshot",
    "TrustedActionPreflight",
    "TrustedActionPreflightRegistry",
    "TrustedActionRequestStartAuthorization",
    "TrustedActionRisk",
    "TrustedActionSubmission",
    "TrustedActionSubmissionResult",
    "TrustedActionSubmissionTransaction",
    "TrustedActionSubmissionTransactionFactory",
    "TrustedActionWritePolicy",
    "durable_retry_summary_is_valid",
    "trusted_action_idempotency_key",
    "trusted_execution_binding_matches",
]
