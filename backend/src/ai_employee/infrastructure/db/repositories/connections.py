"""提供供应商中立连接与能力用例所需的 SQLAlchemy 事务存储适配器。"""

from contextlib import AbstractAsyncContextManager, AsyncExitStack
from datetime import datetime
from types import TracebackType
from typing import get_args
from uuid import UUID

from sqlalchemy import String, cast, delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
from ai_employee.application.ports.credential_rotation import (
    CredentialRotationRepository,
    OAuthRefreshAuditRecord,
    RecoveryCapabilityTransition,
    RecoveryFailureCode,
)
from ai_employee.application.ports.encryption import EncryptedValue, Encryption
from ai_employee.application.use_cases.connections import (
    ConnectionCapabilitySnapshot,
    ConnectionCredentialOwnershipError,
    ConnectionIdentityConflictError,
    ConnectionStore,
    ConsumedOAuthAttempt,
    OAuthAttemptInvalidatedError,
    OAuthAuthorizationScopeConflictError,
    OAuthRevokeBacklog,
    StoredCapability,
    StoredConnection,
    StoredProviderCalendar,
)
from ai_employee.domain.connections import (
    CapabilityStatus,
    ConnectionCapability,
    ConnectionCapabilityDependencyConflict,
    ConnectionStatus,
    validate_capability_disable,
)
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories.credential_rotation import (
    SqlAlchemyCredentialRotationRepository,
    assert_connection_unfenced,
    lock_oauth_user,
)
from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import read_refresh_events
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    invalidate_unclaimed_actions_for_connection,
    lock_connection_submission_scope,
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
        # Microsoft Graph `/me` 依赖的 User.Read 仍属于基础身份 scope，不能被
        # 按未知供应商权限排到 Mail/Calendar 资源之后；这样数据库 JSONB 列与授权 URL
        # 维持同一确定性身份→离线→资源顺序。
        "User.Read": 3,
        "offline_access": 4,
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
        """持久化唯一 state 摘要、供应商、能力意图及记录绑定 PKCE 密文。

        首次授权没有目标连接，必须在本事务锁后重验用户 active，并持锁直到调用方提交，
        防止旧认证请求在全数据删除后向保留的匿名用户重新写入 attempt。渐进授权已由
        get_connection_for_update 按 user→connection 同步，再走本路径的用户外键写入；
        不在持连接之后新增另一套用户锁或改变原有失败分类。

        Raises:
            StateConflictError: 首次授权的用户缺失或已停用；不写入任何 attempt 数据。
        """
        if target_connection_id is None:
            active = await lock_oauth_user(self._session, user_id=user_id)
            if active is not True:
                raise StateConflictError(error_code="user_inactive", message="User is inactive")
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
        """先同步 user 再锁后重读并一次性消费 state，防止重放及失败审计外键反序。

        预读只定位所属 user，不使用预读 attempt 状态作任何授权决定。等待 user 期间
        attempt 可能已被消费、失效或删除，因此下方必须按 state+user 重新读取全部守卫。
        user 锁也是全数据删除的串行屏障；已提交 inactive 时不得消费残留 attempt 或
        追加普通 OAuth 失败审计。返回未命中，沿用既有 ``oauth_state_rejected`` 边界，
        不新增回调协议或复活删除任务。活动用户的消费、能力失败与审计仍原子提交。
        """
        user_id = await self._session.scalar(
            select(OAuthAttemptModel.user_id).where(OAuthAttemptModel.state_hash == state_hash)
        )
        if user_id is None:
            return None
        if not await lock_oauth_user(self._session, user_id=user_id):
            return None
        attempt = await self._session.scalar(
            select(OAuthAttemptModel)
            .where(
                OAuthAttemptModel.state_hash == state_hash,
                OAuthAttemptModel.user_id == user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
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

    async def record_authorization_failed(
        self,
        *,
        user_id: UUID,
        attempt_id: UUID,
        provider: str,
        occurred_at: datetime,
        error_code: RecoveryFailureCode = "oauth_authorization_failed",
    ) -> None:
        """追加本事务已消费 attempt 的固定失败审计，保留原用户归属且不接收 raw error。

        Args:
            user_id: state 解析出的所属用户，不能由 callback query 另行指定。
            attempt_id: 当前事务已锁定并消费的一次性 OAuthAttempt。
            provider: 已与 attempt 匹配的固定供应商名。
            occurred_at: 用例注入的 UTC 消费时刻。

        该方法不提交事务；审计 INSERT 失败必须连同消费和能力收敛一起回滚。
        """
        owned = await self._session.scalar(
            select(OAuthAttemptModel.id).where(
                OAuthAttemptModel.id == attempt_id,
                OAuthAttemptModel.user_id == user_id,
                OAuthAttemptModel.provider == provider,
                OAuthAttemptModel.consumed_at == occurred_at,
            )
        )
        if (
            owned is None
            or provider not in {"google", "microsoft"}
            or error_code not in get_args(RecoveryFailureCode)
        ):
            raise ValueError("OAuth failure audit requires a matching consumed attempt")
        self._session.add(
            AuditEventModel(
                user_id=user_id,
                task_id=None,
                actor_type="system",
                actor_id=None,
                event_type="oauth.authorization_failed",
                created_at=occurred_at,
                event_metadata={
                    "provider": provider,
                    "oauth_attempt_id": str(attempt_id),
                    "error_code": error_code,
                },
            )
        )
        await self._session.flush()

    async def get_refresh_events(
        self, *, user_id: UUID, connection_id: UUID
    ) -> tuple[OAuthRefreshAuditRecord, ...]:
        """复用 coordinator 的用户/连接过滤查询，不复制结果联合解析规则。"""
        return await read_refresh_events(
            self._session, user_id=user_id, connection_id=connection_id
        )

    def credential_rotation(
        self, *, cipher: Encryption, identity: OAuthRefreshIdentity
    ) -> CredentialRotationRepository:
        """向应用层暴露同一事务的 Task27A writer，scope/能力/result 因此共同提交或回滚。"""
        return SqlAlchemyCredentialRotationRepository(
            self._session, cipher=cipher, identity=identity
        )

    async def validate_unbound_attempt_for_callback(
        self,
        *,
        attempt_id: UUID,
        user_id: UUID,
        provider: str,
    ) -> None:
        """先同步 user，再锁定已消费首次 attempt 并拒绝持久失效标记。

        本方法是 targetless 保存事务的首次锁入口；只调整 save helper 内的 SELECT
        会遗留 attempt/connection→user 反序。原 attempt 拒绝和后续 active 守卫不变。
        """
        await lock_oauth_user(self._session, user_id=user_id)
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
        """先同步用户再锁定连接，确保能力/代际和后续 attempt/result FK 使用同一锁序。"""
        await lock_oauth_user(self._session, user_id=user_id)
        row = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
            .with_for_update()
        )
        return None if row is None else _stored_connection(row)

    async def validate_unfenced_identity_for_callback(
        self,
        *,
        user_id: UUID,
        provider: str,
        provider_account_id: str,
        identity_key_version: int,
    ) -> None:
        """在无目标 callback 调用 ensure 前，以规范身份锁定既有连接并拒绝 fence。

        Args:
            user_id: 已验证 OAuthAttempt 的用户；查询始终显式限定归属。
            provider: 已由 attempt 绑定并由 adapter 规范化的供应商名。
            provider_account_id: 不含可变邮箱语义的规范账户键。
            identity_key_version: 与共享 refresh identity 服务一致的根密钥版本。

        不存在或已断开的身份沿用既有 bootstrap/reset 规则；此检查不创建连接、不
        改变 scope/能力/凭据，也不把重新连接解释为原 fence 的 replacement proof。
        已存在行的锁持续到 callback 事务结束；并发插入仍由 ensure 内部复查兜底。

        Raises:
            OAuthRefreshError: connected 身份有未关闭的自动 fence，或审计验证失败。
        """
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.provider == provider,
                OAuthConnectionModel.provider_account_id == provider_account_id,
            )
            .with_for_update()
        )
        if connection is not None and connection.status == ConnectionStatus.CONNECTED.value:
            await assert_connection_unfenced(
                self._session,
                user_id=user_id,
                connection_id=connection.id,
                key_version=identity_key_version,
            )

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
        identity_key_version: int | None = None,
    ) -> UUID:
        """按规范账户键安全建立或合并连接，并初始化/保留能力行。

        ``provider_account_id`` 必须是 adapter 已规范化的稳定键；Task 10 会把 tenant 与
        Graph user ID 同时编码到该键中。两个 OAuth state 可以并发映射到同一规范键，因此
        这里先锁定已有连接，再处理竞争插入：同一用户、供应商和账户键的事务会在连接行锁
        上串行化。后到的 connected callback 只有在新 token 的实际 scope 覆盖当前连接
        scope 时才允许替换凭据；subset/disjoint token 必须 fail closed，避免本地能力并集
        与最后保存的单个 token 不一致。tenant/type 是不可变身份事实，任何不一致都拒绝。

        targetless callback 命中 ``disconnected``（或其他非 connected）连接时仍执行精确
        reset：历史能力和 scope 不能越过断开重新复活。只有已经 connected 的同身份连接
        走 scope 单调覆盖校验，并对既有 capability 行使用 ``DO NOTHING``，由应用层随后
        只更新本次 requested capabilities。
        """
        ordered_scopes = _ordered_scopes(scopes)
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.provider == provider,
                OAuthConnectionModel.provider_account_id == provider_account_id,
            )
            .with_for_update()
        )
        inserted = False
        if connection is None:
            # 先查后插无法独占不存在的唯一键；DO NOTHING 让并发插入等待持有者提交，
            # 随后重新按同一用户范围加行锁读取，避免重复连接或绕过身份核对。
            statement = insert(OAuthConnectionModel).values(
                user_id=user_id,
                provider=provider,
                provider_account_id=provider_account_id,
                provider_tenant_id=provider_tenant_id,
                account_type=account_type,
                account_email=account_email,
                scopes=ordered_scopes,
                status=ConnectionStatus.CONNECTED.value,
            )
            inserted_id = await self._session.scalar(
                statement.on_conflict_do_nothing(
                    constraint="uq_oauth_connections_user_provider_account"
                ).returning(OAuthConnectionModel.id)
            )
            if inserted_id is not None:
                connection = await self._session.scalar(
                    select(OAuthConnectionModel)
                    .where(
                        OAuthConnectionModel.id == inserted_id,
                        OAuthConnectionModel.user_id == user_id,
                    )
                    .with_for_update()
                )
                inserted = True
            else:
                connection = await self._session.scalar(
                    select(OAuthConnectionModel)
                    .where(
                        OAuthConnectionModel.user_id == user_id,
                        OAuthConnectionModel.provider == provider,
                        OAuthConnectionModel.provider_account_id == provider_account_id,
                    )
                    .with_for_update()
                )
        if connection is None:
            # 唯一键冲突后行被并发删除/回滚属于数据库不变量故障，不能静默创建第二个事实。
            raise RuntimeError("OAuth connection disappeared during identity upsert")
        if (
            connection.provider_tenant_id != provider_tenant_id
            or connection.account_type != account_type
        ):
            raise ConnectionIdentityConflictError

        preserve_connected_state = (
            not inserted and connection.status == ConnectionStatus.CONNECTED.value
        )
        if preserve_connected_state:
            # 必须在 account/scope/capability 的任何赋值之前检查；唯一键竞争后重新
            # 读出的既有连接也经过此门禁，无 target 的 code 不能成为恢复 fence 的捷径。
            await assert_connection_unfenced(
                self._session,
                user_id=user_id,
                connection_id=connection.id,
                key_version=identity_key_version,
            )
        if preserve_connected_state and not frozenset(connection.scopes).issubset(scopes):
            # 一个连接只持久化一组 access/refresh token。若 incoming token 不覆盖当前
            # scope，合并 JSON scope 或保留双 enabled 能力都会制造无法由最终凭据证明的
            # 本地事实；必须在写邮箱、状态、能力或 credential 之前整体拒绝。
            raise OAuthAuthorizationScopeConflictError
        connection.account_email = account_email
        connection.last_error_code = None
        if preserve_connected_state:
            # incoming token 已证明包含全部当前 scope；保存其精确事实，并让应用层只更新
            # 本次 requested 能力，未请求行继续保留既有验证结果。
            connection.scopes = ordered_scopes
        else:
            # 新建行已带入本次 scope；断开/过期等旧状态必须从本次 token 事实重新开始。
            connection.scopes = ordered_scopes
            connection.status = ConnectionStatus.CONNECTED.value

        for capability in ConnectionCapability:
            capability_statement = insert(ConnectionCapabilityModel).values(
                user_id=user_id,
                connection_id=connection.id,
                capability=capability.value,
                status=CapabilityStatus.DISABLED.value,
                actual_scopes=[],
                last_verified_at=None,
                last_error_code=None,
            )
            if preserve_connected_state:
                await self._session.execute(
                    capability_statement.on_conflict_do_nothing(
                        constraint="uq_connection_capabilities_user_connection_capability"
                    )
                )
            else:
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
        return connection.id

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

        在任何 credential upsert 或 cursor insert 前，先同步 user，再用
        ``id + user_id + connected`` 锁定连接行。即使调用方传入其他用户的连接 ID，也只能拒绝，不能利用
        ``connection_id + credential_kind`` 唯一键覆写原用户密文。

        ``refresh_token=None`` 表示供应商没有轮换，而不是撤销；此时不执行 upsert，保留
        既有密文。0013 已把 Gmail 游标原位迁移为 ``mail/mailbox``，重连不能再建旧键。
        """
        # callback 的首个 attempt/connection 入口已按 user-first 同步；直接调用 save
        # 也必须保持该顺序。仍先判连接归属、再沿原分类拒绝 inactive，不引入新错误码。
        active = await lock_oauth_user(self._session, user_id=user_id)
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

        # code exchange 已在事务外发生；本地删除可能在等待响应时提交。连接仍存在
        # 不能授权复活 credential，锁住 user 后重检并让同一事务的 scope/upsert 整体回滚。
        if active is not True:
            raise ConnectionCredentialOwnershipError

        await assert_connection_unfenced(
            self._session,
            user_id=user_id,
            connection_id=connection_id,
            key_version=access_token.key_version,
        )

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
    ) -> RecoveryCapabilityTransition:
        """在用户仍 active、连接行锁与授权代际匹配时收敛失败，迟到回调安全 no-op。

        失败 callback 可能与新一轮授权、能力关闭或断开并发。先锁定连接并检查
        ``connected``、用户归属和单调代际，再只更新仍为 ``authorizing`` 的本次能力；
        因而旧失败不能覆盖新状态，也不会抹掉既有实际 scope 快照。
        """
        if not capabilities:
            return "stale_target_noop"
        if error_code not in get_args(RecoveryFailureCode):
            raise ValueError("progressive authorization failure code is invalid")
        # 普通 error callback 已从 consume 取得 user；独立 unsatisfied 事务也从此入口
        # 开始，不能先锁 connection 再让后续失败审计的 user 外键造成反向等待。
        if not await lock_oauth_user(self._session, user_id=user_id):
            # state 已消费并不授权网络后的新事务；删除屏障之后不能重建普通能力事实。
            return "stale_target_noop"
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
            return "stale_target_noop"
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
        return "action_required"

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
        identity_key_version: int | None = None,
    ) -> UUID:
        """按 user→connection 锁定并核对冻结身份/代际，原拒绝语义与 active 守卫保持不变。"""
        await lock_oauth_user(self._session, user_id=user_id)
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
        await assert_connection_unfenced(
            self._session,
            user_id=user_id,
            connection_id=connection_id,
            key_version=identity_key_version,
        )
        return connection.id

    async def disable_capability(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capability: ConnectionCapability,
    ) -> None:
        """在Task→User屏障后关闭能力，保留实际scope供界面说明仍需重连才能缩权。

        公共失效入口的零计数不能区分空候选与inactive；本方法仍在同事务检查用户，
        防止空候选或屏障拒绝后继续改写连接与能力。用户锁保持到整个关闭事务提交。
        """
        # 先覆盖尚未创建 Task 的并发提交；行锁仍从 Task 开始，不能提前锁 Connection。
        await lock_connection_submission_scope(
            self._session, user_id=user_id, connection_id=connection_id
        )
        affected_actions = (
            frozenset({"mail.send"})
            if capability in {ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND}
            else frozenset({"calendar.create", "calendar.update", "calendar.restore"})
        )
        # 必须先沿可信动作固定锁序处理任务，再锁 Connection；claim 若已取得任务锁可先
        # 完成认领，反之会在审批失效后稳定失败，双方不会互相持有反向行锁。
        await invalidate_unclaimed_actions_for_connection(
            self._session,
            user_id=user_id,
            connection_id=connection_id,
            actions=affected_actions,
            reason="connection_capability_disabled",
        )
        if not await lock_oauth_user(self._session, user_id=user_id):
            raise ConnectionCredentialOwnershipError
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
        # scanner 已按全部Task→User→Approval→ToolExecution→本地动作完成候选状态变更；
        # 现在才在同一事务取得 Connection 锁后的能力快照。渐进 callback 必须先通过
        # 这把锁，因而不会在读取依赖与关闭能力之间提交新的写能力。
        enabled = await self.get_enabled_capabilities(
            user_id=user_id,
            connection_id=connection_id,
        )
        if enabled is None:
            raise ConnectionCredentialOwnershipError
        validate_capability_disable(capability, enabled)
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
            # 能力快照是关闭事务的授权输入；保留连接行锁语义，使直接调用该仓储端口
            # 也不会在渐进 callback 修改 capability 的窗口内读取过时 enabled 集合。
            .with_for_update()
        )
        if connection_exists is None:
            return None
        rows = await self._session.scalars(
            select(ConnectionCapabilityModel)
            .where(
                ConnectionCapabilityModel.user_id == user_id,
                ConnectionCapabilityModel.connection_id == connection_id,
            )
            .order_by(ConnectionCapabilityModel.capability)
            .with_for_update()
        )
        try:
            return frozenset(
                ConnectionCapability(row.capability)
                for row in rows
                if row.status == CapabilityStatus.ENABLED.value
            )
        except ValueError as error:
            raise ConnectionCapabilityDependencyConflict from error

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
        在处理可信候选后复核同一User行锁；即使候选为空，inactive也不能失效attempt、
        删除凭据或改写连接状态。窄revoke仍位于本地提交后的事务外。
        """
        # 共享提交屏障必须位于候选扫描和所有行锁之前，避免授权旧快照在断开后落库。
        await lock_connection_submission_scope(
            self._session, user_id=user_id, connection_id=connection_id
        )
        provider = await self._session.scalar(
            select(OAuthConnectionModel.provider).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
        )
        if provider is None:
            return (False, None)
        # 断开连接会同时切断四种真实写动作；沿可信动作全部Task→User→Approval→
        # ToolExecution→本地对象锁序先失效尚未认领的工作，避免已排队消息在连接状态
        # 改变后继续进入 provider。已形成 ToolExecution 的动作由 scanner 保留并交给
        # reconciliation，不在这里伪造取消或结果。
        await invalidate_unclaimed_actions_for_connection(
            self._session,
            user_id=user_id,
            connection_id=connection_id,
            actions=frozenset(
                {
                    "mail.send",
                    "calendar.create",
                    "calendar.update",
                    "calendar.restore",
                }
            ),
            # 已认领动作失去连接凭据后必须进入只读核对并显示统一的能力缺失原因；同一
            # reason 也会写入未认领生命周期审计，便于操作中心按一个稳定码聚合。
            reason="connection_scope_missing",
        )
        if not await lock_oauth_user(self._session, user_id=user_id):
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
        # 断开后本地能力即使原先已启用也不能继续被读取或写入；保留 actual_scopes 与
        # last_verified_at 作为供应商历史投影，只把当前可用状态收敛为 revoked。这样
        # Microsoft 没有窄 revoke 端点或远端失败时，数据库仍不会伪造“scope 已删除”的
        # 事实，同时所有后续能力检查都会 fail closed。
        await self._session.execute(
            update(ConnectionCapabilityModel)
            .where(
                ConnectionCapabilityModel.user_id == user_id,
                ConnectionCapabilityModel.connection_id == connection_id,
            )
            .values(
                status=CapabilityStatus.REVOKED.value,
                last_error_code=None,
            )
        )
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

    async def record_oauth_revoke_unresolved(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        provider: str,
        error_code: str,
        occurred_at: datetime,
    ) -> None:
        """追加一次 token-free 的撤销未决维护事实。

        该方法只在本地断开事务已经提交、供应商调用失败或明确没有窄撤销端点后执行。
        它使用独立短事务写入 ``AuditEvent``，metadata 仅包含供应商、稳定错误码与连接
        标识；不会把 refresh token、供应商响应或异常正文转移到 PostgreSQL，也不创建
        Outbox/TaskRun，因此后续调度无法重建已删除凭据。
        网络后的独立事务先锁User；删除屏障先提交时no-op，保留原网络错误传播，
        不把断开前的活动状态当作追加普通审计的授权。

        Args:
            user_id: 连接所属用户，用于第二层归属校验。
            connection_id: 已断开连接的稳定标识。
            provider: 断开前读取的固定供应商名称。
            error_code: 已脱敏的稳定机器错误码。
            occurred_at: 调用方注入的带时区 UTC 事实时间。

        Raises:
            ValueError: 输入不是稳定、非空的审计边界值。
            ConnectionCredentialOwnershipError: 连接缺失、跨用户或供应商身份已变化。
        """
        if type(provider) is not str or not provider or provider != provider.strip():
            raise ValueError("provider must be a stable non-empty name")
        if type(error_code) is not str or not error_code or error_code != error_code.strip():
            raise ValueError("error_code must be a stable non-empty code")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if not await lock_oauth_user(self._session, user_id=user_id):
            return
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
            .with_for_update()
        )
        if connection is None or connection.provider != provider:
            raise ConnectionCredentialOwnershipError
        self._session.add(
            AuditEventModel(
                user_id=user_id,
                task_id=None,
                event_type="oauth.revoke_unresolved",
                actor_type="system",
                actor_id=None,
                event_metadata={
                    "provider": provider,
                    "error_code": error_code,
                    "connection_id": str(connection_id),
                },
                created_at=occurred_at,
            )
        )
        await self._session.flush()

    async def get_oauth_revoke_backlog(self) -> tuple[OAuthRevokeBacklog, ...]:
        """只聚合未决审计，不读取凭据，也不构造任何可重放 revoke 的 Task/Outbox。

        系统维护可跨用户聚合，但返回仅有两种固定 provider 与计数。补救事实必须同时
        匹配原事件用户、连接和审计游标，其他用户的确认不能降低该用户的积压数量。
        """
        remediation = aliased(AuditEventModel)
        provider = AuditEventModel.event_metadata["provider"].astext
        resolved = exists(
            select(remediation.id).where(
                remediation.event_type == "oauth.revoke_remediated",
                remediation.user_id == AuditEventModel.user_id,
                remediation.event_metadata["connection_id"].astext
                == AuditEventModel.event_metadata["connection_id"].astext,
                remediation.event_metadata["unresolved_event_id"].astext
                == cast(AuditEventModel.id, String),
            )
        )
        rows = (
            await self._session.execute(
                select(provider, func.count(AuditEventModel.id))
                .where(
                    AuditEventModel.event_type == "oauth.revoke_unresolved",
                    provider.in_(("google", "microsoft")),
                    ~resolved,
                )
                .group_by(provider)
                .order_by(provider)
            )
        ).all()
        return tuple(OAuthRevokeBacklog(provider=row[0], unresolved_count=row[1]) for row in rows)

    async def record_oauth_revoke_remediation(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        unresolved_event_id: int,
        occurred_at: datetime,
    ) -> bool:
        """锁定精确连接后追加操作者补救事实，不更新/删除原始 append-only 审计。

        连接行锁让并发确认串行；审计角色无需获得 UPDATE 权限或 AuditEvent 行锁。
        重复、跨用户、连接不匹配和不存在的事件都返回 False，不泄露其他用户的事实。
        """
        if type(unresolved_event_id) is not int or unresolved_event_id <= 0:
            raise ValueError("unresolved_event_id must be positive")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
            .with_for_update()
        )
        if connection is None:
            return False
        original = await self._session.scalar(
            select(AuditEventModel.id).where(
                AuditEventModel.id == unresolved_event_id,
                AuditEventModel.user_id == user_id,
                AuditEventModel.event_type == "oauth.revoke_unresolved",
                AuditEventModel.event_metadata["connection_id"].astext == str(connection_id),
                AuditEventModel.event_metadata["provider"].astext == connection.provider,
            )
        )
        if original is None:
            return False
        resolved = await self._session.scalar(
            select(AuditEventModel.id).where(
                AuditEventModel.user_id == user_id,
                AuditEventModel.event_type == "oauth.revoke_remediated",
                AuditEventModel.event_metadata["connection_id"].astext == str(connection_id),
                AuditEventModel.event_metadata["unresolved_event_id"].astext
                == str(unresolved_event_id),
            )
        )
        if resolved is not None:
            return False
        self._session.add(
            AuditEventModel(
                user_id=user_id,
                task_id=None,
                event_type="oauth.revoke_remediated",
                actor_type="user",
                actor_id=str(user_id),
                created_at=occurred_at,
                event_metadata={
                    "provider": connection.provider,
                    "connection_id": str(connection_id),
                    "unresolved_event_id": str(unresolved_event_id),
                },
            )
        )
        await self._session.flush()
        return True


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
        """分别拥有 session 与短事务；commit ACK 异常仍必须关闭/归还原连接。"""
        self._session = factory()
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> ConnectionStore:
        """进入事务并向应用层返回窄存储接口。"""
        session = await self._stack.enter_async_context(self._session)
        try:
            await self._stack.enter_async_context(session.begin())
        except BaseException:
            await self._stack.aclose()
            raise
        return SqlAlchemyConnectionStore(session)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """委托 SQLAlchemy 执行原子提交或异常回滚。"""
        return await self._stack.__aexit__(exc_type, exc_value, traceback)
