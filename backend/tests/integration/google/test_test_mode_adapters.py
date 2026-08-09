"""验证测试模式 Google 适配器只读取合成 fixture。"""

from pathlib import Path

import httpx
import pytest

from ai_employee.config import Settings
from ai_employee.integrations.google.fake import (
    FakeCalendarReader,
    FakeGmailReader,
    FakeGoogleOAuthClient,
)
from ai_employee.workers import sync_calendar


@pytest.mark.asyncio
async def test_fake_calendar_and_oauth_are_predictable_and_offline() -> None:
    """fake 连接和日历页都返回固定脱敏数据，不依赖任何外网。"""
    fixture = Path(__file__).parents[2] / "contract" / "fixtures" / "calendar_initial.json"
    oauth = FakeGoogleOAuthClient()
    account = await oauth.fetch_account("ignored")
    pages = [page async for page in FakeCalendarReader(fixture).initial_pages()]
    gmail_fixture = Path(__file__).parents[2] / "contract" / "fixtures" / "gmail_initial.json"
    gmail_pages = [page async for page in FakeGmailReader(gmail_fixture).initial_pages()]
    assert account.email == "test-mode@example.test"
    assert pages[0].events[0].event_id == "synthetic-event-1"
    assert gmail_pages[0].latest_history_id


@pytest.mark.asyncio
async def test_fake_calendar_fixture_is_not_cloned_across_directory_calendars() -> None:
    """事件 fixture 只代表 primary collection，不能改写 calendar ID 后复制到次要日历。"""
    fixture_root = Path(__file__).parents[2] / "contract" / "fixtures"
    reader = FakeCalendarReader(
        fixture_root / "calendar_initial.json",
        directory_fixture=fixture_root / "google_calendar_list.json",
    )

    primary = [event async for page in reader.initial_pages("primary") for event in page.events]
    secondary = [
        event
        async for page in reader.initial_pages("readonly@example.test")
        for event in page.events
    ]

    assert primary
    assert secondary == []
    assert (
        await reader.get_current_event(
            "readonly@example.test",
            primary[0].event_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_app_test_mode_microsoft_calendar_is_offline_and_not_cloned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """组合根必须注入离线 Microsoft reader，且 fixture 只能归属 primary 日历。"""
    master_key = tmp_path / "master-key"
    master_key.write_text(
        "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
        encoding="utf-8",
    )

    class NoHttpClient:
        """测试模式若尝试构造任何真实 HTTP client，立即暴露越界。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("APP_TEST_MODE calendar sync must remain offline")

    monkeypatch.setattr(httpx, "AsyncClient", NoHttpClient)
    step = sync_calendar.build_calendar_sync_task_step(
        session_factory=object(),  # type: ignore[arg-type]
        settings=Settings(
            app_env="test",
            app_test_mode=True,
            app_master_key_file=master_key,
        ),
    )
    reader = step._microsoft_reader
    assert isinstance(reader, sync_calendar._FakeMicrosoftCalendarReader)

    directory = [page async for page in reader.directory_pages()]
    primary_id = next(
        calendar.calendar_id for calendar in directory[0].calendars if calendar.is_primary
    )
    secondary_id = next(
        calendar.calendar_id for calendar in directory[0].calendars if not calendar.is_primary
    )
    primary_events = [
        event async for page in reader.initial_pages(primary_id) for event in page.events
    ]
    secondary_events = [
        event async for page in reader.initial_pages(secondary_id) for event in page.events
    ]

    assert primary_events
    assert secondary_events == []


@pytest.mark.asyncio
async def test_fake_microsoft_exact_get_matches_calendar_and_event_identity() -> None:
    """相同 event ID 不能从错误 calendar scope 读取并重新标记归属。"""
    fixture_root = Path(__file__).parents[2] / "contract" / "microsoft" / "fixtures"
    reader = sync_calendar._FakeMicrosoftCalendarReader(
        fixture_root / "calendars.json",
        fixture_root / "calendar_view_delta_initial.json",
    )
    primary_events = [
        event async for page in reader.initial_pages("calendar-primary") for event in page.events
    ]
    assert primary_events

    assert (
        await reader.get_current_event(
            "calendar-readonly",
            primary_events[0].event_id,
        )
        is None
    )
