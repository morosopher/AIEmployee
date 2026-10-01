"""验证历史查询纯规则，不依赖执行来源、持久化或敏感任务载荷。"""

from dataclasses import fields, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from ai_employee.application.task_history import (
    HistoryKey,
    TaskHistoryCursorError,
    TaskHistoryFilterError,
    TaskHistoryItem,
    TaskHistoryQuery,
    history_category,
    history_filters_hash,
    normalize_history_filters,
)
from ai_employee.domain.tasks import TaskStatus

USER = UUID("00000000-0000-0000-0000-000000000001")
BUSINESS = (
    "daily_brief",
    "conversation.respond",
    "mail_draft.generate",
    "trusted_action",
    "calendar.restore.prepare",
)
BACKGROUND = (
    "sync_mail",
    "sync_gmail",
    "sync_calendar",
    "brief.overdue_diagnostic",
    "privacy.clear_source_cache",
    "privacy.delete_all_data",
)


@pytest.mark.parametrize("kind", BUSINESS + BACKGROUND + ("legacy.unknown",))
def test_classification_and_known_filter(kind: str) -> None:
    """用途映射保留全部已知类别，未知记录只通过 other 哨兵筛选。"""
    expected = "business" if kind in BUSINESS else "background" if kind in BACKGROUND else "other"
    assert history_category(kind) == expected
    selected = kind if expected != "other" else "other"
    result = normalize_history_filters(
        user_id=USER, timezone="UTC", query=TaskHistoryQuery(scope="all", kind=selected)
    )
    assert result.kind == selected


@pytest.mark.parametrize("status", list(TaskStatus))
def test_all_statuses_remain_distinct(status: TaskStatus) -> None:
    """十个状态均原样保留，不把人工关注或核对归并为失败。"""
    result = normalize_history_filters(
        user_id=USER, timezone="UTC", query=TaskHistoryQuery(status=status.value)
    )
    assert result.status is status


@pytest.mark.parametrize(
    "query",
    [
        TaskHistoryQuery(limit=0),
        TaskHistoryQuery(limit=101),
        TaskHistoryQuery(limit=True),
        TaskHistoryQuery(scope="unknown"),
        TaskHistoryQuery(status="unknown"),
        TaskHistoryQuery(kind="legacy.unknown"),
        TaskHistoryQuery(kind="other"),
        TaskHistoryQuery(kind="sync_mail"),
        TaskHistoryQuery(scope="background", kind="daily_brief"),
        TaskHistoryQuery(created_from_date="2026-02-29"),
        TaskHistoryQuery(created_from_date="2026-1-01"),
        TaskHistoryQuery(created_from_date="20260101"),
        TaskHistoryQuery(created_from_date="2026-01-02", created_to_date="2026-01-01"),
        TaskHistoryQuery(created_to_date="9999-12-31"),
    ],
)
def test_invalid_filters_have_fixed_safe_error(query: TaskHistoryQuery) -> None:
    """非法组合及日期统一拒绝，错误不包含未验证输入。"""
    with pytest.raises(TaskHistoryFilterError, match="^task_history_filter_invalid$"):
        normalize_history_filters(user_id=USER, timezone="UTC", query=query)


@pytest.mark.parametrize("timezone", ["not/a-zone", "", "/etc/passwd", "../UTC"])
def test_timezone_is_validated_without_date_filter(timezone: str) -> None:
    """未筛日期也必须绑定合法用户时区，不使用宿主时区兜底。"""
    with pytest.raises(TaskHistoryFilterError, match="^task_history_filter_invalid$"):
        normalize_history_filters(user_id=USER, timezone=timezone, query=TaskHistoryQuery())


def test_default_filters_and_hash_binding() -> None:
    """哈希绑定每个规范字段且排除游标，日期精度不得丢失。"""
    base = normalize_history_filters(user_id=USER, timezone="UTC", query=TaskHistoryQuery())
    assert (base.scope, base.limit, base.created_from, base.created_before) == (
        "business",
        20,
        None,
        None,
    )
    same = normalize_history_filters(
        user_id=USER, timezone="UTC", query=TaskHistoryQuery(cursor="opaque")
    )
    assert history_filters_hash(base) == history_filters_hash(same)
    variants = [
        replace(base, user_id=UUID(int=2)),
        replace(base, scope="all"),
        replace(base, kind="daily_brief"),
        replace(base, status=TaskStatus.FAILED),
        replace(base, created_from=datetime(2026, 1, 1, 0, 0, 0, 1, UTC)),
        replace(base, created_before=datetime(2026, 1, 2, tzinfo=UTC)),
        replace(base, timezone="Asia/Shanghai"),
        replace(base, limit=100),
    ]
    assert len({history_filters_hash(base), *(history_filters_hash(v) for v in variants)}) == 9
    assert len(history_filters_hash(base)) == 64


@pytest.mark.parametrize(
    "scope,kind",
    [
        ("background", "other"),
        ("background", "sync_gmail"),
        ("business", "daily_brief"),
        ("all", "other"),
    ],
)
def test_valid_scope_combinations(scope: str, kind: str) -> None:
    """后台范围包含未知种类，合法边界页大小可用。"""
    for limit in (1, 100):
        result = normalize_history_filters(
            user_id=USER,
            timezone="UTC",
            query=TaskHistoryQuery(scope=scope, kind=kind, limit=limit),
        )
        assert result.limit == limit


def test_summary_whitelist_and_full_precision_order() -> None:
    """摘要只暴露安全字段，分页先比较微秒再比较 UUID。"""
    assert {f.name for f in fields(TaskHistoryItem)} == {
        "id",
        "kind",
        "category",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "error_code",
        "retry_of_task_id",
    }
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    assert HistoryKey(instant, UUID(int=1)) < HistoryKey(instant, UUID(int=2))
    assert HistoryKey(instant, UUID(int=2)) < HistoryKey(
        instant.replace(microsecond=1), UUID(int=1)
    )
    assert str(TaskHistoryCursorError()) == "task_history_cursor_invalid"
