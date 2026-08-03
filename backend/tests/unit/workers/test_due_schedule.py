"""验证每日简报调度在 IANA 时区与夏令时边界上的确定性。"""

from datetime import UTC, date, datetime, time
from uuid import UUID, uuid4

import pytest
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListRedisScheduleSource

from ai_employee.application.use_cases.maintenance import ExpireSessionsUseCase
from ai_employee.application.use_cases.schedules import (
    ActiveUserBriefSchedule,
    DispatchDueDailyBriefsUseCase,
)
from ai_employee.domain.tasks import JsonValue
from ai_employee.infrastructure.queue.broker import broker, retry_schedule_source
from ai_employee.infrastructure.queue.scheduler import scheduler
from ai_employee.workers.schedules import (
    daily_brief_idempotency_key,
    dispatch_due_briefs,
    expire_sessions,
    is_daily_brief_due,
    recover_task_retries,
    relay_outbox,
    scheduled_daily_brief_instant,
)


class RecordingScheduleReader:
    """返回预置活动用户计划，模拟 PostgreSQL reader 的应用端口。"""

    def __init__(self, schedules: tuple[ActiveUserBriefSchedule, ...]) -> None:
        self.schedules = schedules

    async def list_active(self) -> tuple[ActiveUserBriefSchedule, ...]:
        """返回测试计划快照。"""
        return self.schedules


class RecordingTaskCreator:
    """记录到期扫描生成的内部任务输入与幂等键。"""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str, dict[str, JsonValue], str]] = []

    async def execute(
        self,
        *,
        user_id: UUID,
        kind: str,
        input_payload: dict[str, JsonValue],
        idempotency_key: str,
    ) -> object:
        """记录创建意图；返回值不参与调度决策。"""
        self.calls.append((user_id, kind, input_payload, idempotency_key))
        return object()


class RecordingSessionMaintenanceRepository:
    """记录应用用例传入的到期边界，并返回删除数量。"""

    def __init__(self) -> None:
        self.now: datetime | None = None

    async def delete_expired(self, *, now: datetime) -> int:
        """保存规范 UTC 瞬间并返回合成删除数。"""
        self.now = now
        return 2


class RecordingSessionMaintenanceFactory:
    """提供最小异步事务上下文，验证 Worker 不直接承载删除规则。"""

    def __init__(self, repository: RecordingSessionMaintenanceRepository) -> None:
        self.repository = repository

    def __call__(self) -> "RecordingSessionMaintenanceFactory":
        """返回自身作为异步上下文。"""
        return self

    async def __aenter__(self) -> RecordingSessionMaintenanceRepository:
        """进入合成事务。"""
        return self.repository

    async def __aexit__(self, *args: object) -> None:
        """退出合成事务；测试不模拟提交异常。"""
        del args


def test_fall_back_ambiguous_minute_uses_first_instant_and_one_key() -> None:
    """重复小时选择第一个真实瞬间，而两个 fold 仍共享同一本地日期幂等键。"""
    user_id = uuid4()
    local_date = date(2026, 11, 1)

    scheduled = scheduled_daily_brief_instant(
        local_date=local_date,
        brief_time=time(1, 30),
        timezone_name="America/New_York",
    )

    assert scheduled == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert daily_brief_idempotency_key(user_id=user_id, local_date=local_date) == (
        f"daily_brief:{user_id}:2026-11-01:scheduled"
    )


def test_spring_forward_missing_minute_runs_at_first_valid_minute_after_gap() -> None:
    """不存在的 02:30 不做 naive 相等比较，而是在时钟跳到 03:00 时到期。"""
    scheduled = scheduled_daily_brief_instant(
        local_date=date(2026, 3, 8),
        brief_time=time(2, 30),
        timezone_name="America/New_York",
    )

    assert scheduled == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert (
        is_daily_brief_due(
            now=datetime(2026, 3, 8, 6, 59, tzinfo=UTC),
            local_date=date(2026, 3, 8),
            brief_time=time(2, 30),
            timezone_name="America/New_York",
        )
        is False
    )
    assert (
        is_daily_brief_due(
            now=datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
            local_date=date(2026, 3, 8),
            brief_time=time(2, 30),
            timezone_name="America/New_York",
        )
        is True
    )


def test_due_comparison_rejects_naive_now() -> None:
    """调度边界必须接收 timezone-aware instant，禁止宿主机时区参与业务日期。"""
    try:
        is_daily_brief_due(
            now=datetime(2026, 8, 1, 8, 0, tzinfo=UTC).replace(tzinfo=None),
            local_date=date(2026, 8, 1),
            brief_time=time(8, 0),
            timezone_name="Asia/Shanghai",
        )
    except ValueError as error:
        assert str(error) == "now must be timezone-aware"
    else:
        raise AssertionError("naive datetime must be rejected")


def test_fixed_jobs_are_registered_with_stable_schedule_ids() -> None:
    """固定调度同时覆盖 Outbox、重试恢复、到期简报与会话清理。"""
    assert relay_outbox.labels["schedule"] == [{"cron": "* * * * *", "schedule_id": "outbox-relay"}]
    assert recover_task_retries.labels["schedule"] == [
        {"cron": "* * * * *", "schedule_id": "recover-task-retries"}
    ]
    assert dispatch_due_briefs.labels["schedule"] == [
        {"cron": "* * * * *", "schedule_id": "due-daily-briefs"}
    ]
    assert expire_sessions.labels["schedule"] == [
        {"cron": "0 * * * *", "schedule_id": "expire-sessions"}
    ]
    assert scheduler.broker is broker
    assert len(scheduler.sources) == 2
    assert scheduler.sources[0] is retry_schedule_source
    assert isinstance(scheduler.sources[0], ListRedisScheduleSource)
    assert isinstance(scheduler.sources[1], LabelScheduleSource)


@pytest.mark.asyncio
async def test_due_scan_uses_each_users_local_date_and_skips_not_yet_due() -> None:
    """同一 UTC 瞬间按各自 IANA 时区求日期，只为已到期活动计划调用创建用例。"""
    due_user_id = uuid4()
    future_user_id = uuid4()
    reader = RecordingScheduleReader(
        (
            ActiveUserBriefSchedule(
                user_id=due_user_id,
                timezone="Asia/Shanghai",
                brief_time=time(8, 0),
            ),
            ActiveUserBriefSchedule(
                user_id=future_user_id,
                timezone="America/Los_Angeles",
                brief_time=time(18, 0),
            ),
        )
    )
    creator = RecordingTaskCreator()
    use_case = DispatchDueDailyBriefsUseCase(reader=reader, task_creator=creator)

    created_count = await use_case.execute(now=datetime(2026, 8, 1, 0, 0, tzinfo=UTC))

    assert created_count == 1
    assert creator.calls == [
        (
            due_user_id,
            "daily_brief",
            {"local_date": "2026-08-01", "schedule_kind": "scheduled"},
            f"daily_brief:{due_user_id}:2026-08-01:scheduled",
        )
    ]


@pytest.mark.asyncio
async def test_expire_sessions_use_case_owns_utc_boundary_and_repository_transaction() -> None:
    """固定 job 调应用用例，由应用层验证 UTC 并让 repository 执行真实删除。"""
    repository = RecordingSessionMaintenanceRepository()
    use_case = ExpireSessionsUseCase(RecordingSessionMaintenanceFactory(repository))
    now = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)

    deleted = await use_case.execute(now=now)

    assert deleted == 2
    assert repository.now == now


@pytest.mark.asyncio
async def test_expire_sessions_rejects_naive_time() -> None:
    """会话清理不能把宿主机本地时间解释成过期边界。"""
    repository = RecordingSessionMaintenanceRepository()
    use_case = ExpireSessionsUseCase(RecordingSessionMaintenanceFactory(repository))

    with pytest.raises(ValueError, match="timezone-aware"):
        await use_case.execute(now=datetime(2026, 8, 1, 0, 0, tzinfo=UTC).replace(tzinfo=None))

    assert repository.now is None
