"""把认证应用端口映射到现有 SQLAlchemy 身份模型与显式事务。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.auth import IdentityRepository
from ai_employee.domain.identity import (
    NewAdmin,
    SessionAuthenticationRecord,
    SessionRecord,
    SessionSummary,
    UserCredential,
    UserIdentity,
)
from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


def _to_user_credential(model: UserModel) -> UserCredential:
    """把 ORM 用户收窄为不携带基础设施类型的认证领域对象。"""
    return UserCredential(
        identity=UserIdentity(
            id=model.id,
            email=model.email,
            display_name=model.display_name,
            timezone=model.timezone,
            locale=model.locale,
            brief_time=model.brief_time,
        ),
        password_hash=model.password_hash,
        is_active=model.is_active,
    )


def _to_session_record(model: UserSessionModel) -> SessionRecord:
    """把 ORM 会话转换为不含原始令牌的领域快照。"""
    return SessionRecord(
        id=model.id,
        user_id=model.user_id,
        csrf_hash=model.csrf_hash,
        created_at=model.created_at,
        expires_at=model.expires_at,
        last_seen_at=model.last_seen_at,
        revoked_at=model.revoked_at,
    )


class SqlAlchemyIdentityRepository:
    """在调用方事务内实现认证所需的最小身份持久化操作。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定一个由 Repository factory 管理生命周期的异步会话。"""
        self._session = session

    async def get_active_user_by_email(self, email: str) -> UserCredential | None:
        """按规范化邮箱和活动状态读取唯一管理员凭据。"""
        model = await self._session.scalar(
            select(UserModel).where(UserModel.email == email, UserModel.is_active.is_(True))
        )
        return None if model is None else _to_user_credential(model)

    async def create_session(
        self,
        *,
        user_id: UUID,
        token_hash: bytes,
        csrf_hash: bytes,
        created_at: datetime,
        expires_at: datetime,
    ) -> SessionRecord:
        """插入仅保存两个 32 字节摘要的新会话并刷新应用生成的 UUID。"""
        model = UserSessionModel(
            user_id=user_id,
            token_hash=token_hash,
            csrf_hash=csrf_hash,
            created_at=created_at,
            expires_at=expires_at,
            last_seen_at=created_at,
        )
        self._session.add(model)
        await self._session.flush()
        return _to_session_record(model)

    async def get_session_authentication(
        self, token_hash: bytes
    ) -> SessionAuthenticationRecord | None:
        """按唯一会话摘要联结用户；活动/过期判断由应用用例统一执行。"""
        row = (
            await self._session.execute(
                select(UserSessionModel, UserModel)
                .join(UserModel, UserModel.id == UserSessionModel.user_id)
                .where(UserSessionModel.token_hash == token_hash)
            )
        ).one_or_none()
        if row is None:
            return None
        session_model, user_model = row
        return SessionAuthenticationRecord(
            user=_to_user_credential(user_model),
            session=_to_session_record(session_model),
        )

    async def update_last_seen_if_due(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        last_seen_at: datetime,
        due_before_or_at: datetime,
    ) -> bool:
        """以单条条件 UPDATE 原子节流最近访问写入，并显式注入用户条件。"""
        updated_session_id = await self._session.scalar(
            update(UserSessionModel)
            .where(
                UserSessionModel.id == session_id,
                UserSessionModel.user_id == user_id,
                UserSessionModel.revoked_at.is_(None),
                UserSessionModel.expires_at > last_seen_at,
                UserSessionModel.last_seen_at <= due_before_or_at,
            )
            .values(last_seen_at=last_seen_at)
            .returning(UserSessionModel.id)
        )
        return updated_session_id is not None

    async def list_active_sessions(
        self, *, user_id: UUID, now: datetime
    ) -> tuple[SessionSummary, ...]:
        """只列指定用户未撤销且严格未过期的会话，按创建时间倒序返回。"""
        models = (
            await self._session.scalars(
                select(UserSessionModel)
                .where(
                    UserSessionModel.user_id == user_id,
                    UserSessionModel.revoked_at.is_(None),
                    UserSessionModel.expires_at > now,
                )
                .order_by(UserSessionModel.created_at.desc(), UserSessionModel.id.desc())
            )
        ).all()
        return tuple(
            SessionSummary(
                id=model.id,
                created_at=model.created_at,
                expires_at=model.expires_at,
                last_seen_at=model.last_seen_at,
            )
            for model in models
        )

    async def revoke_active_session(
        self, *, user_id: UUID, session_id: UUID, revoked_at: datetime
    ) -> bool:
        """锁定并撤销本人活动会话；跨用户、过期或已撤销统一返回 ``False``。"""
        model = await self._session.scalar(
            select(UserSessionModel)
            .where(
                UserSessionModel.id == session_id,
                UserSessionModel.user_id == user_id,
                UserSessionModel.revoked_at.is_(None),
                UserSessionModel.expires_at > revoked_at,
            )
            .with_for_update()
        )
        if model is None:
            return False
        model.revoked_at = revoked_at
        return True

    async def get_admin_for_creation(self) -> UserCredential | None:
        """取得事务级 advisory lock 后读取现有管理员，串行化空表并发创建。"""
        # 行锁无法锁住“尚不存在的行”，固定 advisory lock 才能让不同邮箱共享同一创建互斥边界。
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": 1_346_578_902},
        )
        model = await self._session.scalar(
            select(UserModel).order_by(UserModel.created_at, UserModel.id)
        )
        return None if model is None else _to_user_credential(model)

    async def create_admin(self, admin: NewAdmin) -> UserIdentity:
        """在已持有单管理员创建锁的事务中插入活动管理员。"""
        model = UserModel(
            email=admin.email,
            display_name=admin.display_name,
            password_hash=admin.password_hash,
            timezone=admin.timezone,
            locale=admin.locale,
            brief_time=admin.brief_time,
            is_active=True,
        )
        self._session.add(model)
        await self._session.flush()
        return _to_user_credential(model).identity


class SqlAlchemyIdentityRepositoryFactory:
    """为每个认证用例提供独立且自动提交/回滚的 SQLAlchemy 事务。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级异步会话工厂，不在构造时建立数据库连接。"""
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[IdentityRepository]:
        """在单一事务内暴露窄 Repository，并在异常时由 SQLAlchemy 回滚。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyIdentityRepository(session)
