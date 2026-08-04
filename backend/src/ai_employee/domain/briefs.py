"""每日简报和对话意图的稳定、可审计数据模型。"""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from ai_employee.domain.email import EmailCategory


class BriefSection(StrEnum):
    ATTENTION = "attention"
    NEEDS_REPLY = "needs_reply"
    MAIL_SUMMARY = "mail_summary"
    NOTIFICATIONS = "notifications"
    SCHEDULE = "schedule"
    CONFLICTS = "conflicts"
    SUGGESTED_ACTIONS = "suggested_actions"


class BriefPriority(StrEnum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class EmailJudgement(BaseModel):
    """模型对单个邮件线程的结构化判断。"""

    thread_id: str
    category: EmailCategory
    urgency: str = "normal"
    needs_reply: bool = False
    deadline_at: datetime | None = None
    confidence: float = Field(ge=0, le=1)
    reason_codes: list[str] = Field(default_factory=list)

    @field_validator("urgency")
    @classmethod
    def valid_urgency(cls, value: str) -> str:
        if value not in {"urgent", "normal"}:
            raise ValueError("urgency must be urgent or normal")
        return value


class BriefSourceRef(BaseModel):
    """简报项目的可追溯来源引用。"""

    source_type: str
    source_id: str
    provider_url: str | None = None


class BriefItem(BaseModel):
    """渲染到界面的单个简报条目，必须带来源。"""

    section: BriefSection
    title: str
    body_markdown: str
    priority: BriefPriority = BriefPriority.NORMAL
    source_refs: list[BriefSourceRef] = Field(min_length=1)
    suggested_action_kind: str | None = None

    @field_validator("suggested_action_kind")
    @classmethod
    def safe_action(cls, value: str | None) -> str | None:
        if value is not None and value.lower() in {
            "send_email",
            "modify_calendar",
            "execute",
            "auto_execute",
        }:
            raise ValueError("suggested actions cannot imply automatic execution")
        return value


class DailyBriefContent(BaseModel):
    """每日简报完整内容及完整性状态。"""

    local_date: date
    source_cutoff: datetime
    completeness: str
    headline: str
    items: list[BriefItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("completeness")
    @classmethod
    def valid_completeness(cls, value: str) -> str:
        if value not in {"complete", "partial"}:
            raise ValueError("completeness must be complete or partial")
        return value


class ConversationIntent(BaseModel):
    """会话意图窄分类，避免把聊天请求扩张成任意工具调用。"""

    intent: str
    confidence: float = Field(ge=0, le=1)
    reason_code: str

    @field_validator("intent")
    @classmethod
    def valid_intent(cls, value: str) -> str:
        allowed = {"generate_daily_brief", "show_latest_brief", "explain_capabilities"}
        if value not in allowed:
            raise ValueError("unsupported conversation intent")
        return value
