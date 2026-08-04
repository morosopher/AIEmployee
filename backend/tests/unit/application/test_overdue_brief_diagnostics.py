"""验证逾期简报诊断的纯应用层时间、隔离与幂等规则。"""

from datetime import UTC, date, datetime, time
from uuid import UUID, uuid4

import pytest

from ai_employee.application.use_cases.diagnostics import (
    ActiveUserOverdueBriefSchedule,
    DispatchOverdueBriefDiagnosticsUseCase,
    daily_brief_overdue_alert,
)
from ai_employee.domain.tasks import JsonValue


class _Reader:
    """以合成简报完整度快照实现诊断扫描读取端口。"""

    def __init__(self, schedules: tuple[ActiveUserOverdueBriefSchedule, ...]) -> None:
        """保存活动计划与每个用户日期的完整度结果。"""
        self.schedules = schedules
        self.completeness: dict[tuple[UUID, date], tuple[str, ...]] = {}

    async def list_active(self) -> tuple[ActiveUserOverdueBriefSchedule, ...]:
        """返回活动用户的最小计划快照。"""
        return self.schedules

    async def list_brief_completeness(
        self, *, user_id: UUID, local_date: date
    ) -> tuple[str, ...]:
        """返回指定用户日期的简报完整度，模拟强制用户隔离查询。"""
        return self.completeness.get((user_id, local_date), ())


class _Creator:
    """记录诊断任务创建调用，不连接队列或数据库。"""

    def __init__(self) -> None:
        """初始化空调用记录。"""
        self.calls: list[tuple[UUID, str, dict[str, JsonValue], str]] = []

    async def execute(
        self,
        *,
        user_id: UUID,
        kind: str,
        input_payload: dict[str, JsonValue],
        idempotency_key: str,
    ) -> object:
        """记录创建意图并返回不透明成功标记。"""
        self.calls.append((user_id, kind, input_payload, idempotency_key))
        return object()


@pytest.mark.asyncio
async def test_scan_creates_only_overdue_users_and_failed_brief_does_not_suppress() -> None:
    """超过宽限期且仅有 failed 简报时必须创建一次诊断，complete/partial 均抑制。"""
    overdue_user_id, complete_user_id, partial_user_id = uuid4(), uuid4(), uuid4()
    local_date = date(2026, 8, 1)
    reader = _Reader(
        tuple(
            ActiveUserOverdueBriefSchedule(user_id=user_id, timezone="Asia/Shanghai", brief_time=time(8, 0))
            for user_id in (overdue_user_id, complete_user_id, partial_user_id)
        )
    )
    reader.completeness[(overdue_user_id, local_date)] = ("failed",)
    reader.completeness[(complete_user_id, local_date)] = ("complete",)
    reader.completeness[(partial_user_id, local_date)] = ("partial",)
    creator = _Creator()

    created = await DispatchOverdueBriefDiagnosticsUseCase(reader=reader, task_creator=creator).execute(
        now=datetime(2026, 8, 1, 0, 16, tzinfo=UTC)
    )

    assert created == 1
    assert creator.calls == [
        (
            overdue_user_id,
            "brief.overdue_diagnostic",
            {"local_date": "2026-08-01"},
            f"diagnostic:daily-brief:{overdue_user_id}:2026-08-01",
        )
    ]


@pytest.mark.asyncio
async def test_scan_uses_dst_safe_scheduled_instant_and_waits_for_full_grace_period() -> None:
    """春令时跳过的 02:30 必须在实际 03:15 后才被判断逾期，不能按不存在墙上时间误报。"""
    user_id = uuid4()
    reader = _Reader(
        (ActiveUserOverdueBriefSchedule(user_id=user_id, timezone="America/New_York", brief_time=time(2, 30)),)
    )
    creator = _Creator()
    use_case = DispatchOverdueBriefDiagnosticsUseCase(reader=reader, task_creator=creator)

    assert await use_case.execute(now=datetime(2026, 3, 8, 7, 14, tzinfo=UTC)) == 0
    assert await use_case.execute(now=datetime(2026, 3, 8, 7, 15, tzinfo=UTC)) == 1


def test_derived_alert_exposes_diagnostic_task_without_treating_failed_brief_as_success() -> None:
    """告警视图必须使用稳定代码和任务 UUID，且 failed 简报仍保持 critical 状态。"""
    task_id = uuid4()

    alert = daily_brief_overdue_alert(
        now=datetime(2026, 8, 1, 0, 16, tzinfo=UTC),
        schedule=ActiveUserOverdueBriefSchedule(
            user_id=uuid4(), timezone="Asia/Shanghai", brief_time=time(8, 0)
        ),
        completeness=("failed",),
        diagnostic_task_id=task_id,
    )

    assert alert is not None
    assert alert.code == "daily_brief_overdue"
    assert alert.severity == "critical"
    assert alert.diagnostic_task_id == task_id
