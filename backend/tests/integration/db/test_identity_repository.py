"""在真实 PostgreSQL 上验证身份 Repository 的并发与用户隔离语义。"""

import asyncio
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel
from ai_employee.infrastructure.db.repositories.identity import (
    SqlAlchemyIdentityRepositoryFactory,
)
from ai_employee.infrastructure.db.session import build_session_factory


async def _update_last_seen_after_barrier(
    repositories: SqlAlchemyIdentityRepositoryFactory,
    barrier: asyncio.Barrier,
    *,
    user_id: UUID,
    session_id: UUID,
    target_time: datetime,
    due_before_or_at: datetime,
) -> bool:
    """在独立事务已取得连接后等待同伴，再尝试一次条件更新时间。

    Args:
        repositories: 只供当前协程使用的 Repository factory。
        barrier: 保证两个独立事务都已开始后再同时执行更新。
        user_id: 会话所属的合成用户 ID。
        session_id: 两个事务竞争更新的同一会话 ID。
        target_time: 成功事务应写入的精确 UTC 时间。
        due_before_or_at: 允许更新的旧 ``last_seen_at`` 上界。

    Returns:
        当前事务是否成功跨过条件 UPDATE 的时间与用户约束。
    """
    async with repositories() as repository:
        # 先执行真实查询，确保两个 context 都已取得不同 AsyncSession 的数据库连接和事务。
        await repository.list_active_sessions(user_id=user_id, now=target_time)
        await barrier.wait()
        return await repository.update_last_seen_if_due(
            user_id=user_id,
            session_id=session_id,
            last_seen_at=target_time,
            due_before_or_at=due_before_or_at,
        )


@pytest.mark.asyncio
async def test_concurrent_last_seen_update_is_atomic_and_user_scoped(
    database_url: str,
) -> None:
    """两个真实事务竞争到期会话时只能一个成功，且错误用户条件不能修改结果。"""
    session_factory = build_session_factory(database_url)
    due_before_or_at = datetime(2030, 1, 1, 8, 0, tzinfo=UTC)
    target_time = due_before_or_at + timedelta(minutes=5)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email="last-seen-owner@example.com",
                display_name="Last Seen Owner",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            saved_session = UserSessionModel(
                user_id=user.id,
                token_hash=b"t" * 32,
                csrf_hash=b"c" * 32,
                created_at=due_before_or_at - timedelta(hours=1),
                expires_at=target_time + timedelta(hours=1),
                last_seen_at=due_before_or_at,
            )
            session.add(saved_session)
            await session.flush()
            user_id = user.id
            session_id = saved_session.id

        barrier = asyncio.Barrier(2)
        results = await asyncio.gather(
            _update_last_seen_after_barrier(
                SqlAlchemyIdentityRepositoryFactory(session_factory),
                barrier,
                user_id=user_id,
                session_id=session_id,
                target_time=target_time,
                due_before_or_at=due_before_or_at,
            ),
            _update_last_seen_after_barrier(
                SqlAlchemyIdentityRepositoryFactory(session_factory),
                barrier,
                user_id=user_id,
                session_id=session_id,
                target_time=target_time,
                due_before_or_at=due_before_or_at,
            ),
        )

        assert sorted(results) == [False, True]

        async with SqlAlchemyIdentityRepositoryFactory(session_factory)() as repository:
            wrong_user_updated = await repository.update_last_seen_if_due(
                user_id=uuid4(),
                session_id=session_id,
                last_seen_at=target_time + timedelta(minutes=1),
                due_before_or_at=target_time,
            )
        assert not wrong_user_updated

        async with session_factory() as session:
            final_session = await session.scalar(
                select(UserSessionModel).where(
                    UserSessionModel.id == session_id,
                    UserSessionModel.user_id == user_id,
                )
            )
            assert final_session is not None
            assert final_session.user_id == user_id
            assert final_session.last_seen_at == target_time
    finally:
        await session_factory.dispose()
