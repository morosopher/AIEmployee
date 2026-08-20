"""定义非重复日程创建、修改、恢复、编辑与可用性建议用例。

本模块只协调本地提案、版本、加密快照和本人日历只读事实。它不会创建 ApprovalRequest、
冻结可信命令、创建 ToolExecution 或调用供应商写接口；恢复所需的供应商精确 GET 由 Worker
在数据库事务外完成，再把 provider-neutral ``CalendarEvent`` 交给本用例原子落库。
"""

import json
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from typing import Literal, Protocol, cast
from unicodedata import category as unicode_category
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_employee.application.ports.calendar import CalendarEvent
from ai_employee.application.use_cases.tasks import CreateTaskResult, TaskDispatcher
from ai_employee.domain.actions import CalendarProposalStatus
from ai_employee.domain.calendar_actions import NotificationPolicy
from ai_employee.domain.calendar_availability import (
    AvailabilityEvent,
    AvailabilityResult,
    suggest_meeting_times,
)
from ai_employee.domain.connections import CapabilityStatus, ConnectionStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import normalize_mailbox_address
from ai_employee.domain.settings import WeeklyWorkingHours
from ai_employee.domain.tasks import JsonValue

type CalendarOperationKind = Literal["create", "update", "restore"]
type CalendarSnapshotKind = Literal["desired", "before"]
type CalendarConfirmation = Literal[
    "calendar",
    "time",
    "attendees",
    "notification_policy",
]

DEFAULT_CALENDAR_CONTENT_RETENTION_DAYS = 365
CALENDAR_AVAILABILITY_HORIZON_DAYS = 14
CALENDAR_AVAILABILITY_GRID_MINUTES = 15
CALENDAR_AVAILABILITY_LIMIT = 3

_TIME_FIELDS = frozenset({"starts_at", "ends_at", "timezone", "all_day"})
_NOTIFY_FIELDS = _TIME_FIELDS | {"location", "attendees"}
_EVENT_FIELDS = (
    "title",
    "description",
    "location",
    "starts_at",
    "ends_at",
    "timezone",
    "all_day",
    "attendees",
)
_EDITABLE_FIELDS = frozenset(_EVENT_FIELDS) | {"notification_policy"}
_RECURRENCE_FIELDS = frozenset({"recurrence", "recurrence_rule", "recurring_event_id", "rrule"})
_SHELL_CONFIRMATIONS = (
    "calendar",
    "time",
    "attendees",
    "notification_policy",
)
_SAFE_CALENDAR_COMMANDS = (
    re.compile(
        r"(?:please\s+)?(?:prepare|draft)\s+(?:(?:a|an)\s+)?"
        r"(?:calendar|meeting|event)\s+proposal[.!]?"
    ),
    re.compile(r"请?(?:准备|起草|草拟)(?:一个)?(?:日程|日历|会议)提案[。！!]?"),
)


@dataclass(frozen=True, slots=True)
class CalendarSnapshot:
    """表示 Repository 已认证、解密并校验哈希的不可变日历快照。"""

    snapshot_id: UUID
    proposal_id: UUID
    version: int
    snapshot_kind: CalendarSnapshotKind
    content: dict[str, JsonValue]
    canonical_hash: str
    retain_until: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarProposalStateSnapshot:
    """表示不含日程内容的提案状态写入结果。"""

    proposal_id: UUID
    current_version: int
    status: CalendarProposalStatus


@dataclass(frozen=True, slots=True)
class CalendarProposalSnapshot:
    """表示提案头、当前 desired 与稳定 before 引用的 Repository 快照。"""

    proposal_id: UUID
    connection_id: UUID
    calendar_id: str
    operation_kind: CalendarOperationKind
    target_event_id: str | None
    base_etag: str | None
    creation_payload_hash: str
    current_version: int
    status: CalendarProposalStatus
    retain_until: datetime
    desired_snapshot: CalendarSnapshot
    before_snapshot_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CalendarRestoreSourceSnapshot:
    """表示历史 before snapshot 绑定的精确恢复目标身份。"""

    source_snapshot_id: UUID
    source_proposal_id: UUID
    connection_id: UUID
    calendar_id: str
    provider_event_id: str


@dataclass(frozen=True, slots=True)
class CalendarRestoreSourceProjection:
    """表示恢复入队前锁定的最小持久历史事实。

    该投影同时保留 snapshot 类别、源提案生命周期、本地目标事件 UUID 与密文保留状态。
    API 只能在这些事实全部验证后创建恢复任务，且不得把 path 事件或其它动态字段复制进
    Worker 的持久输入。
    """

    source_snapshot_id: UUID
    source_proposal_id: UUID
    snapshot_kind: str
    operation_kind: str
    proposal_status: CalendarProposalStatus
    target_event_id: str | None
    target_local_event_id: UUID | None
    retain_until: datetime
    ciphertext_present: bool


@dataclass(frozen=True, slots=True)
class CalendarProposalTargetSnapshot:
    """表示本地目录、连接能力与保留设置组成的最小写目标投影。"""

    connection_id: UUID
    calendar_id: str
    provider: str
    timezone: str
    connection_status: ConnectionStatus
    read_capability_status: CapabilityStatus
    write_capability_status: CapabilityStatus
    write_capability_error_code: str | None
    can_write: bool
    retention_days: int = DEFAULT_CALENDAR_CONTENT_RETENTION_DAYS
    supported_notification_policies: frozenset[NotificationPolicy] = frozenset(NotificationPolicy)


@dataclass(frozen=True, slots=True)
class CalendarProposalEventBinding:
    """表示从本地 UUID 解析出的非敏感、不可拆分供应商事件身份。"""

    event_id: UUID
    connection_id: UUID
    calendar_id: str
    provider_event_id: str


@dataclass(frozen=True, slots=True)
class CalendarProposalEventSnapshot:
    """表示本地最新事件及其不可拆分的精确供应商身份。"""

    event_id: UUID
    connection_id: UUID
    calendar_id: str
    provider: str
    provider_event_id: str
    title: str
    description: str
    location: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    timezone: str
    attendees: tuple[str, ...]
    recurring_event_id: str | None
    etag: str | None
    status: str
    can_edit: bool


@dataclass(frozen=True, slots=True)
class CalendarAvailabilityContext:
    """表示建议算法需要的设置、本人事件和缺失连接事实。"""

    timezone: str
    working_hours: WeeklyWorkingHours
    meeting_buffer: timedelta
    events: tuple[AvailabilityEvent, ...]
    missing_connection_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class CalendarAvailabilityRead:
    """冻结短读事务输出，供事务外纯候选计算使用。

    ``proposal`` 与 ``context`` 都是应用层不可变 DTO；adapter 关闭读事务后不得保留或
    复用 ORM 对象。写阶段只使用其中的标量身份、版本和完整 desired mapping。
    """

    proposal: CalendarProposalSnapshot
    context: CalendarAvailabilityContext


class CalendarProposalContent(BaseModel):
    """表示编辑态快照中的完整期望状态与本地控制元数据。

    时间使用规范 ISO 字符串，使不可变 snapshot 保持标准 JSON；模型不接收或产生该结构。
    ``availability`` 只缓存本人日历确定性结果，类型中固定包含
    ``attendee_availability_checked=False``。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    title: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=100_000)
    location: str | None = Field(default=None, max_length=16_384)
    starts_at: str | None = None
    ends_at: str | None = None
    timezone: str | None = None
    all_day: bool | None = None
    attendees: tuple[str, ...] = ()
    notification_policy: NotificationPolicy | None = None
    changed_fields: tuple[str, ...] = ()
    confirmed_fields: tuple[str, ...] = ()
    required_confirmations: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    notification_policy_user_set: bool = False
    requires_explicit_confirmation: bool = False
    availability: AvailabilityResult | None = None

    @field_validator("title", "description", "location")
    @classmethod
    def safe_text(cls, value: str | None) -> str | None:
        """把三类文本收窄为字符串；结构化日历字段允许保留普通换行。"""
        if value is not None and not isinstance(value, str):
            raise TypeError("calendar text fields must be strings or None")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        """验证显式 IANA 时区；未填写只允许出现在未完成 shell。"""
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("calendar proposal timezone must be a valid IANA name") from error
        return value

    @field_validator("attendees")
    @classmethod
    def valid_attendees(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """规范化、跨字段去重最多五十个参会人地址。"""
        if not isinstance(value, tuple):
            raise TypeError("calendar proposal attendees must be a tuple")
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in value:
            address = normalize_mailbox_address(raw)
            if address in seen:
                continue
            seen.add(address)
            normalized.append(address)
        if len(normalized) > 50:
            raise ValueError("calendar proposal attendees must contain at most 50 addresses")
        return tuple(normalized)

    @field_validator("changed_fields")
    @classmethod
    def valid_changed_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """要求完整 diff 字段排序、唯一且只引用日程内容。"""
        if tuple(sorted(set(value))) != value or not set(value).issubset(_EVENT_FIELDS):
            raise ValueError("calendar proposal changed_fields are invalid")
        return value

    @field_validator("confirmed_fields", "required_confirmations")
    @classmethod
    def valid_confirmation_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """要求确认字段来自固定枚举且唯一，并规范旧 snapshot 的字母顺序。"""
        if any(item not in _SHELL_CONFIRMATIONS for item in value):
            raise ValueError("calendar proposal confirmation fields are invalid")
        if len(set(value)) != len(value):
            raise ValueError("calendar proposal confirmation fields must be unique")
        ordered = tuple(item for item in _SHELL_CONFIRMATIONS if item in value)
        return ordered

    @field_validator("source_event_ids")
    @classmethod
    def unique_text_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """拒绝空值或重复控制字段，保持冻结预览确定性。"""
        if any(not isinstance(item, str) or item == "" for item in value):
            raise ValueError("calendar proposal control fields must be nonempty strings")
        if len(set(value)) != len(value):
            raise ValueError("calendar proposal control fields must be unique")
        return value

    @model_validator(mode="after")
    def valid_interval(self) -> "CalendarProposalContent":
        """校验完整时间对；两端都为空只允许本地未完成 shell。"""
        if self.starts_at is None and self.ends_at is None:
            return self
        if self.starts_at is None or self.ends_at is None or self.all_day is None:
            raise ValueError("calendar proposal interval must be complete")
        start = _parse_snapshot_instant(self.starts_at, all_day=self.all_day)
        end = _parse_snapshot_instant(self.ends_at, all_day=self.all_day)
        if end <= start:
            raise ValueError("calendar proposal ends_at must be after starts_at")
        if self.timezone is None:
            raise ValueError("calendar proposal interval requires timezone")
        return self

    @model_validator(mode="after")
    def confirmation_fields_partition_shell(self) -> "CalendarProposalContent":
        """确保显式确认 shell 的已确认/待确认集合恰好覆盖四类字段。

        历史 snapshot 没有显式确认语义时保持兼容；shell 标记为需要确认后，任意重叠、
        遗漏或额外字段都必须在读取边界拒绝，避免未验证更新进入不可变快照。
        """
        if self.requires_explicit_confirmation:
            confirmed = set(self.confirmed_fields)
            required = set(self.required_confirmations)
            shell = set(_SHELL_CONFIRMATIONS)
            if confirmed & required or confirmed | required != shell:
                raise ValueError("calendar proposal confirmations must partition the shell fields")
        return self


@dataclass(frozen=True, slots=True)
class CalendarProposalView:
    """供 API、Worker 和测试使用的当前编辑态提案视图。"""

    proposal_id: UUID
    connection_id: UUID
    calendar_id: str
    operation_kind: CalendarOperationKind
    target_event_id: str | None
    base_etag: str | None
    before_snapshot_id: UUID | None
    current_version: int
    status: CalendarProposalStatus
    retain_until: datetime
    content: CalendarProposalContent

    @property
    def notification_policy(self) -> NotificationPolicy | None:
        """返回当前可编辑通知策略。"""
        return self.content.notification_policy

    @property
    def changed_fields(self) -> tuple[str, ...]:
        """返回确定性完整 diff 字段。"""
        return self.content.changed_fields

    @property
    def required_confirmations(self) -> tuple[str, ...]:
        """返回提交前仍需用户明确确认的字段组。"""
        return self.content.required_confirmations

    @property
    def submission_ready(self) -> bool:
        """按操作种类判断本地编辑态是否具备提交冻结的完整事实。

        该属性只检查当前本地视图，不创建审批、任务或任何外部写入。四项确认必须完整，
        公共文本身份不能只含空白；update/restore 还必须绑定供应商目标、ETag、稳定 before
        和非空完整 diff，而 create 必须明确没有这三类历史目标绑定。
        """
        content_ready = (
            self.status is CalendarProposalStatus.EDITING
            and self.content.confirmed_fields == _SHELL_CONFIRMATIONS
            and not self.content.required_confirmations
            and self.content.notification_policy is not None
            and self.content.starts_at is not None
            and self.content.ends_at is not None
            and self.content.timezone is not None
            and self.content.all_day is not None
            and isinstance(self.content.title, str)
            and self.content.title.strip() != ""
            and isinstance(self.calendar_id, str)
            and self.calendar_id.strip() != ""
        )
        if not content_ready:
            return False
        if self.operation_kind == "create":
            return (
                self.target_event_id is None
                and self.base_etag is None
                and self.before_snapshot_id is None
            )
        if self.operation_kind in {"update", "restore"}:
            return (
                isinstance(self.target_event_id, str)
                and self.target_event_id.strip() != ""
                and isinstance(self.base_etag, str)
                and self.base_etag.strip() != ""
                and self.before_snapshot_id is not None
                and bool(self.content.changed_fields)
            )
        return False


class CalendarProposalRepository(Protocol):
    """定义版本化 AEAD snapshot 与用户创建幂等所需的持久化端口。"""

    async def get_by_creation_key(
        self, *, user_id: UUID, creation_idempotency_key: str
    ) -> CalendarProposalSnapshot | None: ...

    async def create(
        self,
        *,
        proposal_id: UUID,
        snapshot_id: UUID,
        user_id: UUID,
        connection_id: UUID,
        creation_idempotency_key: str,
        creation_payload_hash: str,
        calendar_id: str,
        operation_kind: CalendarOperationKind,
        target_event_id: str | None,
        base_etag: str | None,
        retain_until: datetime,
        desired_state: Mapping[str, object],
    ) -> CalendarProposalSnapshot: ...

    async def get_current(
        self, *, user_id: UUID, proposal_id: UUID
    ) -> CalendarProposalSnapshot | None: ...

    async def save_next_version(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        proposal_id: UUID,
        expected_version: int,
        desired_state: Mapping[str, object],
        retain_until: datetime,
        connection_id: UUID | None = None,
        calendar_id: str | None = None,
    ) -> CalendarProposalSnapshot | None: ...

    async def save_snapshot(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        proposal_id: UUID,
        version: int,
        snapshot_kind: CalendarSnapshotKind,
        content: Mapping[str, object],
        retain_until: datetime,
    ) -> CalendarSnapshot | None: ...

    async def load_snapshot(
        self, *, user_id: UUID, snapshot_id: UUID
    ) -> CalendarSnapshot | None: ...

    async def get_restore_source(
        self, *, user_id: UUID, source_snapshot_id: UUID
    ) -> CalendarRestoreSourceSnapshot | None: ...

    async def list_current(
        self,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[CalendarProposalSnapshot, ...]: ...

    async def cancel(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> CalendarProposalSnapshot | None: ...


class CalendarProposalSourceReader(Protocol):
    """定义本地连接、目录、事件、设置与本人可用性读取端口。"""

    async def get_default_proposal_target(
        self, *, user_id: UUID
    ) -> CalendarProposalTargetSnapshot | None: ...

    async def get_proposal_target(
        self, *, user_id: UUID, connection_id: UUID, calendar_id: str
    ) -> CalendarProposalTargetSnapshot | None: ...

    async def get_proposal_event_binding(
        self, *, user_id: UUID, event_id: UUID
    ) -> CalendarProposalEventBinding | None: ...

    async def get_proposal_event(
        self, *, user_id: UUID, binding: CalendarProposalEventBinding
    ) -> CalendarProposalEventSnapshot | None: ...

    async def get_availability_context(
        self,
        *,
        user_id: UUID,
        observed_at: datetime,
        search_start: datetime,
        horizon_days: int,
    ) -> CalendarAvailabilityContext | None: ...


class CalendarAvailabilityPersistence(Protocol):
    """定义 suggestion 的两短事务持久化边界。

    实现必须让 ``load_suggestion`` 的事务在返回前结束；候选函数随后在应用层运行；
    ``save_suggestion`` 再用独立事务和 ``expected_version`` CAS 写入下一不可变版本。
    """

    async def load_suggestion(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        observed_at: datetime,
        search_start: datetime,
        horizon_days: int,
    ) -> CalendarAvailabilityRead | None: ...

    async def save_suggestion(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        proposal_id: UUID,
        expected_version: int,
        desired_state: Mapping[str, object],
        retain_until: datetime,
    ) -> CalendarProposalSnapshot | None: ...


class CalendarProposalNotFoundError(Exception):
    """表示提案、事件、snapshot 或目标不存在/跨用户，避免资源探测。"""


class CalendarProposalUseCase:
    """协调本地日历提案短事务，不执行审批或外部写入。"""

    def __init__(
        self,
        *,
        proposals: CalendarProposalRepository,
        calendar: CalendarProposalSourceReader,
        clock: Callable[[], datetime],
        id_factory: Callable[[], UUID] = uuid4,
        availability: CalendarAvailabilityPersistence | None = None,
        suggestion_function: Callable[..., AvailabilityResult] = suggest_meeting_times,
    ) -> None:
        """注入提案、本人日历、可选两短事务端口、纯候选函数、时钟和 UUID。

        ``availability`` 对创建、编辑和恢复不是必需依赖，因此构造器保留可选；但是
        ``suggest_times()`` 必须显式注入该端口，否则会在访问任何仓储前稳定失败。
        """
        self._proposals = proposals
        self._calendar = calendar
        self._clock = clock
        self._id_factory = id_factory
        self._availability = availability
        self._suggestion_function = suggestion_function

    async def list(
        self,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[CalendarProposalView, ...]:
        """按当前用户与稳定分页参数列出提案当前版本。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("calendar proposal limit must be between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("calendar proposal offset must be nonnegative")
        return tuple(
            _to_view(snapshot)
            for snapshot in await self._proposals.list_current(
                user_id=user_id,
                limit=limit,
                offset=offset,
            )
        )

    async def get(self, *, user_id: UUID, proposal_id: UUID) -> CalendarProposalView:
        """读取当前用户拥有的提案，不存在或跨用户时统一隐藏。"""
        snapshot = await self._proposals.get_current(
            user_id=user_id,
            proposal_id=proposal_id,
        )
        if snapshot is None:
            raise CalendarProposalNotFoundError
        return _to_view(snapshot)

    async def cancel(self, *, user_id: UUID, proposal_id: UUID) -> CalendarProposalView:
        """取消仍处于 editing/stale 的纯本地提案，不产生任务或外部副作用。"""
        snapshot = await self._proposals.cancel(
            user_id=user_id,
            proposal_id=proposal_id,
        )
        if snapshot is None:
            raise CalendarProposalNotFoundError
        return _to_view(snapshot)

    async def create_shell(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
    ) -> CalendarProposalView:
        """在显式默认可写日历上创建未确认的本地编辑 shell。

        默认连接和日历只用于建立本地归属，不等同于用户已确认冻结目标。时间、参会人和
        通知均保持未完成，后续提交必须检查 ``required_confirmations``。
        """
        target = await self._calendar.get_default_proposal_target(user_id=user_id)
        target = _require_writable_target(target)
        operation_id = self._id_factory()
        content = CalendarProposalContent(
            operation_id=operation_id,
            timezone=target.timezone,
            required_confirmations=_SHELL_CONFIRMATIONS,
            requires_explicit_confirmation=True,
        )
        snapshot = await self._proposals.create(
            proposal_id=self._id_factory(),
            snapshot_id=self._id_factory(),
            user_id=user_id,
            connection_id=target.connection_id,
            creation_idempotency_key=validate_calendar_creation_idempotency_key(idempotency_key),
            creation_payload_hash=_creation_hash(
                connection_id=target.connection_id,
                calendar_id=target.calendar_id,
                operation_kind="create",
                target_event_id=None,
                content=content,
            ),
            calendar_id=target.calendar_id,
            operation_kind="create",
            target_event_id=None,
            base_etag=None,
            retain_until=_retain_until(self._clock, target.retention_days),
            desired_state=_content_json(content),
        )
        return _to_view(snapshot)

    async def create_event(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        calendar_id: str,
        idempotency_key: str,
        values: Mapping[str, object],
    ) -> CalendarProposalView:
        """创建一个字段齐全的非重复本地日程提案。"""
        target = _require_writable_target(
            await self._calendar.get_proposal_target(
                user_id=user_id,
                connection_id=connection_id,
                calendar_id=calendar_id,
            )
        )
        operation_id = self._id_factory()
        prepared = _prepare_create_content(values, operation_id=operation_id, target=target)
        snapshot = await self._proposals.create(
            proposal_id=self._id_factory(),
            snapshot_id=self._id_factory(),
            user_id=user_id,
            connection_id=connection_id,
            creation_idempotency_key=validate_calendar_creation_idempotency_key(idempotency_key),
            creation_payload_hash=_creation_hash(
                connection_id=connection_id,
                calendar_id=calendar_id,
                operation_kind="create",
                target_event_id=None,
                content=prepared,
            ),
            calendar_id=calendar_id,
            operation_kind="create",
            target_event_id=None,
            base_etag=None,
            retain_until=_retain_until(self._clock, target.retention_days),
            desired_state=_content_json(prepared),
        )
        return _to_view(snapshot)

    async def create_update(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
        changes: Mapping[str, object],
        idempotency_key: str | None = None,
    ) -> CalendarProposalView:
        """从本地最新事件创建修改提案并保存加密 before snapshot。"""
        binding = await self._calendar.get_proposal_event_binding(
            user_id=user_id,
            event_id=event_id,
        )
        if binding is None:
            raise CalendarProposalNotFoundError
        target = _require_writable_target(
            await self._calendar.get_proposal_target(
                user_id=user_id,
                connection_id=binding.connection_id,
                calendar_id=binding.calendar_id,
            )
        )
        event = await self._calendar.get_proposal_event(
            user_id=user_id,
            binding=binding,
        )
        if event is None:
            raise CalendarProposalNotFoundError
        _validate_local_update_event(event, target)
        # recurrence 必须先返回稳定领域错误；对象形状不能被通用幂等哈希边界
        # 提前降级为没有 error_code 的 TypeError。
        _reject_recurrence_input(changes)
        creation_key = validate_calendar_creation_idempotency_key(
            idempotency_key or _derived_update_key(event_id=event_id, changes=changes)
        )
        # 创建哈希明确排除随机 operation_id，因此先用零 UUID 形成规范请求，才能在
        # 重放路径上不消耗任何新 ID，同时仍对同键异载荷执行常量时间拒绝。
        hash_before = _content_from_local_event(event, operation_id=UUID(int=0))
        hash_desired = _apply_user_changes(
            hash_before,
            changes,
            operation_kind="update",
            target=target,
            source_event_ids=(str(event.event_id),),
        )
        creation_payload_hash = _creation_hash(
            connection_id=event.connection_id,
            calendar_id=event.calendar_id,
            operation_kind="update",
            target_event_id=event.provider_event_id,
            content=hash_desired,
            base_etag=cast(str, event.etag),
        )
        existing = await self._proposals.get_by_creation_key(
            user_id=user_id,
            creation_idempotency_key=creation_key,
        )
        if existing is not None:
            _require_matching_creation_hash(existing, creation_payload_hash)
            if existing.before_snapshot_id is not None:
                return _to_view(existing)
            # 若首次事务只留下 desired，重放沿用已持久化 operation_id 修复同一逻辑
            # 位置的 before；绝不生成第二个提案或替换既有 desired。
            persisted = _to_view(existing)
            before_values = hash_before.model_dump(mode="python")
            before_values["operation_id"] = persisted.content.operation_id
            before = CalendarProposalContent.model_validate(before_values)
            snapshot = existing
            retain_until = existing.retain_until
        else:
            operation_id = self._id_factory()
            before_values = hash_before.model_dump(mode="python")
            before_values["operation_id"] = operation_id
            before = CalendarProposalContent.model_validate(before_values)
            desired_values = hash_desired.model_dump(mode="python")
            desired_values["operation_id"] = operation_id
            desired = CalendarProposalContent.model_validate(desired_values)
            retain_until = _retain_until(self._clock, target.retention_days)
            snapshot = await self._proposals.create(
                proposal_id=self._id_factory(),
                snapshot_id=self._id_factory(),
                user_id=user_id,
                connection_id=event.connection_id,
                creation_idempotency_key=creation_key,
                creation_payload_hash=creation_payload_hash,
                calendar_id=event.calendar_id,
                operation_kind="update",
                target_event_id=event.provider_event_id,
                base_etag=cast(str, event.etag),
                retain_until=retain_until,
                desired_state=_content_json(desired),
            )
        # 重放可能返回旧提案；before 必须使用已持久化 operation_id，避免随机新 ID 令
        # 同一逻辑位置的不可变 snapshot 哈希发生变化。
        persisted = _to_view(snapshot)
        persisted_before_values = before.model_dump(mode="python")
        persisted_before_values["operation_id"] = persisted.content.operation_id
        before = CalendarProposalContent.model_validate(persisted_before_values)
        saved_before = await self._proposals.save_snapshot(
            snapshot_id=self._id_factory(),
            user_id=user_id,
            proposal_id=snapshot.proposal_id,
            version=1,
            snapshot_kind="before",
            content=_content_json(before),
            retain_until=retain_until,
        )
        if saved_before is None:
            raise CalendarProposalNotFoundError
        refreshed = await self._proposals.get_current(
            user_id=user_id,
            proposal_id=snapshot.proposal_id,
        )
        if refreshed is None:
            raise CalendarProposalNotFoundError
        return _to_view(refreshed)

    async def edit(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        expected_version: int,
        changes: Mapping[str, object],
        internal_cache_update: bool = False,
    ) -> CalendarProposalView:
        """以版本 CAS 保存下一编辑版本，并在时间变化时失效旧可用性缓存。"""
        current = await self._proposals.get_current(
            user_id=user_id,
            proposal_id=proposal_id,
        )
        if current is None:
            raise CalendarProposalNotFoundError
        if current.status is not CalendarProposalStatus.EDITING:
            raise StateConflictError(
                error_code="calendar_proposal_not_editable",
                message="calendar proposal is not editable",
            )
        target = _require_writable_target(
            await self._calendar.get_proposal_target(
                user_id=user_id,
                connection_id=current.connection_id,
                calendar_id=current.calendar_id,
            )
        )
        content = CalendarProposalContent.model_validate(current.desired_snapshot.content)
        if internal_cache_update:
            if set(changes) != {"availability"}:
                raise ValueError("internal cache update may only replace availability")
            updated_values = content.model_dump(mode="python")
            updated_values["availability"] = changes["availability"]
            updated = CalendarProposalContent.model_validate(updated_values)
        else:
            _reject_recurrence_input(changes)
            if not set(changes).issubset(_EDITABLE_FIELDS):
                raise ValueError("calendar proposal edit contains unsupported fields")
            updated = _apply_user_changes(
                content,
                changes,
                operation_kind=current.operation_kind,
                target=target,
                source_event_ids=content.source_event_ids,
                before=await self._before_content(user_id=user_id, snapshot=current),
            )
            if set(changes).intersection(_TIME_FIELDS):
                updated_values = updated.model_dump(mode="python")
                updated_values["availability"] = None
                updated = CalendarProposalContent.model_validate(updated_values)
        saved = await self._proposals.save_next_version(
            snapshot_id=self._id_factory(),
            user_id=user_id,
            proposal_id=proposal_id,
            expected_version=expected_version,
            desired_state=_content_json(updated),
            retain_until=_retain_until(self._clock, target.retention_days),
        )
        if saved is None:
            raise CalendarProposalNotFoundError
        return _to_view(saved)

    async def suggest_times(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        expected_version: int | None,
        search_start: datetime,
    ) -> CalendarProposalView:
        """用独立 freshness 时钟计算候选，并以版本 CAS 缓存下一不可变版本。

        ``CalendarAvailabilityPersistence`` 的读事务在本方法获得冻结 DTO 前已经提交，
        纯候选函数运行期间没有数据库事务，随后才开启独立写事务。构造器允许不注入该
        端口以服务其他提案操作，但建议入口会 fail closed，绝不静默退回 caller-owned
        repository 路径而绕过 expected-version CAS。

        Raises:
            StateConflictError: 未注入两短事务持久化端口，或当前提案不是定时事件。
            CalendarProposalNotFoundError: 提案或其可用性上下文不存在/不属于当前用户。
        """
        availability_persistence = self._availability
        if availability_persistence is None:
            raise _calendar_availability_persistence_unavailable()
        observed_at = _aware_utc(self._clock(), field="calendar proposal clock")
        loaded = await availability_persistence.load_suggestion(
            user_id=user_id,
            proposal_id=proposal_id,
            observed_at=observed_at,
            search_start=search_start,
            horizon_days=CALENDAR_AVAILABILITY_HORIZON_DAYS,
        )
        if loaded is None:
            raise CalendarProposalNotFoundError
        current = loaded.proposal
        context = loaded.context
        content = CalendarProposalContent.model_validate(current.desired_snapshot.content)
        if content.starts_at is None or content.ends_at is None or content.all_day is not False:
            raise StateConflictError(
                error_code="calendar_proposal_time_required",
                message="timed calendar proposal is required for availability suggestions",
            )
        start = _parse_timed_instant(content.starts_at)
        end = _parse_timed_instant(content.ends_at)
        availability = self._suggestion_function(
            requested_duration=end - start,
            search_start=search_start,
            timezone=context.timezone,
            working_hours=context.working_hours,
            meeting_buffer=context.meeting_buffer,
            events=context.events,
            missing_connection_ids=context.missing_connection_ids,
            horizon_days=CALENDAR_AVAILABILITY_HORIZON_DAYS,
            grid_minutes=CALENDAR_AVAILABILITY_GRID_MINUTES,
            limit=CALENDAR_AVAILABILITY_LIMIT,
        )
        values = content.model_dump(mode="python")
        values["availability"] = availability
        updated = CalendarProposalContent.model_validate(values)
        version_for_save = current.current_version if expected_version is None else expected_version
        saved = await availability_persistence.save_suggestion(
            snapshot_id=self._id_factory(),
            user_id=user_id,
            proposal_id=proposal_id,
            expected_version=version_for_save,
            desired_state=_content_json(updated),
            retain_until=current.retain_until,
        )
        if saved is None:
            raise CalendarProposalNotFoundError
        return _to_view(saved)

    async def confirm(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
        expected_version: int,
        confirmation: CalendarConfirmation,
        connection_id: UUID | None = None,
        calendar_id: str | None = None,
    ) -> CalendarProposalView:
        """逐项确认 shell 当前值，并可原子选择精确可写日历。

        普通编辑永远不能调用本路径的等价隐式行为。日历确认必须携带连接与目录 ID；
        其余确认只确认当前 snapshot 中已经完整、可验证的值。每次调用推进一个不可变版本，
        使后续 API 能以版本 CAS 审计用户逐项确认顺序。
        """
        normalized_confirmation = _calendar_confirmation(confirmation)
        current = await self._proposals.get_current(
            user_id=user_id,
            proposal_id=proposal_id,
        )
        if current is None:
            raise CalendarProposalNotFoundError
        if current.status is not CalendarProposalStatus.EDITING:
            raise StateConflictError(
                error_code="calendar_proposal_not_editable",
                message="calendar proposal is not editable",
            )
        content = CalendarProposalContent.model_validate(current.desired_snapshot.content)
        if not content.requires_explicit_confirmation:
            raise StateConflictError(
                error_code="calendar_confirmation_not_required",
                message="calendar proposal does not require explicit field confirmation",
            )

        selected_connection_id = current.connection_id
        selected_calendar_id = current.calendar_id
        if normalized_confirmation == "calendar":
            if (
                not isinstance(connection_id, UUID)
                or not isinstance(calendar_id, str)
                or not calendar_id
            ):
                raise ValueError("calendar confirmation requires connection_id and calendar_id")
            target = _require_writable_target(
                await self._calendar.get_proposal_target(
                    user_id=user_id,
                    connection_id=connection_id,
                    calendar_id=calendar_id,
                )
            )
            selected_connection_id = connection_id
            selected_calendar_id = calendar_id
        else:
            if connection_id is not None or calendar_id is not None:
                raise ValueError("only calendar confirmation accepts a target identity")
            target = _require_writable_target(
                await self._calendar.get_proposal_target(
                    user_id=user_id,
                    connection_id=current.connection_id,
                    calendar_id=current.calendar_id,
                )
            )

        _require_confirmation_value(content, normalized_confirmation, target=target)
        confirmed = _ordered_confirmations({*content.confirmed_fields, normalized_confirmation})
        updated_values = content.model_dump(mode="python")
        updated_values.update(
            {
                "confirmed_fields": confirmed,
                "required_confirmations": _ordered_confirmations(
                    set(_SHELL_CONFIRMATIONS).difference(confirmed)
                ),
            }
        )
        updated = CalendarProposalContent.model_validate(updated_values)
        saved = await self._proposals.save_next_version(
            snapshot_id=self._id_factory(),
            user_id=user_id,
            proposal_id=proposal_id,
            expected_version=expected_version,
            desired_state=_content_json(updated),
            retain_until=_retain_until(self._clock, target.retention_days),
            connection_id=(
                selected_connection_id if normalized_confirmation == "calendar" else None
            ),
            calendar_id=(selected_calendar_id if normalized_confirmation == "calendar" else None),
        )
        if saved is None:
            raise CalendarProposalNotFoundError
        return _to_view(saved)

    async def create_restore(
        self,
        *,
        user_id: UUID,
        source_snapshot_id: UUID,
        current_event: CalendarEvent | None,
        idempotency_key: str,
    ) -> CalendarProposalView:
        """把历史 before 与供应商当前事件比较后创建全新恢复提案。

        调用方必须已经在数据库事务外完成 ``get_current_event``。本方法在当前短事务内
        重新校验 source 归属、连接能力、目录写权限和 provider 当前事实，然后同时保存
        新 desired、当前 before、当前 ETag 与新 operation identity。
        """
        creation_key = validate_calendar_creation_idempotency_key(idempotency_key)
        existing = await self._proposals.get_by_creation_key(
            user_id=user_id,
            creation_idempotency_key=creation_key,
        )
        if existing is not None:
            # 恢复创建键绑定的是第一次读取到的 ETag、历史 source 和完整 desired。重放
            # 必须先复用这份已冻结事实；否则供应商事件在第一次成功后被删除或版本变化时，
            # 重放会错误地先执行 provider GET，既破坏幂等响应，也可能把同键请求误报为
            # 外部冲突。非 restore 提案占用同一用户键时直接拒绝，避免跨操作种类复用。
            if existing.operation_kind != "restore":
                raise StateConflictError(
                    error_code="idempotency_key_payload_mismatch",
                    message="idempotency key is already bound to different content",
                )
            try:
                persisted_content = CalendarProposalContent.model_validate(
                    existing.desired_snapshot.content
                )
                persisted_hash = _creation_hash(
                    connection_id=existing.connection_id,
                    calendar_id=existing.calendar_id,
                    operation_kind="restore",
                    target_event_id=existing.target_event_id,
                    content=persisted_content,
                    base_etag=existing.base_etag,
                    source_snapshot_id=source_snapshot_id,
                )
            except (TypeError, ValueError):
                # 已持久化内容损坏时不能把它当作安全重放；让调用方进入稳定的
                # idempotency/conflict 处理，而不是访问供应商或生成第二个提案。
                raise StateConflictError(
                    error_code="idempotency_key_payload_mismatch",
                    message="idempotency key is already bound to invalid content",
                ) from None
            _require_matching_creation_hash(existing, persisted_hash)
            return _to_view(existing)
        source = await self._proposals.get_restore_source(
            user_id=user_id,
            source_snapshot_id=source_snapshot_id,
        )
        if source is None:
            raise CalendarProposalNotFoundError
        target = _require_writable_target(
            await self._calendar.get_proposal_target(
                user_id=user_id,
                connection_id=source.connection_id,
                calendar_id=source.calendar_id,
            )
        )
        current = _validate_provider_restore_event(current_event, source)
        historical_snapshot = await self._proposals.load_snapshot(
            user_id=user_id,
            snapshot_id=source_snapshot_id,
        )
        if historical_snapshot is None or historical_snapshot.snapshot_kind != "before":
            raise CalendarProposalNotFoundError
        historical = CalendarProposalContent.model_validate(historical_snapshot.content)
        operation_id = self._id_factory()
        current_content = _content_from_provider_event(current, operation_id=operation_id)
        changed_fields = _changed_fields(current_content, historical)
        notification = _default_notification("restore", historical.attendees, changed_fields)
        desired_values = historical.model_dump(mode="python")
        desired_values.update(
            {
                "operation_id": operation_id,
                "notification_policy": notification,
                "notification_policy_user_set": False,
                "changed_fields": changed_fields,
                "confirmed_fields": _SHELL_CONFIRMATIONS,
                "required_confirmations": (),
                "requires_explicit_confirmation": False,
                "source_event_ids": (source.provider_event_id,),
                "availability": None,
            }
        )
        desired = CalendarProposalContent.model_validate(desired_values)
        _require_supported_notification(target, notification)
        retain_until = _retain_until(self._clock, target.retention_days)
        snapshot = await self._proposals.create(
            proposal_id=self._id_factory(),
            snapshot_id=self._id_factory(),
            user_id=user_id,
            connection_id=source.connection_id,
            creation_idempotency_key=creation_key,
            creation_payload_hash=_creation_hash(
                connection_id=source.connection_id,
                calendar_id=source.calendar_id,
                operation_kind="restore",
                target_event_id=source.provider_event_id,
                content=desired,
                base_etag=cast(str, current.etag),
                source_snapshot_id=source_snapshot_id,
            ),
            calendar_id=source.calendar_id,
            operation_kind="restore",
            target_event_id=source.provider_event_id,
            base_etag=cast(str, current.etag),
            retain_until=retain_until,
            desired_state=_content_json(desired),
        )
        persisted = _to_view(snapshot)
        current_values = current_content.model_dump(mode="python")
        current_values["operation_id"] = persisted.content.operation_id
        current_content = CalendarProposalContent.model_validate(current_values)
        saved_before = await self._proposals.save_snapshot(
            snapshot_id=self._id_factory(),
            user_id=user_id,
            proposal_id=snapshot.proposal_id,
            version=1,
            snapshot_kind="before",
            content=_content_json(current_content),
            retain_until=retain_until,
        )
        if saved_before is None:
            raise CalendarProposalNotFoundError
        refreshed = await self._proposals.get_current(
            user_id=user_id,
            proposal_id=snapshot.proposal_id,
        )
        if refreshed is None:
            raise CalendarProposalNotFoundError
        return _to_view(refreshed)

    async def _before_content(
        self,
        *,
        user_id: UUID,
        snapshot: CalendarProposalSnapshot,
    ) -> CalendarProposalContent | None:
        """读取修改/恢复的稳定 before；创建提案没有该事实。"""
        if snapshot.before_snapshot_id is None:
            return None
        before = await self._proposals.load_snapshot(
            user_id=user_id,
            snapshot_id=snapshot.before_snapshot_id,
        )
        if before is None:
            raise CalendarProposalNotFoundError
        return CalendarProposalContent.model_validate(before.content)


class CalendarRestoreEnqueueRepository(Protocol):
    """定义恢复准备任务验证与原子创建所需的窄事务端口。"""

    async def enqueue_restore_prepare(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
        source_snapshot_id: UUID,
        creation_idempotency_key: str,
        now: datetime,
    ) -> CreateTaskResult: ...


class CalendarRestoreEnqueueRepositoryFactory(Protocol):
    """为一次恢复请求创建自动提交或回滚的短事务。"""

    def __call__(self) -> AbstractAsyncContextManager[CalendarRestoreEnqueueRepository]: ...


class CalendarRestoreEnqueueUseCase:
    """验证历史 before 快照并排队 ``calendar.restore.prepare``。

    路由只解析用户输入；本边界要求仓储先锁定、验证 source 的归属、类别、生命周期、
    精确本地事件绑定和密文保留，再在同一事务内创建 TaskRun、AuditEvent 与 Outbox。
    任务输入严格只有 source snapshot ID 与恢复提案创建幂等键；本用例不创建恢复提案、
    ApprovalRequest 或冻结命令。
    """

    def __init__(
        self,
        repositories: CalendarRestoreEnqueueRepositoryFactory,
        dispatcher: TaskDispatcher,
        clock: Callable[[], datetime],
    ) -> None:
        """保存事务工厂、提交后投递器与显式 UTC 时钟。"""
        self._repositories = repositories
        self._dispatcher = dispatcher
        self._clock = clock

    async def execute(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
        source_snapshot_id: UUID,
        creation_idempotency_key: str,
    ) -> CreateTaskResult:
        """创建或精确重放一个恢复准备任务，并返回其持久状态。"""
        checked_key = validate_calendar_creation_idempotency_key(creation_idempotency_key)
        now = _aware_utc(self._clock(), field="calendar restore clock")
        async with self._repositories() as repository:
            result = await repository.enqueue_restore_prepare(
                user_id=user_id,
                event_id=event_id,
                source_snapshot_id=source_snapshot_id,
                creation_idempotency_key=checked_key,
                now=now,
            )
        return CreateTaskResult(
            task_id=result.task_id,
            status=await self._dispatcher.dispatch(result.task_id),
        )


def parse_calendar_proposal_conversation_request(text: str) -> bool:
    """确定性识别“准备日历提案”命令，不提取或猜测任何写入字段。

    直接创建/修改事件、能力询问、疑问句和否定句都返回 ``False``。模型分类结果不能调用
    本函数的替代入口，因此只有原始持久用户消息明确命中才允许 Worker 创建 editing shell。
    """
    if not isinstance(text, str):
        return False
    normalized = " ".join(text.casefold().strip().split())
    return normalized != "" and any(
        pattern.fullmatch(normalized) is not None for pattern in _SAFE_CALENDAR_COMMANDS
    )


def _prepare_create_content(
    values: Mapping[str, object],
    *,
    operation_id: UUID,
    target: CalendarProposalTargetSnapshot,
) -> CalendarProposalContent:
    """规范创建输入、填充默认通知并拒绝 recurrence/扩展字段。"""
    if not isinstance(values, Mapping):
        raise TypeError("calendar create values must be a mapping")
    _reject_recurrence_input(values)
    if not set(values).issubset(_EDITABLE_FIELDS):
        raise ValueError("calendar create values contain unsupported fields")
    normalized = _normalized_changes(values)
    attendees = cast(tuple[str, ...], normalized.get("attendees", ()))
    notification_raw = normalized.get("notification_policy")
    notification = (
        _notification_policy(notification_raw)
        if notification_raw is not None
        else _default_notification("create", attendees, ())
    )
    _require_supported_notification(target, notification)
    content = CalendarProposalContent(
        operation_id=operation_id,
        title=cast(str | None, normalized.get("title")),
        description=cast(str | None, normalized.get("description")),
        location=cast(str | None, normalized.get("location")),
        starts_at=cast(str | None, normalized.get("starts_at")),
        ends_at=cast(str | None, normalized.get("ends_at")),
        timezone=cast(str | None, normalized.get("timezone", target.timezone)),
        all_day=cast(bool | None, normalized.get("all_day")),
        attendees=attendees,
        notification_policy=notification,
        changed_fields=tuple(sorted(set(values).intersection(_EVENT_FIELDS))),
        confirmed_fields=_SHELL_CONFIRMATIONS,
        notification_policy_user_set=notification_raw is not None,
    )
    if not content.title:
        raise ValueError("calendar create proposal requires title")
    return content


def _content_from_local_event(
    event: CalendarProposalEventSnapshot,
    *,
    operation_id: UUID,
) -> CalendarProposalContent:
    """把已解密本地事件复制成不含供应商 SDK 类型的完整 snapshot。"""
    return CalendarProposalContent(
        operation_id=operation_id,
        title=event.title,
        description=event.description,
        location=event.location,
        starts_at=_snapshot_instant(
            event.starts_at,
            all_day=event.all_day,
            timezone=event.timezone,
        ),
        ends_at=_snapshot_instant(
            event.ends_at,
            all_day=event.all_day,
            timezone=event.timezone,
        ),
        timezone=event.timezone,
        all_day=event.all_day,
        attendees=event.attendees,
        source_event_ids=(str(event.event_id),),
    )


def _content_from_provider_event(
    event: CalendarEvent,
    *,
    operation_id: UUID,
) -> CalendarProposalContent:
    """把事务外精确 GET 结果规范成恢复提案的当前 before snapshot。"""
    if event.starts_at is None or event.ends_at is None:
        raise _calendar_event_deleted()
    return CalendarProposalContent(
        operation_id=operation_id,
        title=event.title,
        description=event.description,
        location=event.location,
        starts_at=_snapshot_instant(
            event.starts_at,
            all_day=event.all_day,
            timezone=event.timezone,
        ),
        ends_at=_snapshot_instant(
            event.ends_at,
            all_day=event.all_day,
            timezone=event.timezone,
        ),
        timezone=event.timezone,
        all_day=event.all_day,
        attendees=_provider_attendees(event),
        source_event_ids=(event.event_id,),
    )


def _apply_user_changes(
    content: CalendarProposalContent,
    changes: Mapping[str, object],
    *,
    operation_kind: CalendarOperationKind,
    target: CalendarProposalTargetSnapshot,
    source_event_ids: tuple[str, ...],
    before: CalendarProposalContent | None = None,
) -> CalendarProposalContent:
    """应用白名单编辑并重新计算 diff、通知默认和缓存失效。"""
    if not isinstance(changes, Mapping):
        raise TypeError("calendar proposal changes must be a mapping")
    _reject_recurrence_input(changes)
    if not set(changes).issubset(_EDITABLE_FIELDS):
        raise ValueError("calendar proposal changes contain unsupported fields")
    normalized = _normalized_changes(changes)
    values = content.model_dump(mode="python")
    values.update(normalized)
    values["source_event_ids"] = source_event_ids
    user_set_notification = content.notification_policy_user_set
    notification: NotificationPolicy | None
    if "notification_policy" in normalized:
        notification = _notification_policy(normalized["notification_policy"])
        user_set_notification = True
    else:
        notification = content.notification_policy
    values["notification_policy_user_set"] = user_set_notification
    candidate = CalendarProposalContent.model_validate(values)
    baseline = before or content
    changed_fields = _changed_fields(baseline, candidate)
    if not user_set_notification:
        notification = _default_notification(
            operation_kind,
            candidate.attendees,
            changed_fields,
        )
    if notification is None:
        raise ValueError("calendar proposal notification policy is required")
    _require_supported_notification(target, notification)
    if content.requires_explicit_confirmation:
        invalidated = _invalidated_confirmations(set(changes))
        confirmed = _ordered_confirmations(set(content.confirmed_fields).difference(invalidated))
        required = _ordered_confirmations(set(_SHELL_CONFIRMATIONS).difference(confirmed))
    else:
        confirmed = _SHELL_CONFIRMATIONS
        required = ()
    final_values = candidate.model_dump(mode="python")
    final_values.update(
        {
            "notification_policy": notification,
            "notification_policy_user_set": user_set_notification,
            "changed_fields": changed_fields,
            "required_confirmations": required,
            "confirmed_fields": confirmed,
        }
    )
    return CalendarProposalContent.model_validate(final_values)


def _normalized_changes(changes: Mapping[str, object]) -> dict[str, object]:
    """把动态编辑值收窄为 CalendarProposalContent 可验证的内部值。"""
    result: dict[str, object] = {}
    for field, value in changes.items():
        if field in {"starts_at", "ends_at"}:
            if type(value) is date:
                result[field] = value.isoformat()
            elif isinstance(value, datetime):
                result[field] = _aware_utc(value, field=field).isoformat()
            elif isinstance(value, str):
                result[field] = value
            else:
                raise TypeError(f"calendar proposal {field} has invalid type")
        elif field == "attendees":
            if not isinstance(value, (tuple, list)):
                raise TypeError("calendar proposal attendees must be a sequence")
            result[field] = tuple(value)
        elif field == "notification_policy":
            result[field] = _notification_policy(value)
        elif field == "all_day":
            if type(value) is not bool:
                raise TypeError("calendar proposal all_day must be bool")
            result[field] = value
        elif field in {"title", "description", "location", "timezone"}:
            if value is not None and not isinstance(value, str):
                raise TypeError(f"calendar proposal {field} must be a string or None")
            result[field] = value
        else:
            raise ValueError("calendar proposal changes contain unsupported fields")
    return result


def _reject_recurrence_input(values: Mapping[str, object]) -> None:
    """只把明确 recurrence 字段映射为 M2 稳定不支持错误。"""
    if set(values).intersection(_RECURRENCE_FIELDS):
        raise _calendar_recurring_event_unsupported()


def _changed_fields(
    before: CalendarProposalContent,
    desired: CalendarProposalContent,
) -> tuple[str, ...]:
    """比较完整期望状态并返回排序、唯一的字段名 diff。"""
    return tuple(
        sorted(
            field for field in _EVENT_FIELDS if getattr(before, field) != getattr(desired, field)
        )
    )


def _invalidated_confirmations(changed_keys: set[str]) -> set[str]:
    """把普通编辑映射为必须重新显式确认的字段组。"""
    invalidated: set[str] = set()
    if changed_keys.intersection(_TIME_FIELDS):
        invalidated.add("time")
    if "attendees" in changed_keys:
        # 创建提案的默认通知取决于参会人；即使策略值恰好不变也需重新确认其语义。
        invalidated.update(("attendees", "notification_policy"))
    if "notification_policy" in changed_keys:
        invalidated.add("notification_policy")
    return invalidated


def _ordered_confirmations(values: set[str]) -> tuple[str, ...]:
    """按固定产品顺序返回确认字段子集。"""
    return tuple(item for item in _SHELL_CONFIRMATIONS if item in values)


def _calendar_confirmation(value: object) -> CalendarConfirmation:
    """把动态调用值收窄为四种显式确认之一。"""
    if value not in _SHELL_CONFIRMATIONS:
        raise ValueError("calendar proposal confirmation is invalid")
    return cast(CalendarConfirmation, value)


def _require_confirmation_value(
    content: CalendarProposalContent,
    confirmation: CalendarConfirmation,
    *,
    target: CalendarProposalTargetSnapshot,
) -> None:
    """确认前证明当前 snapshot 已包含对应完整值且供应商可表达。"""
    if confirmation == "time" and (
        content.starts_at is None
        or content.ends_at is None
        or content.timezone is None
        or content.all_day is None
    ):
        raise StateConflictError(
            error_code="calendar_proposal_time_required",
            message="calendar proposal time must be complete before confirmation",
        )
    if confirmation in {"calendar", "notification_policy"}:
        if content.notification_policy is None:
            if confirmation == "notification_policy":
                raise StateConflictError(
                    error_code="calendar_notification_policy_required",
                    message="calendar notification policy must be selected before confirmation",
                )
        else:
            _require_supported_notification(target, content.notification_policy)


def _require_matching_creation_hash(
    snapshot: CalendarProposalSnapshot,
    expected_hash: str,
) -> None:
    """常量时间验证创建键仍绑定同一规范载荷，且不回显日程内容。"""
    if not compare_digest(snapshot.creation_payload_hash, expected_hash):
        raise StateConflictError(
            error_code="idempotency_key_payload_mismatch",
            message="idempotency key is already bound to different content",
        )


def _default_notification(
    operation_kind: CalendarOperationKind,
    attendees: tuple[str, ...],
    changed_fields: tuple[str, ...],
) -> NotificationPolicy:
    """按创建参会人或修改/恢复完整 diff 计算默认通知策略。"""
    if operation_kind == "create":
        return NotificationPolicy.ALL if attendees else NotificationPolicy.NONE
    return (
        NotificationPolicy.ALL
        if set(changed_fields).intersection(_NOTIFY_FIELDS)
        else NotificationPolicy.NONE
    )


def _require_writable_target(
    target: CalendarProposalTargetSnapshot | None,
) -> CalendarProposalTargetSnapshot:
    """要求 connected、calendar.read/write enabled 且目录明确可写。"""
    if target is None or target.connection_status is not ConnectionStatus.CONNECTED:
        raise _connection_capability_disabled()
    statuses = (target.read_capability_status, target.write_capability_status)
    if target.write_capability_error_code == "connection_scope_missing" or any(
        status in {CapabilityStatus.ACTION_REQUIRED, CapabilityStatus.REVOKED}
        for status in statuses
    ):
        raise StateConflictError(
            error_code="connection_scope_missing",
            message="calendar write requires reauthorization for the selected connection",
        )
    if any(status is not CapabilityStatus.ENABLED for status in statuses):
        raise _connection_capability_disabled()
    if not target.can_write:
        raise StateConflictError(
            error_code="calendar_read_only",
            message="selected calendar is read-only",
        )
    return target


def _require_supported_notification(
    target: CalendarProposalTargetSnapshot,
    notification: NotificationPolicy,
) -> None:
    """在提交前拒绝供应商无法无损表达的通知映射。"""
    if notification not in target.supported_notification_policies:
        raise StateConflictError(
            error_code="calendar_notification_mapping_unsupported",
            message="selected provider cannot represent the notification policy",
        )


def _validate_local_update_event(
    event: CalendarProposalEventSnapshot,
    target: CalendarProposalTargetSnapshot,
) -> None:
    """验证本地事件绑定、非重复、可编辑和 ETag 前置条件。"""
    if (
        event.connection_id != target.connection_id
        or event.calendar_id != target.calendar_id
        or event.provider != target.provider
    ):
        raise CalendarProposalNotFoundError
    if event.status.casefold() == "cancelled":
        raise _calendar_event_deleted()
    if event.recurring_event_id is not None:
        raise _calendar_recurring_event_unsupported()
    if not event.can_edit:
        raise _calendar_event_not_editable()
    if not isinstance(event.etag, str) or event.etag == "":
        raise StateConflictError(
            error_code="calendar_event_version_conflict",
            message="calendar event version is unavailable",
        )


def _validate_provider_restore_event(
    event: CalendarEvent | None,
    source: CalendarRestoreSourceSnapshot,
) -> CalendarEvent:
    """验证事务外供应商当前事件仍与精确恢复身份一致且可编辑。"""
    if event is None or event.status.casefold() == "cancelled":
        raise _calendar_event_deleted()
    if event.calendar_id != source.calendar_id or event.event_id != source.provider_event_id:
        raise CalendarProposalNotFoundError
    if event.recurring_event_id is not None:
        raise _calendar_recurring_event_unsupported()
    if not event.can_edit:
        raise _calendar_event_not_editable()
    if not isinstance(event.etag, str) or event.etag == "":
        raise StateConflictError(
            error_code="calendar_event_version_conflict",
            message="calendar event version is unavailable",
        )
    if event.starts_at is None or event.ends_at is None:
        raise _calendar_event_deleted()
    return event


def _provider_attendees(event: CalendarEvent) -> tuple[str, ...]:
    """从 provider-neutral attendee 映射抽取规范邮箱，不保留显示名或响应字段。"""
    result: list[str] = []
    for attendee in event.attendees:
        raw = attendee.get("email", attendee.get("address"))
        if isinstance(raw, str):
            result.append(raw)
    return CalendarProposalContent(
        operation_id=UUID(int=0),
        attendees=tuple(result),
    ).attendees


def _content_json(content: CalendarProposalContent) -> dict[str, JsonValue]:
    """把已验证内容复制为标准 JSON，禁止 ORM/枚举/日期对象渗入仓储。"""
    return cast(dict[str, JsonValue], content.model_dump(mode="json"))


def _creation_hash(
    *,
    connection_id: UUID,
    calendar_id: str,
    operation_kind: CalendarOperationKind,
    target_event_id: str | None,
    content: CalendarProposalContent,
    base_etag: str | None = None,
    source_snapshot_id: UUID | None = None,
) -> str:
    """哈希完整规范创建意图，但排除重放时新生成且不会持久化的随机 ID/缓存。"""
    content_values = _content_json(content)
    content_values.pop("operation_id", None)
    content_values.pop("availability", None)
    payload: dict[str, JsonValue] = {
        "connection_id": str(connection_id),
        "calendar_id": calendar_id,
        "operation_kind": operation_kind,
        "target_event_id": target_event_id,
        "base_etag": base_etag,
        "source_snapshot_id": str(source_snapshot_id) if source_snapshot_id else None,
        "content": content_values,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _derived_update_key(*, event_id: UUID, changes: Mapping[str, object]) -> str:
    """为未显式提供请求键的内部调用生成稳定、内容绑定的创建键。"""
    normalized = {key: _hashable_change_value(value) for key, value in sorted(changes.items())}
    digest = sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return f"calendar-update:{event_id}:{digest}"


def _hashable_change_value(value: object) -> JsonValue:
    """把用户编辑值转换为哈希用 JSON，不执行业务默认。"""
    if isinstance(value, datetime):
        return _aware_utc(value, field="calendar change datetime").isoformat()
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, NotificationPolicy):
        return value.value
    if isinstance(value, tuple):
        return [_hashable_change_value(item) for item in value]
    if isinstance(value, list):
        return [_hashable_change_value(item) for item in value]
    if value is None or type(value) in {str, int, float, bool}:
        return cast(JsonValue, value)
    raise TypeError("calendar change contains a non-JSON value")


def _snapshot_instant(value: datetime, *, all_day: bool, timezone: str) -> str:
    """把定时事件规范为 UTC ISO，全天事件保存其事件时区本地日期。"""
    normalized = _aware_utc(value, field="calendar event instant")
    if not all_day:
        return normalized.isoformat()
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("all-day calendar event requires a valid IANA timezone") from error
    return normalized.astimezone(zone).date().isoformat()


def _parse_snapshot_instant(value: str, *, all_day: bool) -> date | datetime:
    """解析 snapshot 时间并验证全天/定时表示严格互斥。"""
    if not isinstance(value, str) or value == "":
        raise ValueError("calendar proposal instant must be a nonempty ISO string")
    if all_day:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("all-day calendar proposal must use ISO dates") from error
        if "T" in value or " " in value:
            raise ValueError("all-day calendar proposal must use pure dates")
        return parsed
    return _parse_timed_instant(value)


def _parse_timed_instant(value: str) -> datetime:
    """解析带时区 ISO datetime 并规范到 UTC。"""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("timed calendar proposal must use ISO datetime") from error
    return _aware_utc(parsed, field="calendar proposal instant")


def _aware_utc(value: datetime, *, field: str) -> datetime:
    """要求带时区 datetime 并转换到 UTC。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _notification_policy(value: object) -> NotificationPolicy:
    """把枚举或精确字符串收窄为通知策略。"""
    if isinstance(value, NotificationPolicy):
        return value
    if isinstance(value, str):
        try:
            return NotificationPolicy(value)
        except ValueError:
            pass
    raise ValueError("calendar notification policy must be all or none")


def _retain_until(clock: Callable[[], datetime], retention_days: int) -> datetime:
    """用显式 UTC 时钟计算敏感 snapshot 保留截止时间。"""
    if type(retention_days) is not int or not 1 <= retention_days <= 3650:
        retention_days = DEFAULT_CALENDAR_CONTENT_RETENTION_DAYS
    return _aware_utc(clock(), field="calendar proposal clock") + timedelta(days=retention_days)


def validate_calendar_creation_idempotency_key(value: str) -> str:
    """验证日历创建幂等键的共享、可重放边界。

    Args:
        value: 由 API、应用用例或恢复 Worker 携带的候选幂等键。

    Returns:
        原样返回已通过校验的键；不做前缀改写或大小写归一化，确保数据库唯一键与
        审批/恢复载荷绑定的字节完全一致。

    Raises:
        ValueError: 候选值不是字符串、为空、带首尾空白、超过 255 个 Unicode 标量，
            或包含任意 Unicode ``Cc`` 控制字符（包括 C0、DEL 与换行）。

    该函数只依赖标准库，故 Worker 可以在解析持久恢复输入时复用同一边界，而不会
    把 SQLAlchemy、供应商 SDK 或网络行为引入 application 层验证。
    """
    if not isinstance(value, str) or value == "" or value != value.strip() or len(value) > 255:
        raise ValueError("calendar proposal idempotency key is invalid")
    if any(unicode_category(character) == "Cc" for character in value):
        raise ValueError("calendar proposal idempotency key is invalid")
    return value


def _to_view(snapshot: CalendarProposalSnapshot) -> CalendarProposalView:
    """把 Repository 快照解析为类型化应用视图。"""
    return CalendarProposalView(
        proposal_id=snapshot.proposal_id,
        connection_id=snapshot.connection_id,
        calendar_id=snapshot.calendar_id,
        operation_kind=snapshot.operation_kind,
        target_event_id=snapshot.target_event_id,
        base_etag=snapshot.base_etag,
        before_snapshot_id=snapshot.before_snapshot_id,
        current_version=snapshot.current_version,
        status=snapshot.status,
        retain_until=_aware_utc(snapshot.retain_until, field="proposal retain_until"),
        content=CalendarProposalContent.model_validate(snapshot.desired_snapshot.content),
    )


def _connection_capability_disabled() -> StateConflictError:
    """构造连接/依赖能力不可用时的稳定错误。"""
    return StateConflictError(
        error_code="connection_capability_disabled",
        message="calendar write capability is not enabled for the selected connection",
    )


def _calendar_availability_persistence_unavailable() -> StateConflictError:
    """构造 suggestion 缺少两短事务持久化端口时的稳定 fail-closed 错误。"""
    return StateConflictError(
        error_code="calendar_availability_persistence_unavailable",
        message="calendar availability persistence is unavailable",
    )


def _calendar_recurring_event_unsupported() -> StateConflictError:
    """构造不回显事件内容的 M2 重复日程拒绝错误。"""
    return StateConflictError(
        error_code="calendar_recurring_event_unsupported",
        message="recurring calendar events are not supported",
    )


def _calendar_event_deleted() -> StateConflictError:
    """构造供应商当前事件已删除或取消的稳定错误。"""
    return StateConflictError(
        error_code="calendar_event_deleted",
        message="calendar event no longer exists",
    )


def _calendar_event_not_editable() -> StateConflictError:
    """构造供应商或目录不再允许修改的稳定错误。"""
    return StateConflictError(
        error_code="calendar_event_not_editable",
        message="calendar event is no longer editable",
    )


__all__ = [
    "CALENDAR_AVAILABILITY_GRID_MINUTES",
    "CALENDAR_AVAILABILITY_HORIZON_DAYS",
    "CALENDAR_AVAILABILITY_LIMIT",
    "DEFAULT_CALENDAR_CONTENT_RETENTION_DAYS",
    "CalendarAvailabilityContext",
    "CalendarAvailabilityPersistence",
    "CalendarAvailabilityRead",
    "CalendarOperationKind",
    "CalendarProposalContent",
    "CalendarProposalEventBinding",
    "CalendarProposalEventSnapshot",
    "CalendarProposalNotFoundError",
    "CalendarProposalRepository",
    "CalendarProposalSnapshot",
    "CalendarProposalSourceReader",
    "CalendarProposalStateSnapshot",
    "CalendarProposalTargetSnapshot",
    "CalendarProposalUseCase",
    "CalendarProposalView",
    "CalendarRestoreEnqueueRepository",
    "CalendarRestoreEnqueueRepositoryFactory",
    "CalendarRestoreEnqueueUseCase",
    "CalendarRestoreSourceProjection",
    "CalendarRestoreSourceSnapshot",
    "CalendarSnapshot",
    "CalendarSnapshotKind",
    "parse_calendar_proposal_conversation_request",
    "validate_calendar_creation_idempotency_key",
]
