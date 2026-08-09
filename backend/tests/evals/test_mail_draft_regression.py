"""邮件草稿 body-only、事实忠实与可追踪元数据的合成回归评测。"""

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from ai_employee.application.ports.model import ModelUsage
from ai_employee.workers.generate_mail_draft import (
    MAIL_DRAFT_PROMPT_VERSION,
    MAX_MAIL_DRAFT_CONTEXT_CHARACTERS,
    MAX_MAIL_DRAFT_CONTEXT_MESSAGES,
    MailDraftContextMessage,
    MailDraftGenerationMetadata,
    MailDraftModelOutput,
    build_mail_draft_context,
    mail_draft_model_messages,
)


def test_mail_draft_synthetic_cases_preserve_body_only_facts_and_safe_metadata() -> None:
    """逐例验证清洗、事实边界、严格输出和不含正文的调用追踪骨架。"""
    cases = json.loads(
        (Path(__file__).parent / "mail_draft_cases.json").read_text(encoding="utf-8")
    )
    assert len(cases) >= 8
    assert {case["language"] for case in cases} == {"zh", "en"}
    assert {case["requires_uncertainty"] for case in cases} == {True, False}

    for case in cases:
        messages = tuple(
            MailDraftContextMessage(
                message_id=message["message_id"],
                thread_id=message["thread_id"],
                sender=message["sender"],
                recipients=tuple(message["recipients"]),
                subject=message["subject"],
                body_text=message["body_text"],
                received_at=datetime.fromisoformat(message["received_at"]),
                is_spam=message["is_spam"],
            )
            for message in case["messages"]
        )
        context = build_mail_draft_context(
            messages=messages,
            instruction=case["instruction"],
            thread_summary=case["thread_summary"],
        )
        assert len(context.messages) <= MAX_MAIL_DRAFT_CONTEXT_MESSAGES
        assert (
            sum(len(message.body_text) for message in context.messages)
            <= MAX_MAIL_DRAFT_CONTEXT_CHARACTERS
        )
        model_messages = mail_draft_model_messages(context)
        user_content = model_messages[-1]["content"]
        for expected in case["expected_context"]:
            assert expected in user_content, case["id"]
        for forbidden in case["forbidden_context"]:
            assert forbidden not in user_content, case["id"]
        for message in case["messages"]:
            assert message["message_id"] not in user_content, case["id"]
            assert message["thread_id"] not in user_content, case["id"]
            assert message["sender"] not in user_content, case["id"]
            assert message["subject"] not in user_content, case["id"]

        output = MailDraftModelOutput(body_text=case["model_output"])
        for required in case["required_output"]:
            assert required in output.body_text, case["id"]
        for forbidden in case["forbidden_output"]:
            assert forbidden not in output.body_text, case["id"]
        if case["requires_uncertainty"]:
            assert any(
                marker in output.body_text.casefold()
                for marker in ("do not have", "cannot confirm", "need", "missing", "不确定")
            ), case["id"]

        metadata = MailDraftGenerationMetadata(
            provider="fake",
            model_name="fake-mail-model",
            prompt_version=MAIL_DRAFT_PROMPT_VERSION,
            input_hash=sha256(user_content.encode("utf-8")).hexdigest(),
            output_schema=MailDraftModelOutput.__name__,
            usage=ModelUsage(),
            status="succeeded",
            error_code=None,
        )
        assert metadata.prompt_version == "mail_draft_v1"
        assert len(metadata.input_hash) == 64
        assert metadata.output_schema == "MailDraftModelOutput"
        serialized_metadata = repr(metadata)
        assert case["instruction"] not in serialized_metadata
        assert output.body_text not in serialized_metadata
