"""导出并注册基础设施层的 ORM 模型。"""

from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    EmailAnalysisModel,
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
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
    "EmailAnalysisModel",
    "EmailMessageModel",
    "EmailThreadModel",
    "EncryptedCredentialModel",
    "OAuthAttemptModel",
    "OAuthConnectionModel",
    "OutboxEventModel",
    "SyncCursorModel",
    "TaskRunModel",
    "TaskStepModel",
    "ToolExecutionModel",
    "UserModel",
    "UserSessionModel",
]
