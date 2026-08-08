"""验证测试模式 Google 适配器只读取合成 fixture。"""

from pathlib import Path

import pytest

from ai_employee.integrations.google.fake import (
    FakeCalendarReader,
    FakeGmailReader,
    FakeGoogleOAuthClient,
)


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
