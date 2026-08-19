"""定义 M2 可信操作提交所需的供应商中立端口与不可变事务 DTO。

本模块只描述应用层在审批前必须观察和一次性写入的事实。SQLAlchemy、供应商 SDK、
配置实现与密钥材料均不得穿透该边界；完整命令只以不可变领域值参与纯 preflight，
随后以记录绑定 AEAD 三元组进入提交记录。
"""

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.domain.actions import CalendarProposalStatus, MailDraftStatus
from ai_employee.domain.calendar_actions import NotificationPolicy
from ai_employee.domain.connections import CapabilityStatus
from ai_employee.domain.mail_actions import MailMode, ReplyThreadHeaders
from ai_employee.domain.tasks import JsonValue, TaskStatus


class ApprovalWarningCode(StrEnum):
    """审批前供应商无损映射可产生的固定、无内容警告码。"""

    GOOGLE_SEND_UPDATES_NONE_EXTERNAL_SYNC = "google_send_updates_none_external_sync"


class TrustedActionRisk(StrEnum):
    """M2 结构化审批允许的两个风险等级。"""

    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class ApprovalPreflightResult:
    """纯 provider preflight 返回的固定警告集合。"""

    warnings: tuple[ApprovalWarningCode, ...] = ()


class TrustedActionPreflight(Protocol):
    """验证供应商能否无损表达一条冻结命令的无副作用端口。"""

    provider: str

    def validate_for_approval(self, command: TrustedCommand) -> ApprovalPreflightResult:
        """只验证表达能力，不访问网络、不创建供应商草稿或任何写入。"""


class TrustedActionPreflightRegistry(Protocol):
    """按精确 provider/action 返回固定 preflight 的注册表端口。"""

    def trusted_action_preflight(
        self,
        *,
        provider: str,
        action: str,
    ) -> TrustedActionPreflight:
        """返回进程启动时已固定组装的精确动作适配器。"""


class TrustedActionWritePolicy(Protocol):
    """审批提交前的全局、供应商与环境账户写入门禁。"""

    def provider_writes_enabled(self, provider: str) -> bool:
        """判断全局及指定供应商真实写入开关是否同时开启。"""

    def write_account_allowed(self, provider_identity_key: str) -> bool:
        """判断精确 provider identity 是否通过当前环境账户限制。"""


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
        """锁定当前用户草稿并返回命令所需完整投影。"""

    async def lock_calendar_proposal(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> CalendarProposalSubmissionSnapshot | None:
        """锁定当前用户日历提案并返回提交投影。"""

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


class TrustedActionSubmissionTransactionFactory(Protocol):
    """为每次提交创建自动提交或回滚的短事务。"""

    def __call__(self) -> AbstractAsyncContextManager[TrustedActionSubmissionTransaction]:
        """返回一次性事务上下文。"""


__all__ = [
    "ApprovalPreflightResult",
    "ApprovalWarningCode",
    "CalendarProposalSubmissionSnapshot",
    "ExistingTrustedActionSubmission",
    "MailDraftSubmissionSnapshot",
    "TrustedActionCommandCipher",
    "TrustedActionPreflight",
    "TrustedActionPreflightRegistry",
    "TrustedActionRisk",
    "TrustedActionSubmission",
    "TrustedActionSubmissionResult",
    "TrustedActionSubmissionTransaction",
    "TrustedActionSubmissionTransactionFactory",
    "TrustedActionWritePolicy",
]
