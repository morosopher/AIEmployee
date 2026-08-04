"""约束数据库角色脚本的最小权限与密码进程边界。"""

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_retention_role_script_uses_stdin_for_password_and_grants_privacy_worker_tables() -> None:
    """保留角色需覆盖删除器实际表，但密码绝不可通过 ``psql`` argv 传递。"""
    script = (REPOSITORY_ROOT / "scripts" / "init-db-roles.sh").read_text(encoding="utf-8")

    assert "--set=app_password=" not in script
    assert "--set=retention_password=" not in script
    assert "read -r app_password" in script
    assert "read -r retention_password" in script
    # 删除器每批先读取主键或谓词列再执行 DELETE；只授予 DELETE 会在真实角色下失败。
    for table_name in (
        "messages",
        "conversations",
        "daily_brief_items",
        "daily_briefs",
        "llm_invocations",
        "approval_requests",
        "tool_executions",
        "task_steps",
        "outbox_events",
        "task_runs",
        "user_sessions",
        "oauth_connections",
    ):
        assert table_name in script
    assert "GRANT SELECT, DELETE ON messages, conversations, daily_brief_items, daily_briefs," in script
    assert "REVOKE UPDATE ON email_messages, users FROM ai_employee_retention;" in script
    assert (
        "GRANT UPDATE (body_ciphertext, body_nonce, body_key_version) ON email_messages "
        "TO ai_employee_retention;"
    ) in script
    assert re.search(
        r"GRANT UPDATE \(email, display_name, password_hash, is_active, email_body_retention_days,\s+"
        r"source_metadata_retention_days, workspace_history_retention_days\) ON users "
        r"TO ai_employee_retention;",
        script,
    ) is not None
    assert "GRANT UPDATE ON users TO ai_employee_retention;" not in script
    assert "REVOKE UPDATE ON sync_cursors FROM ai_employee_retention;" in script
    assert (
        "GRANT UPDATE (cursor, last_success_at, last_attempt_at, last_error_code) ON sync_cursors "
        "TO ai_employee_retention;"
    ) in script
    assert "GRANT UPDATE ON sync_cursors TO ai_employee_retention;" not in script
    assert "GRANT SELECT, DELETE ON email_analyses, email_threads, calendar_events," in script
    for table_name in ("email_analyses", "email_threads", "calendar_events", "encrypted_credentials"):
        assert re.search(
            rf"GRANT\\s+[^;]*UPDATE[^;]*\\b{table_name}\\b[^;]*ai_employee_retention",
            script,
            flags=re.DOTALL,
        ) is None
    assert "GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ai_employee_app, ai_employee_retention;" in script
    assert "GRANT USAGE, SELECT ON ALL SEQUENCES" not in script
    assert "REVOKE UPDATE, DELETE ON audit_events FROM ai_employee_app" in script
    assert "GRANT SELECT, INSERT, DELETE ON audit_events TO ai_employee_retention" in script
