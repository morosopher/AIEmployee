"""每日简报图的合成数据契约测试。"""

import pytest
from pydantic import ValidationError

from ai_employee.agents.daily_brief.graph import build_daily_brief_graph
from ai_employee.domain.briefs import BriefItem, BriefSourceRef, ConversationIntent
from ai_employee.integrations.llm.fake import FakeModelGateway


def test_brief_item_requires_source_and_safe_action() -> None:
    with pytest.raises(ValidationError):
        BriefItem(section="mail_summary", title="x", body_markdown="x", source_refs=[])
    with pytest.raises(ValidationError):
        BriefItem(
            section="mail_summary",
            title="x",
            body_markdown="x",
            source_refs=[BriefSourceRef(source_type="mail", source_id="1")],
            suggested_action_kind="send_email",
        )


def test_intent_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        ConversationIntent(intent="send_email", confidence=1, reason_code="x")


@pytest.mark.asyncio
async def test_graph_filters_spam_and_renders_sources() -> None:
    state = {
        "local_date": "2026-08-04",
        "source_cutoff": "2026-08-04T00:00:00Z",
        "model_gateway": FakeModelGateway(),
        "mail_threads": [
            {"thread_id": "spam", "sender": "x", "subject": "x", "labels": ["SPAM"]},
            {"thread_id": "normal", "sender": "x", "subject": "hello"},
        ],
        "calendar_events": [],
    }
    result = await build_daily_brief_graph().ainvoke(state)
    assert all(item["source_refs"] for item in result["content"]["items"])
    assert "spam" not in {
        item["source_refs"][0]["source_id"] for item in result["content"]["items"]
    }


@pytest.mark.asyncio
async def test_graph_emits_all_node_lifecycle_events() -> None:
    class Sink:
        def __init__(self) -> None:
            self.events: list[object] = []

        def record(self, event: object) -> None:
            self.events.append(event)

    sink = Sink()
    await build_daily_brief_graph().ainvoke(
        {
            "task_run_id": "task",
            "task_step_event_sink": sink,
            "mail_threads": [{"thread_id": "t", "sender": "x", "subject": "x"}],
            "calendar_events": [],
        }
    )
    assert len(sink.events) == 12
