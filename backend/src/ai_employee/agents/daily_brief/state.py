"""每日简报 LangGraph 跨节点状态定义。"""

from typing import Any, TypedDict


class DailyBriefState(TypedDict, total=False):
    task_run_id: str
    local_date: str
    source_cutoff: str
    mail_threads: list[dict[str, Any]]
    calendar_events: list[dict[str, Any]]
    classifications: list[dict[str, Any]]
    spam_thread_ids: set[str]
    conflicts: list[dict[str, Any]]
    deterministic_items: list[dict[str, Any]]
    model_items: list[dict[str, Any]]
    content: dict[str, Any]
    warnings: list[str]
    step_events: list[dict[str, Any]]
    model_gateway: Any
    task_step_event_sink: Any
    model_redaction_patterns: list[str]
