"""定义统一操作视图及未知结果的人工/用户核对用例。

列表、时间线与 SSE 只暴露稳定标识和枚举；完整内容仅允许进入当前认证用户的类型化
审批预览。人工 CAS 的锁序、审计与 Outbox 原子写入仍由原 repository 完成。
"""

import re
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Annotated, Literal, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from ai_employee.application.use_cases.calendar_proposals import CalendarAvailabilityContext
from ai_employee.application.use_cases.task_execution import utc_instant
from ai_employee.domain.tasks import JsonValue

POSTGRESQL_BIGINT_MAX = 2**63 - 1
_CANONICAL_CURSOR = re.compile(r"^(0|[1-9][0-9]*)$")
MANUAL_RESOLUTIONS = frozenset({"confirmed_executed", "confirmed_not_executed"})


def parse_canonical_cursor(value: object, *, field: str = "task_version") -> int:
    """验证并解析 PostgreSQL BIGINT 范围内的规范十进制游标。

    Args:
        value: API/浏览器传入的字符串；整数、前导零和符号形式都拒绝。
        field: 错误消息中使用的稳定字段名。

    Returns:
        已通过格式与范围检查的 Python ``int``。

    Raises:
        TypeError: 输入不是字符串。
        ValueError: 不是规范非负十进制，或超出 BIGINT 上限。
    """
    if type(value) is not str:
        raise TypeError(f"{field} must be a canonical decimal string")
    if _CANONICAL_CURSOR.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical decimal string")
    max_text = str(POSTGRESQL_BIGINT_MAX)
    if len(value) > len(max_text) or (len(value) == len(max_text) and value > max_text):
        raise ValueError(f"{field} exceeds the supported cursor range")
    # 只在正则与固定长度检查之后转换，避免不受控的大整数文本触发运行时限制。
    return int(value)


def canonical_cursor(value: object, *, field: str = "task_version") -> str:
    """返回经过统一规则验证的游标原文。

    ``ActionSnapshot.event_cursor`` 与 ``task_version`` 使用同一表示；该函数用于输出
    端口也用于输入端，确保前端不会因 JavaScript number 精度而改变 BIGINT。
    """
    parsed = parse_canonical_cursor(value, field=field)
    return str(parsed)


# 便于调用方按语义命名而不复制 parser；所有别名共享同一范围/前导零规则。
validate_task_version = canonical_cursor
parse_task_version = parse_canonical_cursor

type ActionKind = Literal["mail.send", "calendar.create", "calendar.update", "calendar.restore"]
type ActionProvider = Literal["google", "microsoft"]
type ActionItemKind = Literal["mail_draft", "calendar_proposal", "trusted_task"]


class MailApprovalPreview(BaseModel):
    """认证响应专用的冻结邮件预览；列表、审计、SSE 和日志不可序列化此类型。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["mail"] = "mail"
    provider: ActionProvider
    account_email: str
    mode: Literal["new", "reply", "reply_all"]
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    body_text: str
    irreversible: Literal[True] = True


class CalendarPreviewFields(BaseModel):
    """日程审批前后值的统一字段形状，保留全天日期与定时时刻的精确线格式。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    title: str
    description: str | None
    location: str | None
    starts_at: str
    ends_at: str
    timezone: str
    all_day: bool
    attendees: list[str]


class CalendarConflictPreview(BaseModel):
    """只表达本人日历冲突的区间或缺失连接，绝不包含其他事件内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["overlap", "outside_working_hours", "partial_sources"]
    starts_at: str | None = None
    ends_at: str | None = None
    missing_connection_ids: list[UUID] = Field(default_factory=list)


class CalendarApprovalPreview(BaseModel):
    """日程冻结命令、前快照和只读冲突组成的认证预览，不赋予任何写授权。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["calendar"] = "calendar"
    provider: ActionProvider
    account_email: str
    calendar_name: str
    operation: Literal["create", "update", "restore"]
    before: CalendarPreviewFields | None
    after: CalendarPreviewFields
    conflicts: list[CalendarConflictPreview]
    notification_policy: Literal["all", "none"]
    base_etag: str | None
    compensation_available: bool
    provider_warnings: list[Literal["google_send_updates_none_external_sync"]] = Field(
        default_factory=list
    )


type ApprovalPreview = Annotated[
    MailApprovalPreview | CalendarApprovalPreview, Field(discriminator="kind")
]


class ActionLocalSummary(BaseModel):
    """绑定精确本地编辑对象，当前版本与审批冻结版本分别表达。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    item_kind: Literal["mail_draft", "calendar_proposal"]
    status: str
    version: int
    editor_url: str


class ActionApprovalView(BaseModel):
    """审批决定所需版本、哈希和有效期；内容清除时只保留审计骨架。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    status: str
    version: int
    payload_hash: str
    proposal_version: int
    risk_level: Literal["high", "medium"]
    expires_at: datetime
    decided_at: datetime | None
    content_status: Literal["available", "redacted"]
    preview: ApprovalPreview | None


class ActionExecutionView(BaseModel):
    """只暴露执行状态和尝试次数，供应商原始结果始终留在适配器边界。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    status: str
    write_attempt_count: int
    reconciliation_attempt_count: int
    error_code: str | None
    claimed_at: datetime | None
    request_started_at: datetime | None
    completed_at: datetime | None
    manual_resolution: Literal["confirmed_executed", "confirmed_not_executed"] | None


class ActionTimelineEvent(BaseModel):
    """动作快照中的持久时间线；payload 通过与 SSE 相同的内容白名单。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    event: str
    occurred_at: datetime
    payload: dict[str, JsonValue]


class _ActionListBase(BaseModel):
    """统一列表的无内容公共列；每个变体都保持原始状态来源。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    status: str
    action: ActionKind
    provider: ActionProvider
    risk_level: Literal["high", "medium"] | None
    created_at: datetime
    updated_at: datetime


class MailDraftListItem(_ActionListBase):
    """尚可本地编辑的草稿以自身 UUID 导航，不能发明 TaskRun 身份。"""

    item_kind: Literal["mail_draft"] = "mail_draft"
    task_id: None = None
    editor_url: str


class CalendarProposalListItem(_ActionListBase):
    """尚可本地编辑的日程提案以自身 UUID 导航。"""

    item_kind: Literal["calendar_proposal"] = "calendar_proposal"
    task_id: None = None
    editor_url: str


class TrustedTaskListItem(_ActionListBase):
    """已冻结可信任务必须携带权威 TaskRun UUID。"""

    item_kind: Literal["trusted_task"] = "trusted_task"
    task_id: UUID
    editor_url: None = None


type ActionListItem = Annotated[
    MailDraftListItem | CalendarProposalListItem | TrustedTaskListItem,
    Field(discriminator="item_kind"),
]


class ActionListPage(BaseModel):
    """稳定排序后的分页结果，无需解密草稿、提案或审批。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    items: list[ActionListItem]
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ActionSnapshot:
    """同一可重复读事务中的可信状态、精确预览和审计游标。

    后续字段保留默认值以兼容 M2 人工 CAS 内部的最小读取调用。公开 API 由完整只读
    store 填充这些字段；数据库不添加第二个 TaskRun.version 状态来源。
    """

    task_id: UUID
    status: str
    error_code: str | None
    event_cursor: str
    task_version: str
    reconciliation_attempt_count: int = 0
    provider_url: str | None = None
    action: ActionKind | None = None
    provider: ActionProvider | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    local_action: ActionLocalSummary | None = None
    approval: ActionApprovalView | None = None
    execution: ActionExecutionView | None = None
    timeline: tuple[ActionTimelineEvent, ...] = ()

    def __post_init__(self) -> None:
        """要求两个公开游标均为同一规范字符串。"""
        event_cursor = canonical_cursor(self.event_cursor, field="event_cursor")
        task_version = canonical_cursor(self.task_version, field="task_version")
        if event_cursor != task_version:
            raise ValueError("event_cursor and task_version must match")
        if (
            type(self.reconciliation_attempt_count) is not int
            or self.reconciliation_attempt_count < 0
        ):
            raise ValueError("reconciliation_attempt_count must be non-negative")
        object.__setattr__(self, "event_cursor", event_cursor)
        object.__setattr__(self, "task_version", task_version)


class ActionReadStore(Protocol):
    """动作读取端口把多查询限定在同一可重复读事务，mutation 使用独立短事务。"""

    async def get(self, *, user_id: UUID, task_id: UUID) -> ActionSnapshot | None:
        """返回当前用户的完整动作快照或空。"""

    async def exists(self, *, user_id: UUID, task_id: UUID) -> bool:
        """不解密内容地检查任务归属，供人工控制的 404 边界使用。"""

    async def list_actions(
        self,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
        status: str | None,
        item_kind: ActionItemKind | None,
        provider: ActionProvider | None,
        action: ActionKind | None,
    ) -> ActionListPage:
        """按显式用户与封闭筛选项返回稳定分页。"""


class ActionViewUseCase:
    """读取统一操作中心；不复制草稿、审批或执行状态机。"""

    def __init__(self, store: ActionReadStore) -> None:
        """注入负责隔离与一致性读取的端口。"""
        self._store = store

    async def get(self, *, user_id: UUID, task_id: UUID) -> ActionSnapshot | None:
        """读取含敏感预览的认证快照，由 API 设置 no-store。"""
        return await self._store.get(user_id=user_id, task_id=task_id)

    async def exists(self, *, user_id: UUID, task_id: UUID) -> bool:
        """只检查归属，不为 mutation 提前解密内容或锁定业务行。"""
        return await self._store.exists(user_id=user_id, task_id=task_id)

    async def list_actions(
        self,
        *,
        user_id: UUID,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        item_kind: ActionItemKind | None = None,
        provider: ActionProvider | None = None,
        action: ActionKind | None = None,
    ) -> ActionListPage:
        """验证有界分页并返回原始本地/任务状态的只读联合投影。"""
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("action pagination is invalid")
        return await self._store.list_actions(
            user_id=user_id,
            limit=limit,
            offset=offset,
            status=status,
            item_kind=item_kind,
            provider=provider,
            action=action,
        )


def public_action_event_payload(metadata: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """为时间线与现有任务 SSE 复制封闭的标量投影，不透传嵌套供应商内容。

    UUID、版本和计数分别校验；文本仅允许有限长度的稳定代码。地址、自由文本、URL、
    原始 command 和未知键即使出现在历史审计里也不能越过此响应边界。M1 的 kind、
    reason 和 retry_of_task_id 保留以支持旧客户端重放。
    """
    identifiers = {
        "task_id",
        "step_id",
        "approval_id",
        "operation_id",
        "tool_execution_id",
        "connection_id",
        "proposal_id",
        "draft_id",
        "retry_of_task_id",
    }
    counts = {
        "version",
        "proposal_version",
        "approval_version",
        "attempt_count",
        "write_attempt_count",
        "reconciliation_attempt_count",
    }
    codes = {"status", "state", "error_code", "kind", "reason", "source", "decision", "resolution"}
    timestamps = {
        "created_at",
        "updated_at",
        "expires_at",
        "resolved_at",
        "claimed_at",
        "request_started_at",
        "completed_at",
        "scheduled_for",
    }
    result: dict[str, JsonValue] = {}
    for key, value in metadata.items():
        if key in identifiers and isinstance(value, str):
            try:
                result[key] = str(UUID(value))
            except ValueError:
                continue
        elif key in counts and type(value) is int and value >= 0:
            result[key] = value
        elif key in {"task_version", "event_cursor"}:
            try:
                result[key] = canonical_cursor(value)
            except (ValueError, TypeError):
                continue
        elif (
            key in codes
            and isinstance(value, str)
            and re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", value)
            or key == "provider"
            and value in ("google", "microsoft")
            or key == "action"
            and value
            in ("mail.send", "calendar.create", "calendar.update", "calendar.restore", "fake.write")
        ):
            result[key] = value
        elif key in timestamps and isinstance(value, str):
            try:
                instant = datetime.fromisoformat(value)
                if instant.tzinfo is not None:
                    result[key] = instant.isoformat()
            except ValueError:
                continue
    return result


def calendar_conflict_previews(
    fields: CalendarPreviewFields,
    context: CalendarAvailabilityContext,
) -> list[CalendarConflictPreview]:
    """以同一读取快照中的本人忙碌区间、工作时间和缓冲产生确定性警告。

    输入已经过命令时间校验；全天日期显式使用命令 IANA 时区。返回值只含区间和缺失
    连接，不查询参会人 Free/Busy，也不把缺失来源解释为无冲突。
    """
    zone = ZoneInfo(fields.timezone)
    if fields.all_day:
        start = datetime.combine(date.fromisoformat(fields.starts_at), time(), zone)
        end = datetime.combine(date.fromisoformat(fields.ends_at), time(), zone)
    else:
        start = datetime.fromisoformat(fields.starts_at)
        end = datetime.fromisoformat(fields.ends_at)
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    result: list[CalendarConflictPreview] = []
    for event in sorted(context.events, key=lambda item: (item.starts_at, item.ends_at)):
        if event.status.casefold() == "cancelled" or event.transparency.casefold() in {
            "transparent",
            "free",
        }:
            continue
        busy_start = event.starts_at - context.meeting_buffer
        busy_end = event.ends_at + context.meeting_buffer
        if busy_start < end and busy_end > start:
            result.append(
                CalendarConflictPreview(
                    kind="overlap",
                    starts_at=busy_start.isoformat(),
                    ends_at=busy_end.isoformat(),
                )
            )
    local_start, local_end = (
        start.astimezone(ZoneInfo(context.timezone)),
        end.astimezone(ZoneInfo(context.timezone)),
    )
    fits = local_start.date() == local_end.date() and any(
        interval.start <= local_start.time() and local_end.time() <= interval.end
        for interval in context.working_hours.days[local_start.weekday()]
    )
    if not fits:
        result.append(CalendarConflictPreview(kind="outside_working_hours"))
    if context.missing_connection_ids:
        result.append(
            CalendarConflictPreview(
                kind="partial_sources",
                missing_connection_ids=list(context.missing_connection_ids),
            )
        )
    return result


class ActionViewTransaction(Protocol):
    """定义人工结果与用户触发核对的事务端口。"""

    async def resolve_manual(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        task_version: int,
        resolution: str,
        resolved_at: datetime,
    ) -> int:
        """在固定锁序内执行人工 CAS，并返回新审计 ID。"""

    async def request_reconciliation(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        requested_at: datetime,
    ) -> UUID:
        """把 needs-attention 重新打开为只读核对并返回原 ToolExecution ID。"""


class ActionViewTransactionFactory(Protocol):
    """为一次操作视图 mutation 创建自动提交/回滚事务。"""

    def __call__(self) -> AbstractAsyncContextManager[ActionViewTransaction]:
        """返回短事务上下文。"""


class ManualResolutionUseCase:
    """记录人工确认结果，并保证不会重新调用供应商写接口。"""

    def __init__(self, transactions: ActionViewTransactionFactory) -> None:
        """注入负责锁序与持久化的事务工厂。"""
        self._transactions = transactions

    async def execute(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        task_version: str,
        resolution: str,
        now: datetime,
    ) -> str:
        """以审计游标 CAS 原子记录人工结论。

        Args:
            user_id: 当前认证用户；repository 仍会再次显式过滤归属。
            task_id: needs-attention 可信动作任务。
            task_version: 当前操作快照的 canonical decimal-string 游标。
            resolution: 仅 ``confirmed_executed`` 或 ``confirmed_not_executed``。
            now: 带时区人工确认时间。

        Returns:
            新 ``tool.manually_resolved`` 审计 ID 的 canonical decimal-string。

        Raises:
            ValueError/TypeError: 枚举、游标或时间不符合边界。
            StateConflictError: 游标过期、任务已被自动核对或跨用户不可见。
        """
        if type(resolution) is not str or resolution not in MANUAL_RESOLUTIONS:
            raise ValueError("resolution must be confirmed_executed or confirmed_not_executed")
        parsed_version = parse_canonical_cursor(task_version)
        resolved_at = utc_instant(now, field="now")
        async with self._transactions() as transaction:
            audit_id = await transaction.resolve_manual(
                user_id=user_id,
                task_id=task_id,
                task_version=parsed_version,
                resolution=resolution,
                resolved_at=resolved_at,
            )
        if type(audit_id) is not int or audit_id <= 0 or audit_id > POSTGRESQL_BIGINT_MAX:
            raise RuntimeError("manual resolution returned an invalid audit id")
        return str(audit_id)


class RequestActionReconciliationUseCase:
    """响应用户“再次核对”请求，只建立只读任务，不打开任何写权限。"""

    def __init__(self, transactions: ActionViewTransactionFactory) -> None:
        """注入负责状态/CAS/Outbox 原子写入的事务工厂。"""
        self._transactions = transactions

    async def execute(self, *, user_id: UUID, task_id: UUID, now: datetime) -> UUID:
        """把 needs-attention 任务转回 reconciling 并返回原执行 ID。"""
        requested_at = utc_instant(now, field="now")
        async with self._transactions() as transaction:
            return await transaction.request_reconciliation(
                user_id=user_id,
                task_id=task_id,
                requested_at=requested_at,
            )


__all__ = [
    "MANUAL_RESOLUTIONS",
    "POSTGRESQL_BIGINT_MAX",
    "ActionSnapshot",
    "ActionViewTransaction",
    "ActionViewTransactionFactory",
    "ManualResolutionUseCase",
    "RequestActionReconciliationUseCase",
    "canonical_cursor",
    "parse_canonical_cursor",
    "parse_task_version",
    "validate_task_version",
]
