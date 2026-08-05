"""每日简报图节点：确定性规则先行，模型只处理脱敏歧义事实。"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from ai_employee.application.ports.task_steps import TaskStepEvent
from ai_employee.domain.briefs import (
    BriefItem,
    BriefPriority,
    BriefSection,
    BriefSourceRef,
    ConversationIntent,
    DailyBriefContent,
)
from ai_employee.domain.calendar import CalendarEvent, find_conflicts
from ai_employee.domain.email import (
    EmailCategory,
    EmailMessage,
    classify_by_rules,
    classify_urgency_by_rules,
)
from ai_employee.domain.model_redaction import redact_for_model
from ai_employee.integrations.llm.openai_compatible import ModelGatewayError


class DailyBriefSourceFailure(RuntimeError):
    """所有源均不可用时阻止写入空的成功简报。"""


def _event(state: dict[str, Any], name: str, status: str) -> None:
    event = {"step": name, "status": status}
    state.setdefault("step_events", []).append(event)
    sink = state.get("task_step_event_sink")
    if sink is not None:
        sink.record(
            TaskStepEvent(
                task_run_id=str(state.get("task_run_id", "")), step_name=name, status=status
            )
        )


def load_sources(state: dict[str, Any]) -> dict[str, Any]:
    """规范化输入源并记录步骤事件。"""
    _event(state, "load_sources", "started")
    state.setdefault("mail_threads", [])
    state.setdefault("calendar_events", [])
    state.setdefault("warnings", [])
    _event(state, "load_sources", "completed")
    return state


def apply_deterministic_rules(state: dict[str, Any]) -> dict[str, Any]:
    """过滤垃圾邮件并应用分类、紧急度等纯规则。"""
    _event(state, "apply_deterministic_rules", "started")
    result: list[dict[str, Any]] = []
    spam_count = 0
    for thread in state.get("mail_threads", []):
        message = EmailMessage(
            sender=thread.get("sender", ""),
            subject=thread.get("subject", ""),
            labels=frozenset(thread.get("labels", [])),
            headers=thread.get("headers", {}),
        )
        classification = classify_by_rules(
            message, known_work_sender_domains=set(state.get("work_email_domains", []))
        )
        urgency = classify_urgency_by_rules(message)
        if classification and classification.category is EmailCategory.SPAM:
            spam_count += 1
            state.setdefault("spam_thread_ids", set()).add(thread.get("thread_id", ""))
            continue
        if classification:
            result.append(
                {
                    "thread_id": thread.get("thread_id", ""),
                    "category": classification.category.value,
                    "urgency": urgency.urgency.value,
                    # 上游已规范化的确定性提取事实可直接进入简报，不允许模型再猜测。
                    "needs_reply": bool(thread.get("needs_reply", False)),
                    "deadline_at": thread.get("deadline_at"),
                    "reason_codes": classification.reason_codes + urgency.reason_codes,
                }
            )
    state["classifications"] = result
    if spam_count:
        state["warnings"].append(f"spam_count:{spam_count}")
    _event(state, "apply_deterministic_rules", "completed")
    return state


async def classify_ambiguous_threads(state: dict[str, Any]) -> dict[str, Any]:
    """仅把歧义线程的最小事实交给模型，每个线程最多一次。"""
    _event(state, "classify_ambiguous_threads", "started")
    classified = {
        item["thread_id"]
        for item in state.get("classifications", [])
        if item.get("category") is not None
    }
    gateway = state.get("model_gateway")
    outputs = []
    processed_thread_ids: set[str] = set(state.get("spam_thread_ids", set()))
    if gateway:
        for thread in state.get("mail_threads", []):
            thread_id = thread.get("thread_id", "")
            if thread_id in classified or thread_id in processed_thread_ids:
                continue
            processed_thread_ids.add(thread_id)
            # 线程 ID 是内部稳定来源键，绝不能被脱敏替换；其余可变供应商文本在本地清洗。
            patterns = tuple(state.get("model_redaction_patterns", ()))
            facts = {
                "thread_id": thread_id,
                "subject": redact_for_model(
                    thread.get("subject", ""), configured_patterns=patterns
                ).text,
                "sender": redact_for_model(
                    thread.get("sender", ""), configured_patterns=patterns
                ).text,
                "summary": redact_for_model(
                    thread.get("summary", ""), configured_patterns=patterns
                ).text,
                "known_urgency": None,
            }
            try:
                response = await gateway.complete(
                    model_name=state.get("model_name", ""),
                    prompt_version="daily_brief_v1",
                    messages=[
                        {
                            "role": "system",
                            "content": f"daily_brief_v1; locale={state.get('locale', 'zh-CN')}; only supplied facts",
                        },
                        {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
                    ],
                    response_model=__import__(
                        "ai_employee.domain.briefs", fromlist=["EmailJudgement"]
                    ).EmailJudgement,
                )
                _record_model_invocation(state, facts, response)
                judgement = response.value.model_copy(update={"thread_id": thread_id})
                if response.value.thread_id != thread_id:
                    state["warnings"].append(f"model_thread_id_mismatch:{thread_id}")
                if "fake_partial" in judgement.reason_codes:
                    state["warnings"].append(f"model_partial:{thread_id}")
                outputs.append(judgement.model_dump())
            except ModelGatewayError as exc:
                _record_model_failure(state, facts, exc.code)
                if exc.code != "model_invalid_output":
                    state["warnings"].append(f"model_classification_failed:{thread_id}")
                    continue
                # 只允许一次明确修复请求；第二次失败保留确定性结果并标记 partial。
                try:
                    response = await gateway.complete(
                        model_name=state.get("model_name", ""),
                        prompt_version="daily_brief_v1",
                        messages=[
                            {
                                "role": "system",
                                "content": f"daily_brief_v1; locale={state.get('locale', 'zh-CN')}; only supplied facts",
                            },
                            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
                            {"role": "user", "content": "请修复为严格 JSON"},
                        ],
                        response_model=__import__(
                            "ai_employee.domain.briefs", fromlist=["EmailJudgement"]
                        ).EmailJudgement,
                    )
                    _record_model_invocation(state, facts, response)
                    judgement = response.value.model_copy(update={"thread_id": thread_id})
                    if response.value.thread_id != thread_id:
                        state["warnings"].append(f"model_thread_id_mismatch:{thread_id}")
                    if "fake_partial" in judgement.reason_codes:
                        state["warnings"].append(f"model_partial:{thread_id}")
                    outputs.append(judgement.model_dump())
                except ModelGatewayError as repair_error:
                    _record_model_failure(state, facts, repair_error.code)
                    state["warnings"].append(f"model_classification_failed:{thread_id}")
    state["model_items"] = outputs
    _event(state, "classify_ambiguous_threads", "completed")
    return state


def _record_model_invocation(state: dict[str, Any], facts: dict[str, Any], response: Any) -> None:
    """收集不含 Prompt 或模型正文的调用元数据，供事务性审计落库。"""
    usage = response.usage
    state.setdefault("model_invocations", []).append(
        {
            "provider": type(state["model_gateway"]).__name__.removesuffix("Gateway").lower(),
            "model_name": state.get("model_name", ""),
            "prompt_version": "daily_brief_v1",
            "input_hash": sha256(
                json.dumps(facts, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "output_schema": "EmailJudgement",
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "estimated_cost_microusd": usage.estimated_cost_microusd,
            "latency_ms": usage.latency_ms,
            "status": "succeeded",
            "error_code": None,
        }
    )


def _record_model_failure(state: dict[str, Any], facts: dict[str, Any], error_code: str) -> None:
    """记录失败调用的最小审计元数据，绝不保存 prompt 或模型正文。"""
    state.setdefault("model_invocations", []).append(
        {
            "provider": type(state["model_gateway"]).__name__.removesuffix("Gateway").lower(),
            "model_name": state.get("model_name", ""),
            "prompt_version": "daily_brief_v1",
            "input_hash": sha256(
                json.dumps(facts, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "output_schema": "EmailJudgement",
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_microusd": None,
            "latency_ms": 0,
            "status": "failed",
            "error_code": error_code,
        }
    )


def detect_calendar_conflicts(state: dict[str, Any]) -> dict[str, Any]:
    """兼容图节点约定的冲突检测名称。"""
    return detect_calendar_conflicts_node(state)


def classify_conversation_intent(text: str) -> dict[str, Any]:
    """用窄范围中英文规则分类对话，拒绝任意工具动词。"""
    normalized = text.casefold()
    if any(
        token in normalized
        for token in (
            "send email",
            "send mail",
            "发送邮件",
            "modify calendar",
            "change calendar",
            "修改日历",
            "web search",
            "search web",
            "网页搜索",
            "upload",
            "upload files",
            "上传",
            "memory",
            "remember",
            "记忆",
            "plan my work",
            "plan tomorrow",
            "create calendar",
            "delete calendar",
            "create event",
            "delete event",
            "发邮件",
            "创建日历",
            "删除日历",
            "创建日程",
            "删除日程",
            "work planning",
            "规划工作",
            "安排工作",
        )
    ):
        return {
            "intent": "explain_capabilities",
            "confidence": 1.0,
            "reason_code": "unsupported_tool_request",
        }
    if any(
        token in normalized
        for token in ("show today's brief", "show latest brief", "查看今日简报", "显示今日简报")
    ):
        return {
            "intent": "show_latest_brief",
            "confidence": 1.0,
            "reason_code": "deterministic_show_latest",
        }
    if any(
        token in normalized
        for token in (
            "summarize today's mail",
            "generate daily brief",
            "generate today's brief",
            "refresh today's brief",
            "生成今日简报",
            "刷新今日简报",
            "总结今天邮件",
        )
    ):
        return {
            "intent": "generate_daily_brief",
            "confidence": 1.0,
            "reason_code": "deterministic_generate",
        }
    return {"intent": "explain_capabilities", "confidence": 0.5, "reason_code": "ambiguous_request"}


async def classify_ambiguous_conversation_intent(
    text: str,
    *,
    model_gateway: Any,
    model_name: str,
    configured_patterns: tuple[str, ...] = (),
    invocation_metadata: list[dict[str, Any]] | None = None,
) -> ConversationIntent:
    """本地脱敏歧义文本后执行受限的三值意图模型分类。

    确定性 allow/deny 规则仍优先；模型或输出校验失败时安全解释能力边界。
    """
    deterministic = classify_conversation_intent(text)
    if deterministic["reason_code"] != "ambiguous_request":
        return ConversationIntent.model_validate(deterministic)
    redacted = redact_for_model(text, configured_patterns=configured_patterns)
    try:
        response = await model_gateway.complete(
            model_name=model_name,
            prompt_version="conversation_intent_v1",
            messages=[{"role": "user", "content": redacted.text}],
            response_model=ConversationIntent,
        )
        if invocation_metadata is not None:
            invocation_metadata.append(
                {
                    "provider": type(model_gateway).__name__.removesuffix("Gateway").lower(),
                    "model_name": model_name,
                    "prompt_version": "conversation_intent_v1",
                    "input_hash": sha256(redacted.text.encode("utf-8")).hexdigest(),
                    "output_schema": "ConversationIntent",
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "estimated_cost_microusd": response.usage.estimated_cost_microusd,
                    "latency_ms": response.usage.latency_ms,
                    "status": "succeeded",
                    "error_code": None,
                }
            )
        return ConversationIntent.model_validate(response.value)
    except (RuntimeError, ValueError, TypeError, KeyError) as error:
        if invocation_metadata is not None:
            invocation_metadata.append(
                {
                    "provider": type(model_gateway).__name__.removesuffix("Gateway").lower(),
                    "model_name": model_name,
                    "prompt_version": "conversation_intent_v1",
                    "input_hash": sha256(redacted.text.encode("utf-8")).hexdigest(),
                    "output_schema": "ConversationIntent",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_cost_microusd": None,
                    "latency_ms": 0,
                    "status": "failed",
                    "error_code": getattr(error, "code", "conversation_intent_failed"),
                }
            )
        return ConversationIntent(
            intent="explain_capabilities", confidence=0.0, reason_code="intent_model_failed"
        )


def detect_calendar_conflicts_node(state: dict[str, Any]) -> dict[str, Any]:
    """使用确定性日历规则检测冲突，不向模型发送日历内容。"""
    _event(state, "detect_calendar_conflicts", "started")
    events = []
    for raw in state.get("calendar_events", []):
        try:
            start_at = raw.get("start_at", raw.get("start"))
            end_at = raw.get("end_at", raw.get("end"))
            if isinstance(start_at, str):
                start_at = datetime.fromisoformat(start_at)
            if isinstance(end_at, str):
                end_at = datetime.fromisoformat(end_at)
            events.append(
                CalendarEvent(
                    event_id=raw["event_id"],
                    start_at=start_at,
                    end_at=end_at,
                    status=raw.get("status", "confirmed"),
                    transparency=raw.get("transparency", "opaque"),
                    all_day=raw.get("all_day", False),
                )
            )
        except (KeyError, TypeError, ValueError):
            state["warnings"].append("invalid_calendar_event")
    state["conflicts"] = [
        {"event_ids": [pair[0].event_id, pair[1].event_id]} for pair in find_conflicts(events)
    ]
    _event(state, "detect_calendar_conflicts", "completed")
    return state


def compose_structured_brief(state: dict[str, Any]) -> dict[str, Any]:
    """把确定性结果组装为带来源引用的简报。"""
    _event(state, "compose_structured_brief", "started")
    items = []
    thread_urls = {
        str(thread.get("thread_id", "")): thread.get("provider_url")
        for thread in state.get("mail_threads", [])
    }
    event_urls = {
        str(event.get("event_id", "")): event.get("provider_url")
        for event in state.get("calendar_events", [])
    }
    for item in state.get("classifications", []) + state.get("model_items", []):
        if item.get("category") == "notification":
            continue
        items.append(
            BriefItem(
                section=BriefSection.MAIL_SUMMARY,
                title=f"邮件线程 {item['thread_id']}",
                body_markdown="已完成分类。",
                source_refs=[
                    BriefSourceRef(source_type="email_thread", source_id=item["thread_id"], provider_url=thread_urls.get(str(item["thread_id"])))
                ],
                priority=BriefPriority.HIGH
                if item.get("urgency") == "urgent"
                else BriefPriority.NORMAL,
            ).model_dump()
        )
    notification_refs = [
        BriefSourceRef(source_type="email_thread", source_id=item["thread_id"], provider_url=thread_urls.get(str(item["thread_id"])))
        for item in state.get("classifications", []) + state.get("model_items", [])
        if item.get("category") == "notification"
    ]
    if notification_refs:
        items.append(
            BriefItem(
                section=BriefSection.NOTIFICATIONS,
                title=f"{len(notification_refs)} 条通知",
                body_markdown="通知已折叠汇总。",
                source_refs=notification_refs,
            ).model_dump()
        )
    for raw in state.get("calendar_events", []):
        event_id = raw.get("event_id")
        if event_id:
            items.append(
                BriefItem(
                    section=BriefSection.SCHEDULE,
                    title="日程安排",
                    body_markdown="已同步日程。",
                    source_refs=[BriefSourceRef(source_type="calendar_event", source_id=event_id, provider_url=event_urls.get(str(event_id)))],
                ).model_dump()
            )
    for conflict in state.get("conflicts", []):
        refs = [
            BriefSourceRef(source_type="calendar_event", source_id=event_id, provider_url=event_urls.get(str(event_id)))
            for event_id in conflict["event_ids"]
        ]
        items.append(
            BriefItem(
                section=BriefSection.CONFLICTS,
                title="日程冲突",
                body_markdown="两个忙碌日程发生重叠。",
                priority=BriefPriority.HIGH,
                source_refs=refs,
            ).model_dump()
        )
    state["deterministic_items"] = items
    _event(state, "compose_structured_brief", "completed")
    return state


def validate_and_render(state: dict[str, Any]) -> dict[str, Any]:
    """最终验证并在模型失败时保留部分确定性简报。"""
    _event(state, "validate_and_render", "started")
    if not state.get("mail_threads") and not state.get("calendar_events"):
        raise DailyBriefSourceFailure("daily_brief_no_usable_sources")
    content = DailyBriefContent(
        local_date=state.get("local_date", datetime.now(UTC).date()),
        source_cutoff=state.get("source_cutoff", datetime.now(UTC)),
        completeness="partial" if state.get("warnings") else "complete",
        headline="今日办公简报",
        items=[BriefItem.model_validate(i) for i in state.get("deterministic_items", [])],
        warnings=state.get("warnings", []),
    )
    state["content"] = content.model_dump(mode="json")
    _event(state, "validate_and_render", "completed")
    return state
