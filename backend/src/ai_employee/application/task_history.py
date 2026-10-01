"""只读任务历史的不可变共享值类型、用途分类及规范筛选，不触碰执行状态机。"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ai_employee.application.task_history_dates import canonical_utc_timestamp, local_day_start
from ai_employee.domain.tasks import TaskStatus

HistoryScope = Literal["business", "all", "background"]
HistoryCategory = Literal["business", "background", "other"]
HistoryDirection = Literal["older", "newer"]

# 映射只描述用途，不能从触发方式或敏感载荷推断分类；后台查询还包含未知种类。
BUSINESS_KINDS: Final = frozenset(
    {
        "daily_brief",
        "conversation.respond",
        "mail_draft.generate",
        "trusted_action",
        "calendar.restore.prepare",
    }
)
BACKGROUND_KINDS: Final = frozenset(
    {
        "sync_mail",
        "sync_gmail",
        "sync_calendar",
        "brief.overdue_diagnostic",
        "privacy.clear_source_cache",
        "privacy.delete_all_data",
    }
)


class TaskHistoryFilterError(ValueError):
    """无载荷过滤错误；API 可安全映射固定 422，不回显任何原始参数。"""

    def __init__(self) -> None:
        """仅接受无参构造，防止调用方把用户输入拼入错误。"""
        super().__init__("task_history_filter_invalid")


class TaskHistoryCursorError(ValueError):
    """无载荷游标错误；认证仍由调用方执行，不以游标作为用户身份。"""

    def __init__(self) -> None:
        """仅提供固定错误文本，禁止把游标或认证材料传入消息。"""
        super().__init__("task_history_cursor_invalid")


@dataclass(frozen=True, slots=True, order=True)
class HistoryKey:
    """分页排序键：先比较完整 created_at 微秒，再比较 task_id UUID。"""

    created_at: datetime
    task_id: UUID


@dataclass(frozen=True, slots=True)
class TaskHistoryQuery:
    """原始只读查询输入；日期为民用日，cursor 独立交给专用编解码器验证。"""

    scope: str = "business"
    kind: str | None = None
    status: str | None = None
    created_from_date: str | None = None
    created_to_date: str | None = None
    limit: int = 20
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class TaskHistoryFilters:
    """已规范化查询：显式用户隔离、UTC 半开时间范围、IANA 时区及分页大小。

    kind 为 other 时表示不在两个已知集合中的类型；background 范围同样包含
    other。时间为 None 表示不限该侧；相等边界表示整日跳过形成的合法空区间。
    """

    user_id: UUID
    scope: HistoryScope
    kind: str | None
    status: TaskStatus | None
    created_from: datetime | None
    created_before: datetime | None
    timezone: str
    limit: int


@dataclass(frozen=True, slots=True)
class TaskHistoryItem:
    """任务安全摘要：仅含身份、用途、状态、执行时间、错误码及仍存在的重试来源。

    可空字段原样保留，不包含正文、步骤、事件游标或执行授权；详情另行读取。
    """

    id: UUID
    kind: str
    category: HistoryCategory
    status: TaskStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    retry_of_task_id: UUID | None


@dataclass(frozen=True, slots=True)
class HistoryCursorState:
    """签名游标元数据：绑定用户、筛选哈希、方向、共同上界、seek 锚点和签发时间。"""

    user_id: UUID
    filters_hash: str
    direction: HistoryDirection
    upper: HistoryKey
    anchor: HistoryKey
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class HistoryReadPage:
    """同一短只读快照的查询结果；锚点缺失表示对应方向不可继续翻页。

    upper 为第一页确立的创建顺序上界；后台失败计数包含非业务 failed，
    仅受用户与创建时间范围约束，不受当前类型或状态筛选约束。
    """

    items: tuple[TaskHistoryItem, ...]
    upper: HistoryKey | None
    next_anchor: HistoryKey | None
    previous_anchor: HistoryKey | None
    background_failed_count: int


@dataclass(frozen=True, slots=True)
class TaskHistoryPage:
    """返回调用方的只读分页值：摘要、双向游标、UTC 读取时间及实际筛选时区。"""

    items: tuple[TaskHistoryItem, ...]
    next_cursor: str | None
    previous_cursor: str | None
    server_time: datetime
    filter_timezone: str
    background_failed_count: int


def history_category(kind: str) -> HistoryCategory:
    """按固定用途白名单分类，不读取载荷或推断来源。

    Args:
        kind: 持久化任务类型，包括旧类型和未知类型。

    Returns:
        business、background 或兜底 other；未知种类仍可在全部历史中读取。
    """
    if kind in BUSINESS_KINDS:
        return "business"
    if kind in BACKGROUND_KINDS:
        return "background"
    return "other"


def _parse_date(value: str | None) -> date | None:
    """解析精确 YYYY-MM-DD；拒绝 ISO 周日期和宽松紧凑形式。"""
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise TaskHistoryFilterError()
    return date.fromisoformat(value)


def normalize_history_filters(
    *,
    user_id: UUID,
    timezone: str,
    query: TaskHistoryQuery,
) -> TaskHistoryFilters:
    """验证组合并将用户民用日期变为 UTC 半开范围。

    Args:
        user_id: 调用方认证得到的用户 ID，不来自游标。
        timezone: 当前用户配置的显式 IANA 时区。
        query: 原始列表筛选；cursor 不参与本步骤或筛选哈希。

    Returns:
        不可变的类型化筛选；默认无日期限制，每页 20 条。

    Raises:
        TaskHistoryFilterError: 非法枚举、互斥组合、日期或转换溢出，消息无载荷。
    """
    try:
        ZoneInfo(timezone)
        if query.scope not in ("business", "all", "background"):
            raise TaskHistoryFilterError()
        if type(query.limit) is not int or not 1 <= query.limit <= 100:
            raise TaskHistoryFilterError()
        if query.kind is not None:
            if query.kind not in BUSINESS_KINDS | BACKGROUND_KINDS | {"other"}:
                raise TaskHistoryFilterError()
            business = query.kind in BUSINESS_KINDS
            if (query.scope == "business" and not business) or (
                query.scope == "background" and business
            ):
                raise TaskHistoryFilterError()
        status = TaskStatus(query.status) if query.status is not None else None
        start, end = _parse_date(query.created_from_date), _parse_date(query.created_to_date)
        if start is not None and end is not None and start > end:
            raise TaskHistoryFilterError()
        return TaskHistoryFilters(
            user_id=user_id,
            scope=cast(HistoryScope, query.scope),
            kind=query.kind,
            status=status,
            created_from=local_day_start(start, timezone) if start else None,
            created_before=local_day_start(end + timedelta(days=1), timezone) if end else None,
            timezone=timezone,
            limit=query.limit,
        )
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise TaskHistoryFilterError() from exc


def history_filters_hash(filters: TaskHistoryFilters) -> str:
    """计算绑定全部规范筛选字段的 canonical JSON SHA-256。

    Args:
        filters: 已通过规范化的不可变筛选值，不含游标。

    Returns:
        小写 SHA-256 十六进制摘要；字段排序且无空白，时间使用六位微秒 UTC。

    Raises:
        ValueError: 调用方绕过规范化提供 naive 或超界时间。
    """
    payload = {
        "user_id": str(filters.user_id),
        "scope": filters.scope,
        "kind": filters.kind,
        "status": filters.status.value if filters.status is not None else None,
        "created_from": canonical_utc_timestamp(filters.created_from)
        if filters.created_from
        else None,
        "created_before": canonical_utc_timestamp(filters.created_before)
        if filters.created_before
        else None,
        "timezone": filters.timezone,
        "limit": filters.limit,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
