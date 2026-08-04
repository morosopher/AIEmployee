"""提供 Google 连接用例所需的 SQLAlchemy 事务存储适配器。"""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.domain.connections import ConnectionStatus
from ai_employee.infrastructure.db.models.sources import (
    EncryptedCredentialModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.encryption import EncryptedValue


class ConnectionStore(Protocol):
    """限定连接用例可使用的持久化操作，确保用户条件在适配器内明确出现。"""

    async def create_attempt(
        self,
        *,
        attempt_id: UUID,
        user_id: UUID,
        state_hash: bytes,
        verifier: EncryptedValue,
        expires_at: datetime,
        created_at: datetime,
    ) -> UUID: ...
    async def consume_attempt(
        self, *, state_hash: bytes, now: datetime
    ) -> tuple[UUID, UUID, EncryptedValue] | None: ...
    async def ensure_connection(
        self, *, user_id: UUID, provider_account_id: str, account_email: str, scopes: list[str]
    ) -> UUID: ...
    async def save_connection_tokens(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        access_token: EncryptedValue,
        refresh_token: EncryptedValue | None,
        expires_at: datetime,
    ) -> None: ...
    async def list_connections(self, *, user_id: UUID) -> tuple[OAuthConnectionModel, ...]: ...
    async def get_connection(
        self, *, user_id: UUID, connection_id: UUID
    ) -> OAuthConnectionModel | None: ...
    async def disconnect(
        self, *, user_id: UUID, connection_id: UUID
    ) -> tuple[bool, EncryptedCredentialModel | None]: ...


class SqlAlchemyConnectionStore:
    """在调用方拥有的异步事务内读写 OAuth 连接及其加密凭据。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前请求独占会话；本对象从不自行提交事务。"""
        self._session = session

    async def create_attempt(
        self,
        *,
        attempt_id: UUID,
        user_id: UUID,
        state_hash: bytes,
        verifier: EncryptedValue,
        expires_at: datetime,
        created_at: datetime,
    ) -> UUID:
        """持久化唯一 state 摘要及记录绑定的 PKCE 密文。"""
        attempt = OAuthAttemptModel(
            id=attempt_id,
            user_id=user_id,
            state_hash=state_hash,
            encrypted_pkce_verifier=verifier.ciphertext,
            nonce=verifier.nonce,
            key_version=verifier.key_version,
            expires_at=expires_at,
            created_at=created_at,
        )
        self._session.add(attempt)
        await self._session.flush()
        return attempt.id

    async def consume_attempt(
        self, *, state_hash: bytes, now: datetime
    ) -> tuple[UUID, UUID, EncryptedValue] | None:
        """锁定并一次性消费未过期 state，防止并发 callback 重放。"""
        attempt = await self._session.scalar(
            select(OAuthAttemptModel)
            .where(OAuthAttemptModel.state_hash == state_hash)
            .with_for_update()
        )
        if attempt is None or attempt.consumed_at is not None or attempt.expires_at <= now:
            return None
        attempt.consumed_at = now
        await self._session.flush()
        return (
            attempt.id,
            attempt.user_id,
            EncryptedValue(attempt.encrypted_pkce_verifier, attempt.nonce, attempt.key_version),
        )

    async def ensure_connection(
        self, *, user_id: UUID, provider_account_id: str, account_email: str, scopes: list[str]
    ) -> UUID:
        """按用户和 Google subject upsert 连接并返回可用于 AAD 的稳定 UUID。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.provider == "google",
                OAuthConnectionModel.provider_account_id == provider_account_id,
            )
            .with_for_update()
        )
        if connection is None:
            connection = OAuthConnectionModel(
                user_id=user_id,
                provider="google",
                provider_account_id=provider_account_id,
                account_email=account_email,
                scopes=scopes,
                status=ConnectionStatus.CONNECTED.value,
            )
            self._session.add(connection)
            await self._session.flush()
        else:
            connection.account_email = account_email
            connection.scopes = scopes
            connection.status = ConnectionStatus.CONNECTED.value
            connection.last_error_code = None
        await self._session.flush()
        return connection.id

    async def save_connection_tokens(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        access_token: EncryptedValue,
        refresh_token: EncryptedValue | None,
        expires_at: datetime,
    ) -> None:
        """写入记录绑定 token 密文并确保两类初始同步游标存在。"""
        await self._upsert_credential(
            connection_id, user_id, "access_token", access_token, expires_at
        )
        if refresh_token is not None:
            await self._upsert_credential(
                connection_id, user_id, "refresh_token", refresh_token, None
            )
        for resource_kind in ("gmail", "calendar"):
            exists = await self._session.scalar(
                select(SyncCursorModel.id).where(
                    SyncCursorModel.connection_id == connection_id,
                    SyncCursorModel.resource_kind == resource_kind,
                )
            )
            if exists is None:
                self._session.add(
                    SyncCursorModel(connection_id=connection_id, resource_kind=resource_kind)
                )
        await self._session.flush()

    async def _upsert_credential(
        self,
        connection_id: UUID,
        user_id: UUID,
        kind: str,
        encrypted: EncryptedValue,
        expires_at: datetime | None,
    ) -> None:
        """以唯一连接/种类键覆写已旋转密文，不创建额外 Token 行。"""
        credential = await self._session.scalar(
            select(EncryptedCredentialModel)
            .where(
                EncryptedCredentialModel.connection_id == connection_id,
                EncryptedCredentialModel.credential_kind == kind,
            )
            .with_for_update()
        )
        if credential is None:
            self._session.add(
                EncryptedCredentialModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    credential_kind=kind,
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    token_expires_at=expires_at,
                )
            )
            return
        credential.ciphertext = encrypted.ciphertext
        credential.nonce = encrypted.nonce
        credential.key_version = encrypted.key_version
        credential.token_expires_at = expires_at

    async def list_connections(self, *, user_id: UUID) -> tuple[OAuthConnectionModel, ...]:
        """按当前用户过滤连接，避免 API 层遗漏多租户边界。"""
        rows = await self._session.scalars(
            select(OAuthConnectionModel)
            .where(OAuthConnectionModel.user_id == user_id)
            .order_by(OAuthConnectionModel.created_at)
        )
        return tuple(rows)

    async def get_connection(
        self, *, user_id: UUID, connection_id: UUID
    ) -> OAuthConnectionModel | None:
        """按用户和主键读取连接，将跨用户资源伪装为不存在。"""
        return await self._session.scalar(
            select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == connection_id, OAuthConnectionModel.user_id == user_id
            )
        )

    async def disconnect(
        self, *, user_id: UUID, connection_id: UUID
    ) -> tuple[bool, EncryptedCredentialModel | None]:
        """删除全部本地密文并标记断开，返回刷新 token 供调用方尽力撤销。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id, OAuthConnectionModel.user_id == user_id
            )
            .with_for_update()
        )
        if connection is None:
            return (False, None)
        refresh = await self._session.scalar(
            select(EncryptedCredentialModel).where(
                EncryptedCredentialModel.connection_id == connection_id,
                EncryptedCredentialModel.credential_kind == "refresh_token",
            )
        )
        await self._session.execute(
            delete(EncryptedCredentialModel).where(
                EncryptedCredentialModel.connection_id == connection_id
            )
        )
        connection.status = ConnectionStatus.DISCONNECTED.value
        connection.last_error_code = None
        return (True, refresh)


class SqlAlchemyConnectionStoreFactory:
    """为每次用例调用提供自动提交或回滚的连接事务。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存 API 进程共享但会话隔离的数据库工厂。"""
        self._session_factory = session_factory

    def __call__(self) -> AbstractAsyncContextManager[ConnectionStore]:
        """创建受 ``begin`` 管理的存储上下文，提交前不暴露部分写入。"""
        return _ConnectionStoreContext(self._session_factory)


class _ConnectionStoreContext(AbstractAsyncContextManager[ConnectionStore]):
    """把 SQLAlchemy session factory 的事务语义收窄为应用层端口。"""

    def __init__(self, factory: ManagedAsyncSessionMaker) -> None:
        self._context = factory.begin()

    async def __aenter__(self) -> ConnectionStore:
        """进入事务并向应用层返回窄存储接口。"""
        return SqlAlchemyConnectionStore(await self._context.__aenter__())

    async def __aexit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> bool | None:
        """委托 SQLAlchemy 执行原子提交或异常回滚。"""
        await self._context.__aexit__(exc_type, exc_value, traceback)
        return None
