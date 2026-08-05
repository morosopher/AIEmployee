"""每日简报确定性规则的已批准合成评测基线。"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ai_employee.agents.daily_brief.graph import build_daily_brief_graph
from ai_employee.domain.calendar import CalendarEvent, find_conflicts
from ai_employee.domain.email import (
    EmailCategory,
    EmailMessage,
    EmailUrgency,
    classify_by_rules,
    classify_urgency_by_rules,
)
from ai_employee.integrations.llm.fake import FakeModelGateway


def test_daily_brief_synthetic_baseline_preserves_rules_sources_and_facts() -> None:
    """防止规则回归、紧急度降级、来源丢失及摘要臆造进入发布分支。"""
    cases = json.loads(
        (Path(__file__).parent / "daily_brief_cases.json").read_text(encoding="utf-8")
    )
    assert len(cases) >= 50
    assert {case["category"] for case in cases} == {"work", "notification", "spam", "other"}
    assert {case["language"] for case in cases} == {"zh", "en"}
    assert {case["needs_reply"] for case in cases} == {True, False}
    assert {case["conflict"] for case in cases} == {True, False}
    assert any(case["deadline"] is not None for case in cases)
    assert any(case["deadline"] is None for case in cases)
    assert any(case.get("adversarial_html", False) for case in cases)
    for case in cases:
        message = EmailMessage(
            sender=case["sender"],
            subject=case["subject"],
            labels=frozenset(case["labels"]),
            headers=case["headers"],
        )
        classification = classify_by_rules(
            message, known_work_sender_domains=frozenset({"work.example"})
        )
        if case["category"] == "other":
            assert classification is None
        else:
            assert classification is not None
            assert classification.category is EmailCategory(case["category"])
        assert classify_urgency_by_rules(message).urgency is EmailUrgency(case["urgency"])
        assert case["source_ref"] == f"mail:{case['id']}"
        # ``summary_fact`` 是评测批准的合成事实，而非对主题做子串匹配：后者既不能
        # 证明来源绑定，也会让恶意 HTML 意外成为“通过”的摘要内容。
        assert case["summary_fact"]
        assert "<" not in case["summary_fact"] and ">" not in case["summary_fact"]
        if case["deadline"] is not None:
            assert datetime.fromisoformat(case["deadline"]).tzinfo is not None
        assert isinstance(case["needs_reply"], bool)


@pytest.mark.asyncio
async def test_daily_brief_evaluation_binds_items_to_fixture_sources_without_html_echo() -> None:
    """评测图只能引用给定合成线程，且对抗 HTML 不得进入面向用户的结构化输出。"""
    cases = json.loads(
        (Path(__file__).parent / "daily_brief_cases.json").read_text(encoding="utf-8")
    )
    for case in cases:
        result = await build_daily_brief_graph().ainvoke(
            {
                "mail_threads": [
                    {
                        "thread_id": case["id"],
                        "sender": case["sender"],
                        "subject": case["subject"],
                        "labels": case["labels"],
                        "headers": case["headers"],
                        "summary": case["summary_fact"],
                        "needs_reply": case["needs_reply"],
                        "deadline": case["deadline"],
                    }
                ],
                "calendar_events": [],
                "work_email_domains": ["work.example"],
                "model_gateway": FakeModelGateway(),
            }
        )
        serialized = json.dumps(result["content"], ensure_ascii=False)
        source_ids = {
            reference["source_id"]
            for item in result["content"]["items"]
            for reference in item["source_refs"]
        }
        if case["category"] == "spam":
            assert source_ids == set()
        else:
            assert source_ids == {case["id"]}
            item_text = " ".join(
                f"{item['title']} {item['body_markdown']}"
                for item in result["content"]["items"]
            )
            # 条目文字只能来自本案例显式批准的摘要/主题事实，不能由图凭空杜撰来源文本。
            allowed_tokens = (
                set(case["summary_fact"].lower().split())
                | set(case["subject"].lower().split())
                | {
                    case["id"].lower(), "邮件线程", "已完成分类。", "1", "条通知",
                    "通知已折叠汇总。", "建议处理", "需要回复",
                }
            )
            rendered_tokens = set(item_text.lower().replace("#", " ").split())
            assert rendered_tokens <= allowed_tokens | {"-", "•"}
        classifications = result.get("classifications", [])
        if classifications:
            judgement = classifications[0]
            assert judgement["needs_reply"] is case["needs_reply"]
            assert judgement["deadline_at"] == case["deadline"]
        if case.get("adversarial_html"):
            assert "<script" not in serialized and "<img" not in serialized


def test_calendar_conflict_baseline_matches_deterministic_rule() -> None:
    """确认评测集中标记的日程冲突能由纯领域规则复现，避免提示词替代确定性事实。"""
    cases = json.loads(
        (Path(__file__).parent / "daily_brief_cases.json").read_text(encoding="utf-8")
    )
    start = datetime.fromisoformat("2026-08-05T09:00:00+00:00")
    for case in cases:
        second_start = start + (timedelta(minutes=30) if case["conflict"] else timedelta(hours=1))
        conflicts = find_conflicts(
            (
                CalendarEvent(
                    event_id=f"{case['id']}-a", start_at=start, end_at=start + timedelta(hours=1)
                ),
                CalendarEvent(
                    event_id=f"{case['id']}-b",
                    start_at=second_start,
                    end_at=second_start + timedelta(hours=1),
                ),
            )
        )
        assert bool(conflicts) is case["conflict"]
