"""验证普通同步、preflight与marked恢复共享分页收集器的供应商访问上界。"""

import pytest

from ai_employee.application.ports.calendar import CalendarSyncPage
from ai_employee.application.use_cases.sync_calendar import collect_calendar_event_pages
from ai_employee.domain.errors import InternalInvariantError


@pytest.mark.asyncio
async def test_calendar_aad_page_limit_stops_before_an_extra_provider_page():
    """第100页仍声明next-page时立即拒绝，不为证明越界再调用第101页供应商。"""
    calls = []

    async def pages():
        """每次generator推进代表一次独立供应商读取；合成空页避免内容和外部I/O。"""
        for index in range(101):
            calls.append(index)
            yield CalendarSyncPage((), f"synthetic-page-{index + 1}", None)

    with pytest.raises(InternalInvariantError) as failure:
        await collect_calendar_event_pages(pages(), scope_key="synthetic-calendar")
    assert failure.value.error_code == "calendar_pagination_invalid"
    assert len(calls) == 100
