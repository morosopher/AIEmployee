"""验证 Google 同步调度入口的固定十分钟 schedule 标识。"""

from ai_employee.workers import schedules


def test_google_incremental_sync_schedule_is_registered() -> None:
    """调度器公开日历/Gmail 增量扫描入口，避免只靠进程内定时器。"""
    assert hasattr(schedules, "dispatch_google_incremental_syncs")
