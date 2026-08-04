"""验证用户设置的 PostgreSQL 命名约束。"""

from datetime import time

import pytest
from sqlalchemy.exc import IntegrityError

from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    (
        ("email_body_retention_days", 0, "ck_users_email_body_retention_days"),
        ("source_metadata_retention_days", 3651, "ck_users_source_metadata_retention_days"),
        ("workspace_history_retention_days", 0, "ck_users_workspace_history_retention_days"),
    ),
)
async def test_each_retention_constraint_rejects_invalid_value(
    database_url: str, field: str, value: int, constraint: str
) -> None:
    """每一项保留期都必须由数据库独立兜底，不能只依赖 API 校验。"""
    session_factory: ManagedAsyncSessionMaker = build_session_factory(database_url)
    values = {
        "email_body_retention_days": 30,
        "source_metadata_retention_days": 180,
        "workspace_history_retention_days": 365,
        field: value,
    }
    try:
        with pytest.raises(IntegrityError, match=constraint):
            async with session_factory.begin() as session:
                session.add(
                    UserModel(
                        email=f"invalid-{field}@example.test",
                        display_name="Invalid setting",
                        password_hash=None,
                        timezone="UTC",
                        locale="en-US",
                        brief_time=time(8, 0),
                        is_active=True,
                        **values,
                    )
                )
    finally:
        await session_factory.dispose()
