"""保留 legacy ``sync_gmail`` Worker 导入的显式兼容再导出。"""

from ai_employee.workers.sync_mail import MailSyncTaskStep, build_mail_sync_task_step

GmailSyncTaskStep = MailSyncTaskStep
build_gmail_sync_task_step = build_mail_sync_task_step

__all__ = ["GmailSyncTaskStep", "build_gmail_sync_task_step"]
