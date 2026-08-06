"""每日简报图的集成式合成测试，不访问网络。"""

import pytest

from ai_employee.agents.daily_brief.graph import build_daily_brief_graph
from ai_employee.agents.daily_brief.nodes import DailyBriefSourceFailure
from ai_employee.integrations.llm.fake import FakeModelGateway


@pytest.mark.asyncio
async def test_no_sources_is_total_failure() -> None:
    with pytest.raises(DailyBriefSourceFailure):
        await build_daily_brief_graph().ainvoke({"mail_threads": [], "calendar_events": []})


@pytest.mark.asyncio
async def test_ambiguous_thread_reaches_model() -> None:
    fake = FakeModelGateway()
    result = await build_daily_brief_graph(model_gateway=fake).ainvoke(
        {
            "mail_threads": [{"thread_id": "t", "sender": "a@x", "subject": "h"}],
            "calendar_events": [],
        }
    )
    assert len(fake.calls) == 1
    assert result["content"]["items"][0]["source_refs"][0]["source_id"] == "t"
