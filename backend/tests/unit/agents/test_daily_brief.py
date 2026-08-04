"""每日简报图的合成数据契约测试。"""

import pytest
from langgraph.checkpoint.memory import MemorySaver
from pydantic import ValidationError

from ai_employee.agents.daily_brief.graph import build_daily_brief_graph
from ai_employee.agents.daily_brief.nodes import classify_ambiguous_conversation_intent
from ai_employee.application.ports.model import ModelResponse, ModelUsage
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


@pytest.mark.asyncio
async def test_model_notification_is_aggregated_not_mail_summary() -> None:
    class NotificationModel(FakeModelGateway):
        async def complete(self, **kwargs):  # type: ignore[no-untyped-def]
            from ai_employee.domain.briefs import EmailJudgement

            return ModelResponse(
                value=EmailJudgement(thread_id="t", category="notification", confidence=1),
                usage=ModelUsage(),
            )

    result = await build_daily_brief_graph().ainvoke(
        {
            "mail_threads": [{"thread_id": "t", "sender": "a@x", "subject": "x"}],
            "calendar_events": [],
            "model_gateway": NotificationModel(),
        }
    )
    assert [item["section"] for item in result["content"]["items"]] == ["notifications"]


@pytest.mark.asyncio
async def test_ambiguous_intent_redacts_before_model() -> None:
    fake = FakeModelGateway()
    result = await classify_ambiguous_conversation_intent(
        "what can you do? Bearer secret-value", model_gateway=fake, model_name="fake"
    )
    assert result.intent == "generate_daily_brief"
    assert "secret-value" not in fake.calls[0][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected",
    [
        ("send mail now", "explain_capabilities"),
        ("modify calendar", "explain_capabilities"),
        ("search web", "explain_capabilities"),
        ("upload files", "explain_capabilities"),
        ("remember this", "explain_capabilities"),
        ("plan my work", "explain_capabilities"),
        ("generate today's brief", "generate_daily_brief"),
        ("refresh today's brief", "generate_daily_brief"),
        ("show today's brief", "show_latest_brief"),
    ],
)
async def test_explicit_intents_never_call_model(text: str, expected: str) -> None:
    fake = FakeModelGateway()
    result = await classify_ambiguous_conversation_intent(
        text, model_gateway=fake, model_name="fake"
    )
    assert result.intent == expected
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["发邮件给老板", "create calendar event", "delete calendar event", "plan tomorrow's work"],
)
async def test_write_and_planning_verbs_never_call_model(text: str) -> None:
    fake = FakeModelGateway()
    result = await classify_ambiguous_conversation_intent(
        text, model_gateway=fake, model_name="fake"
    )
    assert result.intent == "explain_capabilities"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_mail_model_input_is_deduplicated_and_has_safe_facts() -> None:
    fake = FakeModelGateway()
    await build_daily_brief_graph().ainvoke(
        {
            "mail_threads": [
                {"thread_id": "t", "sender": "a@x", "subject": "s", "summary": "ok"},
                {"thread_id": "t", "sender": "a@x", "subject": "s"},
                {"thread_id": "spam", "sender": "a@x", "subject": "secret", "labels": ["spam"]},
            ],
            "calendar_events": [],
            "model_gateway": fake,
        }
    )
    assert len(fake.calls) == 1
    assert fake.calls[0][0]["content"] == "daily_brief_v1; locale=zh-CN; only supplied facts"
    assert "secret" not in str(fake.calls)


@pytest.mark.asyncio
async def test_mail_model_input_redacts_builtin_and_configured_values() -> None:
    fake = FakeModelGateway()
    await build_daily_brief_graph().ainvoke(
        {
            "mail_threads": [
                {
                    "thread_id": "safe-id",
                    "sender": "api_key=private-value",
                    "subject": "Bearer token-value 123456",
                    "summary": "CUSTOM_SECRET",
                }
            ],
            "calendar_events": [],
            "model_gateway": fake,
            "model_redaction_patterns": ["CUSTOM_SECRET"],
        }
    )
    sent = str(fake.calls)
    assert (
        "private-value" not in sent
        and "token-value" not in sent
        and "123456" not in sent
        and "CUSTOM_SECRET" not in sent
    )
    assert "safe-id" in sent


def test_graph_accepts_injected_checkpoint_saver() -> None:
    saver = MemorySaver()
    assert build_daily_brief_graph(checkpointer=saver).checkpointer is saver
