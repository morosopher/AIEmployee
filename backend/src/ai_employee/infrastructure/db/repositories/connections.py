"""提供供应商中立连接与能力用例所需的 SQLAlchemy 事务存储适配器。"""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.use_cases.connections import (
    OAUTH_AUTHORIZATION_FAILED_ERROR_CODE,
    ConnectionCapabilitySnapshot,
    ConnectionCredentialOwnershipError,
    ConnectionIdentityConflictError,
    ConnectionStore,
    ConsumedOAuthAttempt,
    OAuthAttemptInvalidatedError,
    StoredCapability,
    StoredConnection,
    StoredProviderCalendar,
)
from ai_employee.domain.connections import (
    CapabilityStatus,
    ConnectionCapability,
    ConnectionStatus,
)
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

# 运行时回填只识别 M2 已批准的两个 Google read scope；写 scope 绝不用于自动启用能力。
_GOOGLE_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


def _scope_sort_key(scope: str) -> tuple[int, int, str]:
    """把无序实际 scope 规范为稳定的身份、邮件、日历展示顺序。

    OAuth 端口故意使用 ``frozenset`` 表示权限判断不依赖供应商返回顺序；JSONB 列仍需
    确定性列表以保持 M1 响应和审计 diff 稳定。排序只识别跨供应商共有的身份 scope 与
    ``mail``/``calendar`` 资源族，不导入或解释任何供应商 SDK 类型。
    """
    identity_order = {
        "openid": 0,
        "profile": 1,
        "email": 2,
        "offline_access": 3,
    }
    if scope in identity_order:
        return (0, identity_order[scope], scope)
    normalized = scope.casefold()
    if "mail" in normalized:
        return (1, 0, scope)
    if "calendar" in normalized:
        return (2, 0, scope)
    return (3, 0, scope)


def _ordered_scopes(scopes: frozenset[str]) -> list[str]:
    """复制无序 scope 集合为可持久化的确定性列表。"""
    return sorted(scopes, key=_scope_sort_key)


def _stored_connection(row: OAuthConnectionModel) -> StoredConnection:
    """把 ORM 连接行复制为不会跨事务触发隐式 I/O 的应用投影。"""
    return StoredConnection(
        id=row.id,
        user_id=row.user_id,
        provider=row.provider,
        provider_account_id=row.provider_account_id,
        provider_tenant_id=row.provider_tenant_id,
        account_type=row.account_type,
        account_email=row.account_email,
        scopes=tuple(row.scopes),
        status=row.status,
        last_error_code=row.last_error_code,
        authorization_generation=row.authorization_generation,
    )


class SqlAlchemyConnectionStore:
    """在调用方拥有的异步事务内读写 OAuth 连接、能力及其加密凭据。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前请求独占会话；本对象从不自行提交事务。"""
        self._session = session

    async def create_attempt(
        self,
        *,
        attempt_id: UUID,
        user_id: UUID,
        provider: str,
        state_hash: bytes,
        verifier: EncryptedValue,
        requested_capabilities: frozenset[ConnectionCapability],
        oidc_nonce_hash: bytes | None,
        expires_at: datetime,
        created_at: datetime,
        target_connection_id: UUID | None = None,
        target_authorization_generation: int | None = None,
    ) -> UUID:
        """持久化唯一 state 摘要、供应商、能力意图及记录绑定 PKCE 密文。"""
        attempt = OAuthAttemptModel(
            id=attempt_id,
            user_id=user_id,
            provider=provider,
            state_hash=state_hash,
            encrypted_pkce_verifier=verifier.ciphertext,
            nonce=verifier.nonce,
            key_version=verifier.key_version,
            requested_capabilities=[
                capability.value
                for capability in sorted(
                    requested_capabilities,
                    key=lambda item: item.value,
                )
            ],
            oidc_nonce_hash=oidc_nonce_hash,
            target_connection_id=target_connection_id,
            target_authorization_generation=target_authorization_generation,
            expires_at=expires_at,
            created_at=created_at,
        )
        self._session.add(attempt)
        await self._session.flush()
        return attempt.id

    async def consume_attempt(
        self,
        *,
        state_hash: bytes,
        now: datetime,
    ) -> ConsumedOAuthAttempt | None:
        """锁定并一次性消费未过期 state，防止并发 callback 重放。"""
        attempt = await self._session.scalar(
            select(OAuthAttemptModel)
            .where(OAuthAttemptModel.state_hash == state_hash)
            .with_for_update()
        )
        if attempt is None:
            return None
        if attempt.invalidated_at is not None:
            # 断开事务会保留失效事实；即使 state 尚未被消费，也必须返回稳定冲突而非
            # 让旧浏览器回调走首次连接 upsert。
            raise OAuthAttemptInvalidatedError
        if attempt.consumed_at is not None or attempt.expires_at <= now:
            return None
        attempt.consumed_at = now
        try:
            requested = frozenset(
                ConnectionCapability(value) for value in attempt.requested_capabilities
            )
        except ValueError as error:
            # 未知持久值表示数据库事实被破坏，不能静默丢弃后继续授权。
            raise RuntimeError("OAuth attempt contains an unknown capability") from error
        await self._session.flush()
        return ConsumedOAuthAttempt(
            id=attempt.id,
            user_id=attempt.user_id,
            provider=attempt.provider,
            requested_capabilities=requested,
            verifier=EncryptedValue(
                attempt.encrypted_pkce_verifier,
                attempt.nonce,
                attempt.key_version,
            ),
            oidc_nonce_hash=attempt.oidc_nonce_hash,
            target_connection_id=attempt.target_connection_id,
            target_authorization_generation=attempt.target_authorization_generation,
        )

    async def validate_unbound_attempt_for_callback(
        self,
        *,
        attempt_id: UUID,
        user_id: UUID,
        provider: str,
    ) -> None:
        """锁定已消费的首次 OAuth attempt，并拒绝断开事务写入的失效标记。"""
        attempt = await self._session.scalar(
            select(OAuthAttemptModel)
            .where(
                OAuthAttemptModel.id == attempt_id,
                OAuthAttemptModel.user_id == user_id,
                OAuthAttemptModel.provider == provider,
            )
            .with_for_update()
        )
        if (
            attempt is None
            or attempt.target_connection_id is not None
            or attempt.target_authorization_generation is not None
            or attempt.consumed_at is None
            or attempt.invalidated_at is not None
        ):
            raise OAuthAttemptInvalidatedError

    async def get_connection_for_update(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> StoredConnection | None:
        """按用户锁定连接行，确保授权能力快照与代际递增处于同一事务。"""
        row = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
            .with_for_update()
        )
        return None if row is None else _stored_connection(row)

    async def ensure_connection(
        self,
        *,
        user_id: UUID,
        provider: str,
        provider_account_id: str,
        provider_tenant_id: str,
        account_type: str,
        account_email: str,
        scopes: frozenset[str],
    ) -> UUID:
        """原子 upsert 供应商连接，并把首次/重连能力基线收敛为 disabled。

        ``provider_account_id`` 必须是 adapter 已规范化的稳定键；Task 10 会把 tenant 与
        Graph user ID 同时编码到该键中。两个 OAuth state 可以并发映射到同一规范键，但
        tenant/type 是键对应的不可变身份事实：冲突更新只在两者完全一致时刷新邮箱、scope
        和状态，否则 PostgreSQL 原子拒绝更新，避免 token 静默改绑到另一身份。该端口只由
        targetless 首次 OAuth callback 使用；命中断开后的旧连接时，四项能力行必须重置，
        随后应用层只按本次冻结读取意图重新启用，不能让历史写能力越过新 scope 复活。
        """
        statement = insert(OAuthConnectionModel).values(
            user_id=user_id,
            provider=provider,
            provider_account_id=provider_account_id,
            provider_tenant_id=provider_tenant_id,
            account_type=account_type,
            account_email=account_email,
            scopes=_ordered_scopes(scopes),
            status=ConnectionStatus.CONNECTED.value,
        )
        connection_id = await self._session.scalar(
            statement.on_conflict_do_update(
                constraint="uq_oauth_connections_user_provider_account",
                set_={
                    "account_email": statement.excluded.account_email,
                    "scopes": statement.excluded.scopes,
                    "status": ConnectionStatus.CONNECTED.value,
                    "last_error_code": None,
                },
                where=and_(
                    OAuthConnectionModel.provider_tenant_id
                    == statement.excluded.provider_tenant_id,
                    OAuthConnectionModel.account_type == statement.excluded.account_type,
                ),
            ).returning(OAuthConnectionModel.id)
        )
        if connection_id is None:
            raise ConnectionIdentityConflictError

        for capability in ConnectionCapability:
            capability_statement = insert(ConnectionCapabilityModel).values(
                user_id=user_id,
                connection_id=connection_id,
                capability=capability.value,
                status=CapabilityStatus.DISABLED.value,
                actual_scopes=[],
                last_verified_at=None,
                last_error_code=None,
            )
            await self._session.execute(
                capability_statement.on_conflict_do_update(
                    constraint="uq_connection_capabilities_user_connection_capability",
                    set_={
                        "status": CapabilityStatus.DISABLED.value,
                        "actual_scopes": [],
                        "last_verified_at": None,
                        "last_error_code": None,
                    },
                )
            )
        await self._session.flush()
        return connection_id

    async def update_connection_scopes(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scopes: frozenset[str],
    ) -> None:
        """在渐进 callback 的绑定锁内更新实际 scope，并拒绝跨用户或断开连接。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == ConnectionStatus.CONNECTED.value,
            )
            .with_for_update()
        )
        if connection is None:
            raise ConnectionCredentialOwnershipError
        connection.scopes = _ordered_scopes(scopes)
        await self._session.flush()

    async def ensure_google_capability_rows(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> None:
        """按连接保存的 scope 幂等补齐 Google 能力行，且不覆盖已有人工状态。

        该方法用于迁移 0011 后仍缺少子行的开发数据库。所有新行复制同一份规范化
        ``actual_scopes``；两个写能力固定 ``disabled``，即使连接保存了粗粒度写 scope
        也不会因配置或回填被自动打开。
        """
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.provider == "google",
                OAuthConnectionModel.status == ConnectionStatus.CONNECTED.value,
            )
            .with_for_update()
        )
        if connection is None:
            # 缺失、跨用户或已断开的连接都保持幂等 no-op；调用方随后读取快照并统一映射
            # 404，不能让维护回填改变原有资源隐藏边界。
            return
        normalized_scopes = frozenset(connection.scopes)
        actual_scopes = _ordered_scopes(normalized_scopes)
        read_status = {
            ConnectionCapability.MAIL_READ: (
                CapabilityStatus.ENABLED.value
                if _GOOGLE_GMAIL_READ_SCOPE in normalized_scopes
                else CapabilityStatus.DISABLED.value
            ),
            ConnectionCapability.CALENDAR_READ: (
                CapabilityStatus.ENABLED.value
                if _GOOGLE_CALENDAR_READ_SCOPE in normalized_scopes
                else CapabilityStatus.DISABLED.value
            ),
        }
        for capability in ConnectionCapability:
            await self._session.execute(
                insert(ConnectionCapabilityModel)
                .values(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability=capability.value,
                    status=read_status.get(capability, CapabilityStatus.DISABLED.value),
                    actual_scopes=actual_scopes,
                )
                .on_conflict_do_nothing(
                    constraint="uq_connection_capabilities_user_connection_capability"
                )
            )
        await self._session.flush()

    async def save_connection_tokens(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        access_token: EncryptedValue,
        refresh_token: EncryptedValue | None,
        expires_at: datetime,
    ) -> None:
        """写入记录绑定 token，并确保 M1 兼容的规范初始同步游标存在。

        在任何 credential upsert 或 cursor insert 前，先用 ``id + user_id + connected``
        锁定连接行。即使调用方错误传入其他用户的连接 ID，也只能得到稳定拒绝，不能利用
        ``connection_id + credential_kind`` 唯一键覆写原用户密文。

        ``refresh_token=None`` 表示供应商没有轮换，而不是撤销；此时不执行 upsert，保留
        既有密文。0013 已把 Gmail 游标原位迁移为 ``mail/mailbox``，重连不能再建旧键。
        """
        owned_connection_id = await self._session.scalar(
            select(OAuthConnectionModel.id)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == ConnectionStatus.CONNECTED.value,
            )
            .with_for_update()
        )
        if owned_connection_id is None:
            raise ConnectionCredentialOwnershipError

        await self._upsert_credential(
            connection_id,
            user_id,
            "access_token",
            access_token,
            expires_at,
        )
        if refresh_token is not None:
            await self._upsert_credential(
                connection_id,
                user_id,
                "refresh_token",
                refresh_token,
                None,
            )
        for resource_kind, scope_key in (
            ("mail", "mailbox"),
            ("calendar", "primary"),
        ):
            await self._session.execute(
                insert(SyncCursorModel)
                .values(
                    connection_id=connection_id,
                    resource_kind=resource_kind,
                    scope_key=scope_key,
                )
                .on_conflict_do_nothing(constraint="uq_sync_cursors_connection_resource")
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
        """原子覆写旋转 token 密文，避免并发 callback 产生第二条 credential 行。"""
        statement = insert(EncryptedCredentialModel).values(
            user_id=user_id,
            connection_id=connection_id,
            credential_kind=kind,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            key_version=encrypted.key_version,
            token_expires_at=expires_at,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_encrypted_credentials_connection_kind",
                set_={
                    "ciphertext": statement.excluded.ciphertext,
                    "nonce": statement.excluded.nonce,
                    "key_version": statement.excluded.key_version,
                    "token_expires_at": statement.excluded.token_expires_at,
                },
            )
        )

    async def save_capability_state(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capability: ConnectionCapability,
        status: CapabilityStatus,
        actual_scopes: frozenset[str],
        last_verified_at: datetime | None,
        last_error_code: str | None,
    ) -> None:
        """upsert 单项能力，并以供应商最新事实完整替换 actual scopes。"""
        statement = insert(ConnectionCapabilityModel).values(
            user_id=user_id,
            connection_id=connection_id,
            capability=capability.value,
            status=status.value,
            actual_scopes=_ordered_scopes(actual_scopes),
            last_verified_at=last_verified_at,
            last_error_code=last_error_code,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_connection_capabilities_user_connection_capability",
                set_={
                    "status": statement.excluded.status,
                    "actual_scopes": statement.excluded.actual_scopes,
                    "last_verified_at": statement.excluded.last_verified_at,
                    "last_error_code": statement.excluded.last_error_code,
                },
            )
        )
        await self._session.flush()

    async def set_capabilities_authorizing(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capabilities: frozenset[ConnectionCapability],
    ) -> int:
        """锁定连接并递增代际，再把授权并集置为 authorizing。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.status == ConnectionStatus.CONNECTED.value,
            )
            .with_for_update()
        )
        if connection is None:
            raise ConnectionCredentialOwnershipError
        connection.authorization_generation += 1
        for capability in sorted(capabilities, key=lambda item: item.value):
            statement = insert(ConnectionCapabilityModel).values(
                user_id=user_id,
                connection_id=connection_id,
                capability=capability.value,
                status=CapabilityStatus.AUTHORIZING.value,
                actual_scopes=[],
                last_error_code=None,
            )
            await self._session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_connection_capabilities_user_connection_capability",
                    set_={
                        "status": CapabilityStatus.AUTHORIZING.value,
                        "last_error_code": None,
                    },
                )
            )
        await self._session.flush()
        return connection.authorization_generation

    async def mark_progressive_authorization_failed(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        authorization_generation: int,
        capabilities: frozenset[ConnectionCapability],
        error_code: str,
    ) -> None:
        """在连接行锁与授权代际仍匹配时收敛失败能力，过时回调安全 no-op。

        失败 callback 可能与新一轮授权、能力关闭或断开并发。先锁定连接并检查
        ``connected``、用户归属和单调代际，再只更新仍为 ``authorizing`` 的本次能力；
        因而旧失败不能覆盖新状态，也不会抹掉既有实际 scope 快照。
        """
        if not capabilities:
            return
        if error_code != OAUTH_AUTHORIZATION_FAILED_ERROR_CODE:
            raise ValueError("progressive authorization failure code is invalid")
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
            .with_for_update()
        )
        if (
            connection is None
            or connection.status != ConnectionStatus.CONNECTED.value
            or connection.authorization_generation != authorization_generation
        ):
            return
        capability_values = tuple(capability.value for capability in capabilities)
        await self._session.execute(
            update(ConnectionCapabilityModel)
            .where(
                ConnectionCapabilityModel.user_id == user_id,
                ConnectionCapabilityModel.connection_id == connection_id,
                ConnectionCapabilityModel.capability.in_(capability_values),
                ConnectionCapabilityModel.status == CapabilityStatus.AUTHORIZING.value,
            )
            .values(
                status=CapabilityStatus.ACTION_REQUIRED.value,
                last_error_code=error_code,
            )
        )
        await self._session.flush()

    async def ensure_bound_connection_for_callback(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        authorization_generation: int,
        provider: str,
        provider_account_id: str,
        provider_tenant_id: str,
        account_type: str,
    ) -> UUID:
        """锁定并核对冻结连接身份与授权代际，任何不一致都 fail closed。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
            .with_for_update()
        )
        if (
            connection is None
            or connection.status != ConnectionStatus.CONNECTED.value
            or connection.authorization_generation != authorization_generation
            or connection.provider != provider
            or connection.provider_account_id != provider_account_id
            or connection.provider_tenant_id != provider_tenant_id
            or connection.account_type != account_type
        ):
            raise OAuthAttemptInvalidatedError
        return connection.id

    async def disable_capability(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capability: ConnectionCapability,
    ) -> None:
        """本地关闭能力并保留实际 scope 事实，供界面说明仍需重连才能缩权。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
            .with_for_update()
        )
        if connection is None:
            raise ConnectionCredentialOwnershipError
        connection.authorization_generation += 1
        statement = insert(ConnectionCapabilityModel).values(
            user_id=user_id,
            connection_id=connection_id,
            capability=capability.value,
            status=CapabilityStatus.DISABLED.value,
            actual_scopes=[],
            last_error_code=None,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                constraint="uq_connection_capabilities_user_connection_capability",
                set_={
                    "status": CapabilityStatus.DISABLED.value,
                    "last_error_code": None,
                },
            )
        )
        await self._session.flush()

    async def get_enabled_capabilities(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> frozenset[ConnectionCapability] | None:
        """先证明连接归属，再返回精确 ``enabled`` 能力集合。"""
        connection_exists = await self._session.scalar(
            select(OAuthConnectionModel.id)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
            .with_for_update()
        )
        if connection_exists is None:
            return None
        rows = await self._session.scalars(
            select(ConnectionCapabilityModel.capability).where(
                ConnectionCapabilityModel.user_id == user_id,
                ConnectionCapabilityModel.connection_id == connection_id,
                ConnectionCapabilityModel.status == CapabilityStatus.ENABLED.value,
            )
        )
        try:
            return frozenset(ConnectionCapability(value) for value in rows)
        except ValueError as error:
            raise RuntimeError("connection capability row contains an unknown value") from error

    async def get_capability_snapshot(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> ConnectionCapabilitySnapshot | None:
        """按用户读取能力与日历目录，并复制为显式 application DTO。"""
        connection = await self._session.scalar(
            select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
        )
        if connection is None:
            return None
        capability_rows = tuple(
            (
                await self._session.scalars(
                    select(ConnectionCapabilityModel)
                    .where(
                        ConnectionCapabilityModel.user_id == user_id,
                        ConnectionCapabilityModel.connection_id == connection_id,
                    )
                    .order_by(ConnectionCapabilityModel.capability)
                )
            ).all()
        )
        calendar_rows = tuple(
            (
                await self._session.scalars(
                    select(ProviderCalendarModel)
                    .where(
                        ProviderCalendarModel.user_id == user_id,
                        ProviderCalendarModel.connection_id == connection_id,
                    )
                    .order_by(
                        ProviderCalendarModel.is_primary.desc(),
                        ProviderCalendarModel.name,
                        ProviderCalendarModel.provider_calendar_id,
                    )
                )
            ).all()
        )
        try:
            capabilities = tuple(
                StoredCapability(
                    capability=ConnectionCapability(row.capability),
                    status=CapabilityStatus(row.status),
                    actual_scopes=tuple(row.actual_scopes),
                    last_verified_at=row.last_verified_at,
                    last_error_code=row.last_error_code,
                )
                for row in capability_rows
            )
        except ValueError as error:
            raise RuntimeError("connection capability row contains an unknown state") from error
        calendars = tuple(
            StoredProviderCalendar(
                id=row.provider_calendar_id,
                name=row.name,
                timezone=row.timezone,
                is_primary=row.is_primary,
                access_role=row.access_role,
                can_write=row.can_write,
                provider_url=row.provider_url,
            )
            for row in calendar_rows
        )
        return ConnectionCapabilitySnapshot(
            connection_id=connection.id,
            provider=connection.provider,
            capabilities=capabilities,
            provider_calendars=calendars,
        )

    async def list_connections(self, *, user_id: UUID) -> tuple[StoredConnection, ...]:
        """按当前用户过滤连接，避免 API 层遗漏多租户边界。"""
        rows = await self._session.scalars(
            select(OAuthConnectionModel)
            .where(OAuthConnectionModel.user_id == user_id)
            .order_by(OAuthConnectionModel.created_at)
        )
        return tuple(_stored_connection(row) for row in rows)

    async def get_connection(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> StoredConnection | None:
        """按用户和主键读取连接，将跨用户资源伪装为不存在。"""
        row = await self._session.scalar(
            select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
        )
        return None if row is None else _stored_connection(row)

    async def disconnect(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        invalidated_at: datetime,
    ) -> tuple[bool, EncryptedValue | None]:
        """先失效首次 OAuth state，再删除密文并标记断开。

        断开事务先更新同用户/供应商的 targetless attempt，再锁定 connection；首次 callback
        保存阶段使用同样的 ``attempt row → connection row`` 锁序，避免交错提交形成死锁，
        并防止旧 state 在断开后复活连接。渐进 callback 不锁定 attempt，而是锁定冻结的
        target connection 并校验 ``authorization_generation``；断开时的 targetless UPDATE
        不会命中它，代际变化仍会使旧渐进 callback fail closed。
        """
        provider = await self._session.scalar(
            select(OAuthConnectionModel.provider).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
        )
        if provider is None:
            return (False, None)
        # 同一用户/供应商的所有首次 state（包括已消费但尚未保存结果的 state）都必须
        # 原子标记；渐进授权 target_connection_id 非 NULL，交由授权代际单独控制。
        await self._session.execute(
            update(OAuthAttemptModel)
            .where(
                OAuthAttemptModel.user_id == user_id,
                OAuthAttemptModel.provider == provider,
                OAuthAttemptModel.target_connection_id.is_(None),
                OAuthAttemptModel.invalidated_at.is_(None),
            )
            .values(invalidated_at=invalidated_at)
        )
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
            .with_for_update()
        )
        if connection is None:
            return (False, None)
        refresh_row = await self._session.scalar(
            select(EncryptedCredentialModel).where(
                EncryptedCredentialModel.user_id == user_id,
                EncryptedCredentialModel.connection_id == connection_id,
                EncryptedCredentialModel.credential_kind == "refresh_token",
            )
        )
        refresh = (
            EncryptedValue(
                refresh_row.ciphertext,
                refresh_row.nonce,
                refresh_row.key_version,
            )
            if refresh_row is not None
            else None
        )
        await self._session.execute(
            delete(EncryptedCredentialModel).where(
                EncryptedCredentialModel.user_id == user_id,
                EncryptedCredentialModel.connection_id == connection_id,
            )
        )
        connection.authorization_generation += 1
        connection.status = ConnectionStatus.DISCONNECTED.value
        connection.last_error_code = None
        await self._session.flush()
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
        """保存尚未进入的 SQLAlchemy ``begin`` 上下文。"""
        self._context = factory.begin()

    async def __aenter__(self) -> ConnectionStore:
        """进入事务并向应用层返回窄存储接口。"""
        return SqlAlchemyConnectionStore(await self._context.__aenter__())

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> bool | None:
        """委托 SQLAlchemy 执行原子提交或异常回滚。"""
        await self._context.__aexit__(exc_type, exc_value, traceback)
        return None
