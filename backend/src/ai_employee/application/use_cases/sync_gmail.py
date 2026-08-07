"""保留 M2 迁移期间 Gmail 同步用例名称的显式兼容再导出。"""

from ai_employee.application.use_cases.sync_mail import (
    MailConnectionNotFoundError,
    MailSyncResult,
    MailSyncStore,
    MailSyncStoreFactory,
    SyncMailUseCase,
)

GmailConnectionNotFoundError = MailConnectionNotFoundError
GmailSyncResult = MailSyncResult
GmailSyncStore = MailSyncStore
GmailSyncStoreFactory = MailSyncStoreFactory
SyncGmailUseCase = SyncMailUseCase

__all__ = [
    "GmailConnectionNotFoundError",
    "GmailSyncResult",
    "GmailSyncStore",
    "GmailSyncStoreFactory",
    "SyncGmailUseCase",
]
