"""验证身份 ORM 模型在真实 PostgreSQL 中的持久化行为。"""

from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel
from ai_employee.infrastructure.db.session import build_session_factory


def _assert_utc(value: datetime) -> None:
    """验证 PostgreSQL 返回的时间戳带 UTC 偏移，避免把 naive 时间传入业务层。"""
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_user_round_trip(database_url: str) -> None:
    """验证用户 UUID、Python 默认值和数据库时间戳可在事务间无损往返。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email="owner@example.com",
                display_name="Owner",
                timezone="Asia/Shanghai",
                brief_time=time(8, 0),
            )
            session.add(user)

        assert isinstance(user.id, UUID)
        _assert_utc(user.created_at)
        _assert_utc(user.updated_at)
        assert user.locale == "zh-CN"
        assert user.is_active is True
        async with session_factory() as session:
            saved = await session.scalar(select(UserModel))
            assert saved is not None
            assert saved.email == "owner@example.com"
            assert ZoneInfo(saved.timezone).key == "Asia/Shanghai"
            assert isinstance(saved.id, UUID)
            _assert_utc(saved.created_at)
            _assert_utc(saved.updated_at)
            assert saved.locale == "zh-CN"
            assert saved.is_active is True
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_session_factory_can_be_explicitly_disposed(database_url: str) -> None:
    """调用方应能显式释放工厂拥有的连接池，而不访问内部 ``kw``。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            assert (await session.scalar(text("SELECT 1"))) == 1
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_user_session_round_trip_returns_uuid_and_utc_timestamps(database_url: str) -> None:
    """验证会话摘要、UUID 和生命周期时间字段在真实 PostgreSQL 中保持类型与时区。"""
    session_factory = build_session_factory(database_url)
    created_at = datetime(2030, 1, 1, 0, 0, tzinfo=UTC)
    expires_at = datetime(2030, 1, 2, 0, 0, tzinfo=UTC)
    last_seen_at = datetime(2030, 1, 1, 1, 0, tzinfo=UTC)
    token_hash = b"t" * 32
    csrf_hash = b"c" * 32
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email="session-owner@example.com",
                display_name="Session Owner",
                timezone="UTC",
                brief_time=time(9, 0),
            )
            session.add(user)
            await session.flush()
            saved_session = UserSessionModel(
                user_id=user.id,
                token_hash=token_hash,
                csrf_hash=csrf_hash,
                created_at=created_at,
                expires_at=expires_at,
                last_seen_at=last_seen_at,
            )
            session.add(saved_session)

        assert isinstance(saved_session.id, UUID)
        async with session_factory() as session:
            loaded = await session.scalar(select(UserSessionModel))
            assert loaded is not None
            assert isinstance(loaded.id, UUID)
            assert loaded.user_id == user.id
            assert loaded.token_hash == token_hash
            assert loaded.csrf_hash == csrf_hash
            _assert_utc(loaded.created_at)
            _assert_utc(loaded.expires_at)
            _assert_utc(loaded.last_seen_at)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_duplicate_token_hash_is_rejected(database_url: str) -> None:
    """会话令牌摘要必须由数据库唯一约束去重。"""
    session_factory = build_session_factory(database_url)
    timestamp = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email="duplicate-token-owner@example.com",
                display_name="Duplicate Token Owner",
                timezone="UTC",
                brief_time=time(9, 0),
            )
            session.add(user)
            await session.flush()
            session.add_all(
                (
                    UserSessionModel(
                        user_id=user.id,
                        token_hash=b"d" * 32,
                        csrf_hash=b"a" * 32,
                        created_at=timestamp,
                        expires_at=timestamp,
                        last_seen_at=timestamp,
                    ),
                    UserSessionModel(
                        user_id=user.id,
                        token_hash=b"d" * 32,
                        csrf_hash=b"b" * 32,
                        created_at=timestamp,
                        expires_at=timestamp,
                        last_seen_at=timestamp,
                    ),
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_token_hash_must_be_exactly_32_bytes(database_url: str) -> None:
    """数据库必须拒绝长度不是 32 字节的会话令牌摘要。"""
    session_factory = build_session_factory(database_url)
    timestamp = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email="short-token-owner@example.com",
                display_name="Short Token Owner",
                timezone="UTC",
                brief_time=time(9, 0),
            )
            session.add(user)
            await session.flush()
            session.add(
                UserSessionModel(
                    user_id=user.id,
                    token_hash=b"s" * 31,
                    csrf_hash=b"c" * 32,
                    created_at=timestamp,
                    expires_at=timestamp,
                    last_seen_at=timestamp,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_csrf_hash_must_be_exactly_32_bytes(database_url: str) -> None:
    """数据库必须拒绝长度不是 32 字节的 CSRF 摘要。"""
    session_factory = build_session_factory(database_url)
    timestamp = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email="short-csrf-owner@example.com",
                display_name="Short CSRF Owner",
                timezone="UTC",
                brief_time=time(9, 0),
            )
            session.add(user)
            await session.flush()
            session.add(
                UserSessionModel(
                    user_id=user.id,
                    token_hash=b"t" * 32,
                    csrf_hash=b"s" * 31,
                    created_at=timestamp,
                    expires_at=timestamp,
                    last_seen_at=timestamp,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_deleting_user_cascades_to_sessions(database_url: str) -> None:
    """删除用户时 PostgreSQL 外键应原子级联删除其会话记录。"""
    session_factory = build_session_factory(database_url)
    timestamp = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email="cascade-owner@example.com",
                display_name="Cascade Owner",
                timezone="UTC",
                brief_time=time(9, 0),
            )
            session.add(user)
            await session.flush()
            session.add(
                UserSessionModel(
                    user_id=user.id,
                    token_hash=b"e" * 32,
                    csrf_hash=b"f" * 32,
                    created_at=timestamp,
                    expires_at=timestamp,
                    last_seen_at=timestamp,
                )
            )

        async with session_factory.begin() as session:
            await session.execute(delete(UserModel).where(UserModel.id == user.id))

        async with session_factory() as session:
            session_count = await session.scalar(select(func.count()).select_from(UserSessionModel))
            assert session_count == 0
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_updated_at_is_eagerly_available_after_update(database_url: str) -> None:
    """更新提交后 updated_at 不得过期，否则 async ORM 会触发隐藏的同步属性 I/O。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email="updated-at-owner@example.com",
                display_name="Before",
                timezone="UTC",
                brief_time=time(9, 0),
            )
            session.add(user)

        async with session_factory() as session:
            saved = await session.scalar(
                select(UserModel).where(UserModel.email == "updated-at-owner@example.com")
            )
            assert saved is not None
            before = saved.updated_at
            saved.display_name = "After"
            await session.commit()

            assert "updated_at" not in inspect(saved).expired_attributes
            _assert_utc(saved.updated_at)
            assert saved.updated_at > before
    finally:
        await session_factory.dispose()
