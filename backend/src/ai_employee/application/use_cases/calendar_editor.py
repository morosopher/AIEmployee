"""读取日程编辑器的原 before 和本人冲突事实，在数据库短读完成后运行纯算法。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ai_employee.application.use_cases.action_views import (
    CalendarConflictPreview,
    CalendarPreviewFields,
    calendar_conflict_previews,
)
from ai_employee.application.use_cases.calendar_proposals import (
    CalendarAvailabilityContext,
    CalendarProposalContent,
    CalendarProposalNotFoundError,
    CalendarProposalView,
    CalendarRestoreSourceProjection,
    validate_calendar_restore_source_projection,
)
from ai_employee.domain.errors import StateConflictError

type BeforeStatus = Literal["not_applicable", "available", "unavailable"]


class CalendarRestoreSource(BaseModel):
    """本人已应用修改的精确恢复入口；只是读取事实，入队仍重新验证全部资格。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: UUID
    snapshot_id: UUID


class CalendarEditorFacts(BaseModel):
    """编辑 GET 的类型化事实；incomplete 绝不能显示为已检查且没有冲突。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    before: CalendarPreviewFields | None
    before_status: BeforeStatus
    conflict_status: Literal["incomplete", "checked"]
    conflicts: list[CalendarConflictPreview] | None
    restore_source: CalendarRestoreSource | None = None


@dataclass(frozen=True, slots=True)
class CalendarEditorRead:
    """短读事务冻结的最小 DTO，不把 ORM、供应商响应或游标带出事务。"""

    proposal: CalendarProposalView
    before: CalendarPreviewFields | None
    before_status: BeforeStatus
    context: CalendarAvailabilityContext | None
    restore_source_projection: CalendarRestoreSourceProjection | None = None


class CalendarEditorPersistence(Protocol):
    """复用日历 availability adapter 的只读边界，返回前关闭数据库事务。"""

    async def load_editor(
        self, *, user_id: UUID, proposal_id: UUID, observed_at: datetime
    ) -> CalendarEditorRead | None:
        """读取本人提案、原 before 与覆盖完整区间的有界可用性事实。"""
        ...


def editor_calendar_fields(content: CalendarProposalContent) -> CalendarPreviewFields | None:
    """把完整且已校验的内容映射为展示字段；shell 缺失任何关键字段时返回空。"""
    if (
        content.title is None
        or not content.title.strip()
        or content.starts_at is None
        or content.ends_at is None
        or content.timezone is None
        or content.all_day is None
    ):
        return None
    return CalendarPreviewFields(
        title=content.title,
        description=content.description,
        location=content.location,
        starts_at=content.starts_at,
        ends_at=content.ends_at,
        timezone=content.timezone,
        all_day=content.all_day,
        attendees=list(content.attendees),
    )


class CalendarEditorUseCase:
    """分离短读与纯冲突计算，只返回当前版本对应事实，不产生任何写操作。"""

    def __init__(
        self, persistence: CalendarEditorPersistence, clock: Callable[[], datetime]
    ) -> None:
        """注入既有只读持久化和明确时钟，测试无需依赖真实当前时间。"""
        self._persistence = persistence
        self._clock = clock

    async def get(
        self, *, user_id: UUID, proposal_id: UUID
    ) -> tuple[CalendarProposalView, CalendarEditorFacts]:
        """读取已认证用户的编辑视图；原 before 清除与尚未检查冲突分别表达。"""
        observed_at = self._clock()
        loaded = await self._persistence.load_editor(
            user_id=user_id, proposal_id=proposal_id, observed_at=observed_at
        )
        if loaded is None:
            raise CalendarProposalNotFoundError
        fields = editor_calendar_fields(loaded.proposal.content)
        checked = fields is not None and loaded.context is not None
        conflicts = (
            calendar_conflict_previews(fields, loaded.context)
            if fields is not None and loaded.context is not None
            else None
        )
        return loaded.proposal, CalendarEditorFacts(
            before=loaded.before,
            before_status=loaded.before_status,
            conflict_status="checked" if checked else "incomplete",
            conflicts=conflicts,
            restore_source=_restore_source(loaded, observed_at=observed_at),
        )


def _restore_source(
    loaded: CalendarEditorRead, *, observed_at: datetime
) -> CalendarRestoreSource | None:
    """复用入队的来源判断；原before无法认证或读取已过期时不提供猜测入口。

    Args:
        loaded: 单个用户短事务返回的原提案、快照及精确事件绑定。
        observed_at: 与本次读取相同的显式UTC时钟。

    Returns:
        可用于显式准备请求的两项UUID；缺失或不再合格时返回空。
    """
    projection = loaded.restore_source_projection
    if (
        loaded.before_status != "available"
        or projection is None
        or projection.target_local_event_id is None
        or projection.source_proposal_id != loaded.proposal.proposal_id
        or projection.source_snapshot_id != loaded.proposal.before_snapshot_id
    ):
        return None
    try:
        validate_calendar_restore_source_projection(
            projection, event_id=projection.target_local_event_id, now=observed_at
        )
    except StateConflictError:
        return None
    return CalendarRestoreSource(
        event_id=projection.target_local_event_id, snapshot_id=projection.source_snapshot_id
    )
