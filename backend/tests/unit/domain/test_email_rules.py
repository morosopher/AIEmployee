"""验证邮件确定性分类与紧急度规则彼此独立。"""

from ai_employee.domain.email import (
    EmailCategory,
    EmailMessage,
    EmailUrgency,
    classify_by_rules,
    classify_urgency_by_rules,
)


def test_spam_label_classifies_without_model_fallback() -> None:
    """Gmail 垃圾邮件标签必须给出确定分类，正文不应进入模型路径。"""
    result = classify_by_rules(
        EmailMessage(
            sender="untrusted@example.test",
            subject="Synthetic offer",
            labels=frozenset({"SPAM"}),
        ),
        known_work_sender_domains=frozenset(),
    )

    assert result is not None
    assert result.category is EmailCategory.SPAM
    assert result.reason_codes == ("gmail_label_spam",)


def test_list_unsubscribe_and_bulk_precedence_are_notifications() -> None:
    """退订或批量投递头必须短路为通知，避免无意义模型分类。"""
    result = classify_by_rules(
        EmailMessage(
            sender="updates@example.test",
            subject="Synthetic update",
            headers={"List-Unsubscribe": "<https://example.test/unsubscribe>"},
        ),
        known_work_sender_domains=frozenset(),
    )

    assert result is not None
    assert result.category is EmailCategory.NOTIFICATION
    assert result.reason_codes == ("header_list_unsubscribe",)

    bulk_result = classify_by_rules(
        EmailMessage(
            sender="updates@example.test",
            subject="Synthetic update",
            headers={"Precedence": "bulk"},
        ),
        known_work_sender_domains=frozenset(),
    )

    assert bulk_result is not None
    assert bulk_result.category is EmailCategory.NOTIFICATION
    assert bulk_result.reason_codes == ("header_precedence_bulk",)


def test_known_work_sender_domain_classifies_as_work() -> None:
    """用户配置的已知工作域应产生可审计的确定性工作分类。"""
    result = classify_by_rules(
        EmailMessage(sender="manager@company.example", subject="Synthetic agenda"),
        known_work_sender_domains=frozenset({"company.example"}),
    )

    assert result is not None
    assert result.category is EmailCategory.WORK
    assert result.reason_codes == ("known_work_sender_domain",)


def test_ambiguous_message_returns_none_for_model_decision() -> None:
    """未命中确定性规则的邮件必须明确交由后续模型路径判断。"""
    result = classify_by_rules(
        EmailMessage(sender="person@example.test", subject="Synthetic note"),
        known_work_sender_domains=frozenset(),
    )

    assert result is None


def test_urgency_is_a_separate_dimension_from_category() -> None:
    """紧急度规则不得从分类推导，垃圾邮件也可独立标记为紧急。"""
    message = EmailMessage(
        sender="untrusted@example.test",
        subject="URGENT: Synthetic incident",
        labels=frozenset({"SPAM"}),
    )

    category_result = classify_by_rules(message, known_work_sender_domains=frozenset())
    urgency_result = classify_urgency_by_rules(message)

    assert category_result is not None
    assert category_result.category is EmailCategory.SPAM
    assert urgency_result.urgency is EmailUrgency.URGENT
    assert urgency_result.reason_codes == ("subject_urgent_marker",)
