"""每日简报 LangGraph 跨节点状态定义。"""

from typing import Any, TypedDict


class DailyBriefState(TypedDict, total=False):
    task_run_id: str
    local_date: str
    source_cutoff: str
    mail_threads: list[dict[str, Any]]
    calendar_events: list[dict[str, Any]]
    # 这些输入必须显式声明在 LangGraph 状态契约中，否则入口会过滤掉工作域配置，
    # 造成确定性分类失效；它们均为可安全 checkpoint 的合成/配置值。
    work_email_domains: list[str]
    locale: str
    model_name: str
    classifications: list[dict[str, Any]]
    spam_thread_ids: set[str]
    # 只保存入口真实存在的非空线程 ID；模型不能新增可点击的回复来源。
    replyable_thread_ids: set[str]
    conflicts: list[dict[str, Any]]
    deterministic_items: list[dict[str, Any]]
    model_items: list[dict[str, Any]]
    model_invocations: list[dict[str, Any]]
    content: dict[str, Any]
    warnings: list[str]
    step_events: list[dict[str, Any]]
    task_step_event_sink: Any
    model_redaction_patterns: list[str]
