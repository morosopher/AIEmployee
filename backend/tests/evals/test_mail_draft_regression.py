"""邮件草稿 body-only、事实忠实与可追踪元数据的合成回归评测。"""

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import pytest

from ai_employee.integrations.llm.fake import FakeModelGateway
from ai_employee.workers.generate_mail_draft import (
    MAIL_DRAFT_PROMPT_VERSION,
    MAX_MAIL_DRAFT_CONTEXT_CHARACTERS,
    MAX_MAIL_DRAFT_CONTEXT_MESSAGES,
    MailDraftContextMessage,
    MailDraftGenerationMetadata,
    MailDraftModelOutput,
    build_mail_draft_context,
    load_mail_draft_prompt,
    mail_draft_model_messages,
)


@pytest.mark.asyncio
async def test_mail_draft_synthetic_cases_preserve_body_only_facts_and_safe_metadata() -> None:
    """逐例通过 Fake 网关验证清洗、严格输出与不含正文的调用追踪。"""
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

        system_content = model_messages[0]["content"]
        assert system_content == load_mail_draft_prompt(), case["id"]
        for prompt_boundary in (
            "不得选择、添加、删除或修改收件人",
            "主题、线程",
            "日期、时间、金额、承诺、决定",
            "事实不足时必须明确表达不确定",
        ):
            assert prompt_boundary in system_content, case["id"]

        # 每个案例必须穿过生产使用的结构化模型端口；fixture 只注入确定性 Fake 正文，
        # 不能直接构造 Pydantic 输出，否则 Prompt 版本或响应 Schema 接错时评测仍会误通过。
        gateway = FakeModelGateway(mail_draft_body=case["model_output"])
        response = await gateway.complete(
            model_name="fake-mail-model",
            prompt_version="mail_draft_v1",
            messages=model_messages,
            response_model=MailDraftModelOutput,
        )
        assert gateway.calls == [model_messages], case["id"]
        assert isinstance(response.value, MailDraftModelOutput), case["id"]
        output = response.value
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
            usage=response.usage,
            status="succeeded",
            error_code=None,
        )
        assert metadata.prompt_version == "mail_draft_v1"
        assert len(metadata.input_hash) == 64
        assert metadata.output_schema == "MailDraftModelOutput"
        serialized_metadata = repr(metadata)
        assert case["instruction"] not in serialized_metadata
        assert output.body_text not in serialized_metadata
