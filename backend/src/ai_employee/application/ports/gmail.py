"""保留 M2 迁移期间 Gmail 命名端口的显式兼容再导出。"""

from ai_employee.application.ports.mail import MailConnectionState as GmailConnectionState
from ai_employee.application.ports.mail import (
    MailCursorExpiredError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.application.ports.mail import MailMessage as GmailMessage
from ai_employee.application.ports.mail import MailReader as GmailReader
from ai_employee.application.ports.mail import MailSyncPage as GmailSyncPage

HistoryCursorExpiredError = MailCursorExpiredError

__all__ = [
    "GmailConnectionState",
    "GmailMessage",
    "GmailReader",
    "GmailSyncPage",
    "HistoryCursorExpiredError",
    "TransientProviderError",
    "UserActionRequiredError",
]
