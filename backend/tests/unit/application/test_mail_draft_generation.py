"""验证邮件草稿 body-only 模型输入与严格输出边界。"""

import json
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.integrations.llm.fake import FakeModelGateway
from ai_employee.workers.generate_mail_draft import (
    MailDraftContextMessage,
    MailDraftModelOutput,
    _mask_mailbox_addresses,
    build_mail_draft_context,
    load_mail_draft_prompt,
    mail_draft_model_messages,
)


class _IndexCountingText(str):
    """统计生产扫描器通过整数索引访问字符的次数，不改变字符串语义。"""

    def __new__(cls, value: str) -> Self:
        """创建携带独立访问计数的不可变字符串实例。"""
        instance = super().__new__(cls, value)
        instance.index_accesses = 0
        return instance

    def __getitem__(self, key: int | slice) -> str:
        """整数索引计为一次字符访问；切片只用于构造候选，不按长度放大计数。"""
        if isinstance(key, int):
            self.index_accesses += 1
        return super().__getitem__(key)


def test_model_output_forbids_recipient_and_subject_fields() -> None:
    """模型输出 Schema 只允许正文。"""
    with pytest.raises(ValidationError):
        MailDraftModelOutput.model_validate(
            {"body_text": "Synthetic body", "to": ["recipient@example.test"]}
        )


def test_model_context_keeps_only_three_messages_and_twelve_thousand_characters() -> None:
    """上下文过滤垃圾邮件、tracking URL 并限制消息数与总字符。"""
    messages = tuple(
        MailDraftContextMessage(
            message_id=f"message-{index}",
            thread_id="thread-synthetic",
            sender=f"sender-{index}@example.test",
            recipients=("owner@example.test",),
            subject="Synthetic subject",
            body_text=("body " + str(index) + " https://tracking.example/pixel\n") * 4_000,
            received_at=datetime(2026, 8, 9, tzinfo=UTC) + timedelta(minutes=index),
            is_spam=index == 3,
        )
        for index in range(4)
    )
    context = build_mail_draft_context(messages=messages, instruction="Reply briefly")

    assert len(context.messages) == 3
    assert sum(len(item.body_text) for item in context.messages) <= 12_000
    assert all("tracking.example" not in item.body_text for item in context.messages)
    assert all(item.message_id != "message-3" for item in context.messages)


def test_model_messages_exclude_addresses_subject_thread_and_removed_history() -> None:
    """序列化给模型的内容只含清洗后的正文事实与用户写作要求。"""
    context = build_mail_draft_context(
        messages=(
            MailDraftContextMessage(
                message_id="secret-message-id",
                thread_id="secret-thread-id",
                sender="sender@example.test",
                recipients=("owner@example.test",),
                subject="Secret subject",
                body_text=(
                    "Could you confirm the synthetic question?\n"
                    "https://tracking.example/pixel\n"
                    "Best regards\nSender Signature"
                ),
                received_at=datetime(2026, 8, 9, tzinfo=UTC),
            ),
        ),
        instruction="Reply briefly to owner@example.test",
    )

    serialized = str(mail_draft_model_messages(context))

    assert "Could you confirm the synthetic question?" in serialized
    assert "tracking.example" not in serialized
    assert "Sender Signature" not in serialized
    assert "sender@example.test" not in serialized
    assert "owner@example.test" not in serialized
    assert "Secret subject" not in serialized
    assert "secret-thread-id" not in serialized
    assert "secret-message-id" not in serialized


def test_model_messages_mask_quoted_unicode_idn_and_punctuated_mailboxes() -> None:
    """指令、摘要与正文中的疑似邮箱都必须在本地掩码，并保留周围标点。"""
    context = build_mail_draft_context(
        messages=(
            MailDraftContextMessage(
                message_id="synthetic-message",
                thread_id="synthetic-thread",
                sender="",
                recipients=(),
                subject="",
                body_text=(
                    "Primary (Case.Sensitive+Tag@Sub.Example.TEST); "
                    "international 用户@例子.测试!"
                ),
                received_at=datetime(2026, 8, 9, tzinfo=UTC),
            ),
        ),
        instruction='Draft to "quoted local"@Example.Test, please.',
        thread_summary="备用地址是 owner@例子.测试。",
    )

    payload = json.loads(mail_draft_model_messages(context)[1]["content"])

    assert payload["instruction"] == "Draft to [ADDRESS_REMOVED], please."
    assert payload["thread_summary"] == "备用地址是 [ADDRESS_REMOVED]。"
    assert payload["related_body_facts"] == [
        "Primary ([ADDRESS_REMOVED]); international [ADDRESS_REMOVED]!"
    ]


def test_model_context_preserves_non_mailbox_at_sign_text() -> None:
    """提及、分隔符和无 DNS 域的普通 ``@`` 文本不得被邮箱扫描器破坏。"""
    context = build_mail_draft_context(
        messages=(
            MailDraftContextMessage(
                message_id="synthetic-message",
                thread_id="synthetic-thread",
                sender="",
                recipients=(),
                subject="",
                body_text="Keep @team, A @ B, file@localhost and 版本@草案 literal.",
                received_at=datetime(2026, 8, 9, tzinfo=UTC),
            ),
        ),
        instruction="Mention @team and keep A @ B unchanged.",
        thread_summary="file@localhost 与 版本@草案 都只是普通文本。",
    )

    payload = json.loads(mail_draft_model_messages(context)[1]["content"])

    assert payload["instruction"] == "Mention @team and keep A @ B unchanged."
    assert payload["thread_summary"] == "file@localhost 与 版本@草案 都只是普通文本。"
    assert payload["related_body_facts"] == [
        "Keep @team, A @ B, file@localhost and 版本@草案 literal."
    ]


def test_mailbox_masking_visits_adversarial_escaped_quotes_linearly() -> None:
    """大量转义引号加 ``@`` 不得触发对既有前缀的重复回扫。"""
    text = _IndexCountingText('\\"@' * 8_000)

    masked = _mask_mailbox_addresses(text)

    assert masked == text
    assert text.index_accesses <= len(text) * 12


def test_mailbox_masking_handles_escaped_quote_and_quoted_smtputf8_boundaries() -> None:
    """quoted local-part 内的转义引号与 Unicode/IDN 地址必须整段掩码并保留标点。"""
    text = 'Keep \\"@ literal; send to ("用户 \\"别名"@例子.测试)!'

    assert _mask_mailbox_addresses(text) == (
        'Keep \\"@ literal; send to ([ADDRESS_REMOVED])!'
    )


def test_prompt_forbids_model_control_and_requires_uncertainty() -> None:
    """版本化 Prompt 明确限制模型只生成正文且不得臆造事实。"""
    prompt = load_mail_draft_prompt()

    assert "不得选择、添加、删除或修改收件人" in prompt
    assert "不得自行改变日期、时间、金额、承诺、决定" in prompt
    assert "事实不足时必须明确表达不确定" in prompt
    assert "不代表已批准或已发送" in prompt


@pytest.mark.asyncio
async def test_fake_gateway_supports_strict_mail_draft_output() -> None:
    """离线 Fake 必须返回严格 body-only Schema，评测和 CI 不访问真实模型。"""
    response = await FakeModelGateway().complete(
        model_name="fake-mail-model",
        prompt_version="mail_draft_v1",
        messages=({"role": "user", "content": "Synthetic facts"},),
        response_model=MailDraftModelOutput,
    )

    assert response.value == MailDraftModelOutput(body_text="Synthetic mail draft body.")


def test_task_runner_registers_mail_draft_generation_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """耐久 Worker 必须把草稿生成任务路由到专用 body-only 步骤。"""
    from ai_employee.workers import execute_task as execute_task_module

    captured: dict[str, object] = {}
    generated_step = object()

    class _CapturingRunner:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(execute_task_module, "DurableTaskRunner", _CapturingRunner)
    monkeypatch.setattr(
        execute_task_module,
        "build_generate_mail_draft_task_step",
        lambda **_kwargs: generated_step,
        raising=False,
    )
    execute_task_module.build_task_runner_for_session(object(), settings=Settings())
    resolver = captured["resolve_steps"]
    assert callable(resolver)
    task = LeasedTask(
        task_id=uuid4(),
        user_id=uuid4(),
        kind="mail_draft.generate",
        input_payload={"draft_id": str(uuid4()), "instruction": "Reply briefly"},
        started_at=datetime(2026, 8, 9, tzinfo=UTC),
    )

    assert resolver(task) == (generated_step,)
