"""每日简报确定性规则的已批准合成评测基线。"""

import json
from pathlib import Path

from ai_employee.domain.email import (
    EmailCategory,
    EmailMessage,
    EmailUrgency,
    classify_by_rules,
    classify_urgency_by_rules,
)


def test_daily_brief_synthetic_baseline_preserves_rules_sources_and_facts() -> None:
    """防止规则回归、紧急度降级、来源丢失及摘要臆造进入发布分支。"""
    cases = json.loads((Path(__file__).parent / "daily_brief_cases.json").read_text(encoding="utf-8"))
    assert len(cases) >= 50
    assert {case["category"] for case in cases} == {"work", "notification", "spam", "other"}
    assert {case["language"] for case in cases} == {"zh", "en"}
    for case in cases:
        message = EmailMessage(sender=case["sender"], subject=case["subject"], labels=frozenset(case["labels"]), headers=case["headers"])
        classification = classify_by_rules(message, known_work_sender_domains=frozenset({"work.example"}))
        if case["category"] == "other":
            assert classification is None
        else:
            assert classification is not None
            assert classification.category is EmailCategory(case["category"])
        assert classify_urgency_by_rules(message).urgency is EmailUrgency(case["urgency"])
        assert case["source_ref"] and case["summary_fact"] in case["subject"]
