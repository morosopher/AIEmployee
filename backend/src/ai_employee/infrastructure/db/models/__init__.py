"""导出并注册基础设施层的 ORM 模型。"""

from ai_employee.infrastructure.db.models.briefs import (
    ConversationModel,
    DailyBriefItemModel,
    DailyBriefModel,
    LLMInvocationModel,
    MessageModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    EmailAnalysisModel,
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)

__all__ = [
    "ApprovalRequestModel",
    "AuditEventModel",
    "CalendarEventModel",
    "ConnectionCapabilityModel",
    "ConversationModel",
    "DailyBriefItemModel",
    "DailyBriefModel",
    "EmailAnalysisModel",
    "EmailMessageModel",
    "EmailThreadModel",
    "EncryptedCredentialModel",
    "LLMInvocationModel",
    "MessageModel",
    "OAuthAttemptModel",
    "OAuthConnectionModel",
    "OutboxEventModel",
    "ProviderCalendarModel",
    "SyncCursorModel",
    "TaskRunModel",
    "TaskStepModel",
    "ToolExecutionModel",
    "UserModel",
    "UserSessionModel",
]
