"""验证身份 ORM 模型在真实 PostgreSQL 中的持久化行为。"""

from datetime import time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.session import build_session_factory


@pytest.mark.asyncio
async def test_user_round_trip(database_url: str) -> None:
    """验证用户核心身份字段可在独立事务间无损写入并读取。"""
    session_factory = build_session_factory(database_url)
    async with session_factory.begin() as session:
        session.add(
            UserModel(
                email="owner@example.com",
                display_name="Owner",
                timezone="Asia/Shanghai",
                locale="zh-CN",
                brief_time=time(8, 0),
            )
        )

    async with session_factory() as session:
        saved = await session.scalar(select(UserModel))
        assert saved is not None
        assert saved.email == "owner@example.com"
        assert ZoneInfo(saved.timezone).key == "Asia/Shanghai"
