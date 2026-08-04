"""每日简报图节点：确定性规则先行，模型只处理脱敏歧义事实。"""

from datetime import UTC, datetime
from typing import Any

from ai_employee.domain.briefs import (
    BriefItem,
    BriefPriority,
    BriefSection,
    BriefSourceRef,
    DailyBriefContent,
)
from ai_employee.domain.calendar import CalendarEvent, find_conflicts
from ai_employee.domain.email import (
    EmailCategory,
    EmailMessage,
    classify_by_rules,
    classify_urgency_by_rules,
)


class DailyBriefSourceFailure(RuntimeError):
    """所有源均不可用时阻止写入空的成功简报。"""


def _event(state: dict[str, Any], name: str, status: str) -> None:
    state.setdefault("step_events", []).append({"step": name, "status": status})


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
            continue
        if classification:
            result.append(
                {
                    "thread_id": thread.get("thread_id", ""),
                    "category": classification.category.value,
                    "urgency": urgency.urgency.value,
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
    if gateway:
        for thread in state.get("mail_threads", []):
            thread_id = thread.get("thread_id", "")
            if (
                thread_id in classified
                or thread.get("labels", [])
                and "SPAM" in thread.get("labels", [])
            ):
                continue
            try:
                response = await gateway.complete(
                    model_name=state.get("model_name", ""),
                    prompt_version="daily_brief_v1",
                    messages=[{"role": "user", "content": thread_id}],
                    response_model=__import__(
                        "ai_employee.domain.briefs", fromlist=["EmailJudgement"]
                    ).EmailJudgement,
                )
                outputs.append(response.value.model_dump())
            except (RuntimeError, ValueError, TypeError, KeyError):
                # 只允许一次明确修复请求；第二次失败保留确定性结果并标记 partial。
                try:
                    response = await gateway.complete(
                        model_name=state.get("model_name", ""),
                        prompt_version="daily_brief_v1",
                        messages=[
                            {"role": "user", "content": thread_id},
                            {"role": "user", "content": "请修复为严格 JSON"},
                        ],
                        response_model=__import__(
                            "ai_employee.domain.briefs", fromlist=["EmailJudgement"]
                        ).EmailJudgement,
                    )
                    outputs.append(response.value.model_dump())
                except (RuntimeError, ValueError, TypeError, KeyError):
                    state["warnings"].append(f"model_classification_failed:{thread_id}")
    state["model_items"] = outputs
    _event(state, "classify_ambiguous_threads", "completed")
    return state


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
            "发送邮件",
            "modify calendar",
            "修改日历",
            "web search",
            "网页搜索",
            "upload",
            "上传",
            "memory",
            "记忆",
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
    for item in state.get("classifications", []) + state.get("model_items", []):
        items.append(
            BriefItem(
                section=BriefSection.MAIL_SUMMARY,
                title=f"邮件线程 {item['thread_id']}",
                body_markdown="已完成分类。",
                source_refs=[
                    BriefSourceRef(source_type="email_thread", source_id=item["thread_id"])
                ],
                priority=BriefPriority.HIGH
                if item.get("urgency") == "urgent"
                else BriefPriority.NORMAL,
            ).model_dump()
        )
    notification_refs = [
        BriefSourceRef(source_type="email_thread", source_id=item["thread_id"])
        for item in state.get("classifications", [])
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
                    source_refs=[BriefSourceRef(source_type="calendar_event", source_id=event_id)],
                ).model_dump()
            )
    for conflict in state.get("conflicts", []):
        refs = [
            BriefSourceRef(source_type="calendar_event", source_id=event_id)
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
