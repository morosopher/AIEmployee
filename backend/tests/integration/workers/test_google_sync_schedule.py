"""验证十分钟 provider-neutral 同步调度的能力、scope 与幂等任务。"""

from dataclasses import dataclass
from uuid import UUID

import pytest

from ai_employee.workers import schedules


@dataclass(frozen=True, slots=True)
class _Scope:
    """模拟 Repository 已按 connected + enabled read capability 过滤的同步 scope。"""

    user_id: UUID
    connection_id: UUID
    provider: str
    resource_kind: str
    scope_key: str


class _Reader:
    """返回过渡期邮箱与两个日历 scope，验证 Scheduler 的目录 owner 防线。"""

    async def enabled_scopes(self) -> tuple[_Scope, ...]:
        """仅返回具备 enabled 读能力且已有持久恢复 scope 的事实。"""
        user_id = UUID("00000000-0000-0000-0000-000000000001")
        connection_id = UUID("00000000-0000-0000-0000-000000000002")
        return (
            _Scope(user_id, connection_id, "google", "mail", "mailbox"),
            _Scope(user_id, connection_id, "google", "calendar", "primary"),
            _Scope(user_id, connection_id, "google", "calendar", "team-calendar"),
        )


@pytest.mark.asyncio
async def test_provider_scheduler_uses_one_google_calendar_directory_owner(monkeypatch) -> None:
    """Google Calendar 普通周期必须把多个事件 scope 聚合为一个 directory owner。"""
    created: list[dict[str, object]] = []

    class _Creator:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def execute(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(
        schedules,
        "SqlAlchemyEnabledSyncScopeReader",
        lambda _factory: _Reader(),
    )
    monkeypatch.setattr(schedules, "CreateTaskUseCase", _Creator)
    monkeypatch.setattr(schedules, "_build_outbox_relay", lambda: object())
    await schedules.dispatch_google_incremental_syncs()
    assert [item["kind"] for item in created] == [
        "sync_mail",
        "sync_calendar",
    ]
    assert [item["input_payload"]["scope_key"] for item in created] == [
        "mailbox",
        "directory",
    ]
    assert all(
        str(item["idempotency_key"]).startswith("sync:google:00000000-0000-0000-0000-000000000002:")
        for item in created
    )
    idempotency_keys = [str(item["idempotency_key"]) for item in created]
    assert len(set(idempotency_keys)) == 2
    # opaque folder/calendar ID 可能含帐号标识且最长 512；键只保存稳定摘要，避免超长或泄露。
    assert all(
        scope not in key
        for scope in ("mailbox", "directory", "primary", "team-calendar")
        for key in idempotency_keys
    )


def test_google_incremental_schedule_has_ten_minute_label() -> None:
    """Taskiq 注册固定 schedule_id 和十分钟 cron，避免进程内无持久化定时器。"""
    assert hasattr(schedules, "dispatch_google_incremental_syncs")
