"""邮件草稿 body-only、事实忠实与可追踪元数据的离线合成回归评测。

本文件只证明清洗、Prompt 边界、结构化 Fake 管线和确定性事实 oracle 会互相约束；它不访问
真实模型，也不声称这些断言能够代表任何真实模型供应商的生成质量。
"""

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import TypedDict, cast

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

_PROMPT_BOUNDARIES = (
    "不得选择、添加、删除或修改收件人",
    "主题、线程",
    "日期、时间、金额、承诺、决定",
    "事实不足时必须明确表达不确定",
)
_UNKNOWN_CONTEXT_MARKERS = (
    "not been",
    "no ",
    "missing",
    "absent",
    "cannot confirm",
    "未记录",
    "缺少",
    "没有",
)
_UNSUPPORTED_COMMITMENT_MARKERS = (
    "i promise",
    "we guarantee",
    "has been approved",
    "is approved",
    "我保证",
    "已经批准",
)


class _MailDraftEvalMessage(TypedDict):
    """描述一个合成同步消息输入，不包含真实个人数据。"""

    message_id: str
    thread_id: str
    sender: str
    recipients: list[str]
    subject: str
    body_text: str
    received_at: str
    is_spam: bool


class _MailDraftEvalCase(TypedDict):
    """描述输入事实与独立 oracle；故意不提供模型期望正文。"""

    id: str
    language: str
    instruction: str
    thread_summary: str
    messages: list[_MailDraftEvalMessage]
    expected_context: list[str]
    forbidden_context: list[str]
    required_output: list[str]
    forbidden_output: list[str]
    requires_uncertainty: bool


def _load_cases() -> tuple[_MailDraftEvalCase, ...]:
    """读取受版本控制的合成案例，并在 JSON 边界收窄为精确 TypedDict。"""
    raw = json.loads(
        (Path(__file__).parent / "mail_draft_cases.json").read_text(encoding="utf-8")
    )
    if not isinstance(raw, list):
        raise TypeError("mail draft eval fixture must be a list")
    return tuple(cast(_MailDraftEvalCase, case) for case in raw)


def _case(case_id: str) -> _MailDraftEvalCase:
    """按稳定 ID 取得一个案例，缺失时让测试固定失败。"""
    return next(case for case in _load_cases() if case["id"] == case_id)


def _build_model_messages(
    case: _MailDraftEvalCase,
    *,
    prompt_override: str | None = None,
    context_replacements: tuple[tuple[str, str], ...] = (),
) -> tuple[dict[str, str], ...]:
    """穿过生产上下文构造器，并可施加单一 mutation 供敏感性测试使用。"""
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
    model_messages = list(mail_draft_model_messages(context))
    if prompt_override is not None:
        model_messages[0] = {"role": "system", "content": prompt_override}
    for old, new in context_replacements:
        model_messages[-1] = {
            "role": "user",
            "content": model_messages[-1]["content"].replace(old, new),
        }
    return tuple(model_messages)


def _deterministic_fake_body(model_messages: tuple[dict[str, str], ...]) -> str:
    """仅从实际模型消息派生 Fake 正文，使上下文 mutation 会传播到输出。

    该函数不是语言质量模拟器。它只提供一个透明、可解释的离线响应：已知事实原样回显，
    缺失事实明确不确定；若关键事实边界被从 Prompt 删除，则故意产生不受支持承诺，确保
    mutation 测试能杀死弱化 Prompt 的改动。
    """
    system_content = model_messages[0]["content"]
    payload = json.loads(model_messages[-1]["content"])
    instruction = payload.get("instruction")
    thread_summary = payload.get("thread_summary")
    related_facts = payload.get("related_body_facts")
    if (
        not isinstance(instruction, str)
        or not isinstance(thread_summary, str)
        or not isinstance(related_facts, list)
        or any(not isinstance(fact, str) for fact in related_facts)
    ):
        raise TypeError("mail draft eval model payload is invalid")
    latest_fact = related_facts[-1] if related_facts else ""
    combined = f"{instruction} {thread_summary}".casefold()
    if any(marker in combined for marker in _UNKNOWN_CONTEXT_MARKERS):
        body = "I cannot confirm the requested fact from the available context."
        if latest_fact:
            body = f"{body} {latest_fact}"
    else:
        body = latest_fact or "I cannot confirm the requested fact from the available context."
    if "日期、时间、金额、承诺、决定" not in system_content:
        body = f"{body} I promise this is approved."
    return body


async def _evaluate_case(
    case: _MailDraftEvalCase,
    *,
    prompt_override: str | None = None,
    context_replacements: tuple[tuple[str, str], ...] = (),
    output_suffix: str = "",
) -> None:
    """通过 FakeModelGateway 完成一次案例，并运行独立上下文、Prompt 与输出 oracle。"""
    model_messages = _build_model_messages(
        case,
        prompt_override=prompt_override,
        context_replacements=context_replacements,
    )
    user_content = model_messages[-1]["content"]
    fake_body = f"{_deterministic_fake_body(model_messages)}{output_suffix}"
    gateway = FakeModelGateway(mail_draft_body=fake_body)
    response = await gateway.complete(
        model_name="fake-mail-model",
        prompt_version=MAIL_DRAFT_PROMPT_VERSION,
        messages=model_messages,
        response_model=MailDraftModelOutput,
    )

    # 先经过真实 Fake 端口再执行 oracle，使 Prompt、上下文或输出 mutation 均会被评测捕获。
    assert gateway.calls == [model_messages], case["id"]
    assert isinstance(response.value, MailDraftModelOutput), case["id"]
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
    assert system_content == (prompt_override or load_mail_draft_prompt()), case["id"]
    for prompt_boundary in _PROMPT_BOUNDARIES:
        assert prompt_boundary in system_content, case["id"]

    output = response.value
    for required in case["required_output"]:
        assert required in output.body_text, case["id"]
    for forbidden in case["forbidden_output"]:
        assert forbidden not in output.body_text, case["id"]
    output_casefold = output.body_text.casefold()
    assert not any(
        marker in output_casefold for marker in _UNSUPPORTED_COMMITMENT_MARKERS
    ), case["id"]
    if case["requires_uncertainty"]:
        assert any(
            marker in output_casefold
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
    assert metadata.prompt_version == MAIL_DRAFT_PROMPT_VERSION
    assert len(metadata.input_hash) == 64
    assert metadata.output_schema == MailDraftModelOutput.__name__
    serialized_metadata = repr(metadata)
    assert case["instruction"] not in serialized_metadata
    assert output.body_text not in serialized_metadata


def test_mail_draft_eval_cases_do_not_inject_the_expected_model_output() -> None:
    """评测输入不得把待验证正文同时作为 Fake 返回值保存在同一 fixture。"""
    assert all("model_output" not in case for case in _load_cases())


@pytest.mark.asyncio
async def test_mail_draft_synthetic_cases_preserve_body_only_facts_and_safe_metadata() -> None:
    """逐例验证离线清洗、Prompt、Fake 结构化端口、事实 oracle 与安全元数据。"""
    cases = _load_cases()
    assert len(cases) >= 8
    assert {case["language"] for case in cases} == {"zh", "en"}
    assert {case["requires_uncertainty"] for case in cases} == {True, False}

    for case in cases:
        await _evaluate_case(case)


@pytest.mark.asyncio
async def test_mail_draft_eval_rejects_removed_prompt_fact_boundary() -> None:
    """删除日期/金额/承诺限制必须使离线 Prompt 契约评测失败。"""
    mutated_prompt = load_mail_draft_prompt().replace(
        "不得自行改变日期、时间、金额、承诺、决定、审批结果或其他业务事实。",
        "",
    )

    with pytest.raises(AssertionError):
        await _evaluate_case(
            _case("confirm-scheduled-review"),
            prompt_override=mutated_prompt,
        )


@pytest.mark.asyncio
async def test_mail_draft_eval_rejects_replaced_context_fact() -> None:
    """替换已批准金额事实必须同时破坏上下文与输出 oracle。"""
    with pytest.raises(AssertionError):
        await _evaluate_case(
            _case("preserve-approved-amount"),
            context_replacements=(("USD 1,250", "USD 1,500"),),
        )


@pytest.mark.asyncio
async def test_mail_draft_eval_rejects_unsupported_promise_or_hallucination() -> None:
    """即使正文仍含正确事实，额外承诺也必须被独立 oracle 拒绝。"""
    with pytest.raises(AssertionError):
        await _evaluate_case(
            _case("confirm-scheduled-review"),
            output_suffix=" I promise this is approved.",
        )
