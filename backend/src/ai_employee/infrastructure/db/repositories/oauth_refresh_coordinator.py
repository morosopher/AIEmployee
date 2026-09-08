"""用 PostgreSQL session lease、append-only fence 和唯一 CAS writer 协调自动 OAuth refresh。

lease connection 跨网络保持同一物理会话，每个业务事务独立且短暂。任何 started 之后
的未知结果只读核对，绝不把异常解释为再次 grant 的许可；ACK-loss 不依赖丢失的 lease。
"""

import hashlib
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from ai_employee.application.calendar_aad_digests import (
    canonical_utc,
    connection_digest_v1,
    credential_snapshot_digest_v1,
    refresh_credential_snapshot_digest_v1,
)
from ai_employee.application.oauth_refresh_identity import (
    OAuthRefreshIdentity,
    validated_token_text,
)
from ai_employee.application.ports.credential_rotation import (
    ConfirmedV1,
    CredentialReplacedV1,
    OAuthRefreshAuditRecord,
    OAuthRefreshStartedV1,
    RecoveryUnsatisfiedV1,
    parse_automatic_started,
    parse_oauth_refresh_result,
)
from ai_employee.application.ports.encryption import (
    Encryption,
    EncryptionBoundaryError,
    EncryptionKeyVersionError,
)
from ai_employee.application.ports.oauth import OAuthTokenSet
from ai_employee.application.ports.oauth_refresh import (
    OAuthRefreshClaim,
    OAuthRefreshClosedResult,
    OAuthRefreshError,
    OAuthRefreshLease,
    OAuthRefreshProvider,
    OAuthRefreshReady,
    OAuthRefreshRequest,
    OAuthRefreshSnapshot,
)
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories.credential_rotation import (
    SqlAlchemyCredentialRotationRepository,
    audit_record,
    credential_aad,
    encrypted_value,
    load_refresh_snapshot,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

OAUTH_REFRESH_EVENT_TYPES = (
    "oauth.refresh_started",
    "oauth.refresh_confirmed",
    "oauth.refresh_recovery_authorization_started",
    "oauth.refresh_recovery_unsatisfied",
    "oauth.refresh_credential_replaced",
)


class PostgreSQLOAuthRefreshLease:
    """持有专用 AsyncConnection 的 domain-separated bigint session lock，不允许重入获取。

    connection 只用于持锁与短 ownership 查询，不在 provider 网络阶段保留事务。
    固定 backend PID 防止 SQLAlchemy 断线后偷偷重连为新 session 再声称持有旧 lease。
    """

    def __init__(self, connection: AsyncConnection, connection_id: UUID) -> None:
        """记录连接及稳定 domain key；尚未 acquire 时不能用于 provider admission。"""
        self.connection = connection
        digest = hashlib.sha256(
            b"AIEMPLOYEE/oauth/refresh-lease/v1\x00" + str(connection_id).encode("ascii")
        ).digest()
        self._unsigned_key = int.from_bytes(digest[:8], "big")
        self._key = int.from_bytes(digest[:8], "big", signed=True)
        self._backend_pid: int | None = None
        self._acquire_result_unknown = False

    async def acquire(self) -> None:
        """执行一次非阻塞 session try-lock；失败不写 fence、不等待或读取 Secret。

        发出 SELECT 前登记结果未知；异常或取消不能被误解为未取锁，外层必须丢弃该会话。
        """
        if self._backend_pid is not None or self._acquire_result_unknown:
            raise OAuthRefreshError("oauth_refresh_claim_locked")
        self._acquire_result_unknown = True
        result = (
            await self.connection.execute(
                text("SELECT pg_backend_pid(), pg_try_advisory_lock(:key)"), {"key": self._key}
            )
        ).one()
        if result[1] is True:
            # session lock 已在 SELECT 返回时生效，不能等 commit ACK 才登记 owner。
            # ACK 丢失仍须让外层 finally 解锁，避免把持锁会话归还连接池。
            self._backend_pid = int(result[0])
        self._acquire_result_unknown = False
        await self.connection.commit()
        if result[1] is not True:
            raise OAuthRefreshError("oauth_refresh_claim_locked")

    async def assert_owned(self) -> None:
        """重读 pg_locks 的实际 owner；无法证明原 session 持锁时返回规范 claim_lost。

        不允许重入 acquire 或断线重连掩盖租约丢失；调用方只能核对持久 result。
        """
        if self.connection.closed or self.connection.invalidated or self._backend_pid is None:
            raise OAuthRefreshError("oauth_refresh_claim_lost")
        try:
            owned = await self.connection.scalar(
                text(
                    "SELECT pg_backend_pid() = :pid AND EXISTS ("
                    "SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() "
                    "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
                    "AND classid=:high AND objid=:low AND objsubid=1 AND granted AND mode='ExclusiveLock')"
                ),
                {
                    "pid": self._backend_pid,
                    "high": self._unsigned_key >> 32,
                    "low": self._unsigned_key & 0xFFFFFFFF,
                },
            )
            await self.connection.commit()
        except (SQLAlchemyError, OSError):
            raise OAuthRefreshError("oauth_refresh_claim_lost") from None
        if owned is not True:
            raise OAuthRefreshError("oauth_refresh_claim_lost")

    async def release(self) -> None:
        """已知持锁时显式 unlock；取锁结果未知或解锁异常时丢弃底层物理会话。"""
        if self.connection.closed or self.connection.invalidated:
            return
        if self._acquire_result_unknown:
            # SELECT 可能已在服务器取得 session lock，但客户端尚未收到 PID/结果。
            # rollback 不释放此类锁；不能因没有登记 owner 就把活连接归池。
            await self.connection.invalidate()
            return
        if self._backend_pid is None:
            return
        try:
            await self.connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
            )
            await self.connection.commit()
        except (SQLAlchemyError, OSError):
            await self.connection.invalidate()


async def read_refresh_events(
    session: AsyncSession, *, user_id: UUID, connection_id: UUID
) -> tuple[OAuthRefreshAuditRecord, ...]:
    """只按显式用户与固定连接 digest 读取五种版本化事件，原 credential 内容不进入查询。"""
    rows = (
        await session.scalars(
            select(AuditEventModel)
            .where(
                AuditEventModel.user_id == user_id,
                AuditEventModel.event_type.in_(OAUTH_REFRESH_EVENT_TYPES),
                AuditEventModel.event_metadata["connection_digest"].astext
                == connection_digest_v1(connection_id),
            )
            .order_by(AuditEventModel.id)
        )
    ).all()
    return tuple(audit_record(row) for row in rows)


def find_refresh_result(
    records: tuple[OAuthRefreshAuditRecord, ...],
    *,
    user_id: UUID,
    connection_id: UUID,
    attempt_id: UUID,
    key_version: int,
    recovery: bool = False,
) -> OAuthRefreshClosedResult | None:
    """以共享严格 parser 匹配允许关闭该 attempt 的 union member；不读取 current rows。

    automatic 只接受 confirmed/replacement；recovery 只接受 unsatisfied/replacement。
    两个合法结果声称关闭同一 attempt 时 fail closed，不能任意挑选较新一条。
    """
    matches: list[OAuthRefreshClosedResult] = []
    starts = [row for row in records if row.event_type == "oauth.refresh_started"]
    recoveries = [
        row for row in records if row.event_type == "oauth.refresh_recovery_authorization_started"
    ]
    results = [
        row
        for row in records
        if row.event_type
        in {
            "oauth.refresh_confirmed",
            "oauth.refresh_recovery_unsatisfied",
            "oauth.refresh_credential_replaced",
        }
    ]
    for automatic in starts:
        if not recovery and automatic.metadata.get("refresh_attempt_id") != str(attempt_id):
            continue
        for result in results:
            if result.metadata.get("refresh_attempt_id") != automatic.metadata.get(
                "refresh_attempt_id"
            ):
                continue
            linked_recovery = next(
                (
                    row
                    for row in recoveries
                    if str(row.event_id)
                    == result.metadata.get("recovery_authorization_started_event_id")
                ),
                None,
            )
            parsed = parse_oauth_refresh_result(
                result,
                automatic=automatic,
                recovery=linked_recovery,
                user_id=user_id,
                connection_id=connection_id,
                key_version=key_version,
            )
            if parsed is None:
                continue
            if recovery:
                if isinstance(parsed, ConfirmedV1) or parsed.recovery_oauth_attempt_id != str(
                    attempt_id
                ):
                    continue
            elif isinstance(parsed, RecoveryUnsatisfiedV1):
                continue
            matches.append(OAuthRefreshClosedResult(automatic, linked_recovery, result, parsed))
    if len(matches) > 1:
        raise OAuthRefreshError("oauth_credential_state_conflict")
    return matches[0] if matches else None


class SqlAlchemyOAuthRefreshCoordinator:
    """以专用 session lease 串行自动 grant，业务事务仍由 coordinator 的应用边界控制。"""

    def __init__(
        self,
        *,
        session_factory: ManagedAsyncSessionMaker,
        cipher: Encryption,
        identity: OAuthRefreshIdentity,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """注入同一 root/version、唯一进程会话工厂与可替换 UTC clock；构造不执行 I/O。"""
        if cipher.key_version != identity.key_version:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        self._sessions, self._cipher, self._identity = session_factory, cipher, identity
        self._clock = clock or (lambda: datetime.now(UTC))

    @asynccontextmanager
    async def _lease(self, connection_id: UUID) -> AsyncIterator[PostgreSQLOAuthRefreshLease]:
        """整个 attempt 使用同一物理连接；网络阶段该连接处于无事务持锁状态。"""
        async with self._sessions.engine.connect() as connection:
            lease = PostgreSQLOAuthRefreshLease(connection, connection_id)
            try:
                await lease.acquire()
                yield lease
            finally:
                await lease.release()

    @asynccontextmanager
    async def explicit_recovery_lease(
        self, *, user_id: UUID, connection_id: UUID
    ) -> AsyncIterator[PostgreSQLOAuthRefreshLease]:
        """提供 Task27B 的显式授权码 lease 原语；不消费 state、不创建 automatic started。"""
        async with self._lease(connection_id) as lease:
            async with self._sessions() as session:
                owned = await session.scalar(
                    select(OAuthConnectionModel.id).where(
                        OAuthConnectionModel.id == connection_id,
                        OAuthConnectionModel.user_id == user_id,
                        OAuthConnectionModel.status == "connected",
                    )
                )
                if owned is None:
                    raise OAuthRefreshError("oauth_credential_state_conflict")
            await lease.assert_owned()
            yield lease

    def _now(self) -> datetime:
        """拒绝非 UTC 时钟，避免宿主时区影响 expiry、deadline 与严格时间顺序。"""
        now = self._clock()
        canonical_utc(now)
        return now

    def _plaintext(self, snapshot: OAuthRefreshSnapshot) -> tuple[str, str, str]:
        """先 exact-AAD 解密并验证，再计算固定 identity；所有失败都在 started/provider 前。"""
        try:
            access = validated_token_text(
                self._cipher.decrypt(
                    encrypted_value(snapshot.access), credential_aad(snapshot.access)
                )
            )
            refresh_bytes = self._cipher.decrypt(
                encrypted_value(snapshot.refresh), credential_aad(snapshot.refresh)
            )
            refresh = validated_token_text(refresh_bytes)
            identity = self._identity.fingerprint(refresh_bytes)
        except (
            InvalidTag,
            EncryptionBoundaryError,
            EncryptionKeyVersionError,
            ValueError,
            TypeError,
        ):
            raise OAuthRefreshError("oauth_credential_state_conflict") from None
        return access, refresh, identity

    def _validate_history(
        self,
        snapshot: OAuthRefreshSnapshot,
        records: tuple[OAuthRefreshAuditRecord, ...],
        identity: str,
    ) -> None:
        """先按历史事实闭合 fence，再独立应用 changed=true 的 A→B→A guard。

        物理快照、generation、scope 或正常同明文重加密都不会重开合法闭合；只有
        changed=true 后 identity 回到 old，或非法/未决历史才阻止新的 automatic admission。
        """
        for started in records:
            if started.event_type != "oauth.refresh_started":
                continue
            parsed = parse_automatic_started(
                started,
                user_id=snapshot.user_id,
                connection_id=snapshot.connection_id,
                key_version=self._identity.key_version,
            )
            if parsed is None:
                raise OAuthRefreshError("oauth_credential_state_conflict")
            closed = find_refresh_result(
                records,
                user_id=snapshot.user_id,
                connection_id=snapshot.connection_id,
                attempt_id=UUID(parsed.refresh_attempt_id),
                key_version=self._identity.key_version,
            )
            if closed is None:
                raise OAuthRefreshError()
            result = closed.metadata
            if (
                isinstance(result, (ConfirmedV1, CredentialReplacedV1))
                and result.refresh_identity_changed
                and secrets.compare_digest(identity, result.old_refresh_token_identity_v1)
            ):
                raise OAuthRefreshError("oauth_credential_state_conflict")

    async def read_current(self, request: OAuthRefreshRequest) -> OAuthRefreshReady:
        """从同一短只读快照校验 credentials 与历史，expiry 仍由使用方判断。

        无 lease 的读取可能与合法 refresh 提交交错；固定事务快照，避免把旧凭据
        与新 confirmed 拼成虚假的 A→B→A。返回快照不替代使用方的当前状态准入检查。
        """
        async with self._sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            snapshot, _, _ = await load_refresh_snapshot(session, request, lock=False)
            records = await read_refresh_events(
                session, user_id=request.user_id, connection_id=request.connection_id
            )
            access, _, identity = self._plaintext(snapshot)
            self._validate_history(snapshot, records, identity)
        return OAuthRefreshReady(snapshot, access)

    async def read_result(
        self, *, user_id: UUID, connection_id: UUID, attempt_id: UUID, recovery: bool = False
    ) -> OAuthRefreshClosedResult | None:
        """丢失原 session/lease 后只用新 session 查询合法历史 union，不猜测或补写结果。"""
        async with self._sessions() as session, session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY"))
            records = await read_refresh_events(
                session, user_id=user_id, connection_id=connection_id
            )
        return find_refresh_result(
            records,
            user_id=user_id,
            connection_id=connection_id,
            attempt_id=attempt_id,
            key_version=self._identity.key_version,
            recovery=recovery,
        )

    async def refresh(
        self,
        request: OAuthRefreshRequest,
        provider: OAuthRefreshProvider,
        *,
        outer_lease: OAuthRefreshLease | None = None,
    ) -> OAuthRefreshReady:
        """唯一 automatic grant 路径：已提交 started→一次网络→完整 CAS/confirmed。

        started 之后所有异常均先只读核对；实际 commit 可恢复。无合法结果时只保留已知
        lease/CAS 分类，其余 rollback/未知结果保持 unknown，不能重新发送旧 refresh。
        outer_lease 供封闭 rollout 注入 revision-global lease；普通 Worker/API 省略它。
        两层检查覆盖行锁等待之后的 started 与 confirmed 提交，不能由 provider wrapper
        代替。已提交合法结果仍优先闭合历史，外层 rollout 独立决定能否继续发布。
        """
        async with self._lease(request.connection_id) as lease:

            async def assert_leases() -> None:
                """在同一物理会话重读两层 ownership，不重入 acquire 或隐藏已丢失租约。"""
                await lease.assert_owned()
                if outer_lease is not None:
                    await outer_lease.assert_owned()

            await assert_leases()
            # session 与事务分别管理：commit ACK 异常仍执行外层 close，不能遗留池连接。
            async with self._sessions() as session, session.begin():
                snapshot, _, _ = await load_refresh_snapshot(session, request, lock=True)
                if provider.provider != snapshot.provider:
                    raise OAuthRefreshError("oauth_credential_state_conflict")
                records = await read_refresh_events(
                    session, user_id=request.user_id, connection_id=request.connection_id
                )
                access, refresh, identity = self._plaintext(snapshot)
                self._validate_history(snapshot, records, identity)
                # load_refresh_snapshot 可能长时间等锁；在任何新 started 前重证两层租约。
                await assert_leases()
                if request.attempt_id is not None:
                    # 可信写 401 使用 execution/write-attempt 派生的稳定 UUID。持久结果
                    # 已存在时只恢复当前 readiness；崩溃后不能生成第二个 refresh grant。
                    closed = find_refresh_result(
                        records,
                        user_id=request.user_id,
                        connection_id=request.connection_id,
                        attempt_id=request.attempt_id,
                        key_version=self._identity.key_version,
                    )
                    if closed is not None:
                        original = parse_automatic_started(
                            closed.automatic,
                            user_id=request.user_id,
                            connection_id=request.connection_id,
                            key_version=self._identity.key_version,
                        )
                        if (
                            original is None
                            or original.source != request.source
                            or original.rollout_digest_v1 != request.rollout_digest_v1
                        ):
                            raise OAuthRefreshError("oauth_credential_state_conflict")
                        return OAuthRefreshReady(
                            snapshot, access, refreshed=True, attempt_id=request.attempt_id
                        )
                # 两个 reader 可能先后拿到同一旧 access 快照。赢家已提交更晚 credentials
                # 时，后到的 401 使用当前事实，不再发出另一 grant；未决 fence 仍优先阻断。
                if (
                    request.expected_access is not None
                    and request.expected_access != snapshot.access
                ):
                    return OAuthRefreshReady(snapshot, access)
                fence = OAuthRefreshStartedV1(
                    fence_schema_version="oauth_refresh_fence.v1",
                    source=request.source,
                    refresh_attempt_id=str(request.attempt_id or uuid4()),
                    connection_digest=connection_digest_v1(request.connection_id),
                    source_revision="20260809_0018"
                    if request.source == "calendar_aad_preflight"
                    else None,
                    target_revision="20260809_0019"
                    if request.source == "calendar_aad_preflight"
                    else None,
                    rollout_digest_v1=request.rollout_digest_v1,
                    fence_generation=snapshot.authorization_generation,
                    pre_credential_snapshot_digest_v1=credential_snapshot_digest_v1(
                        snapshot.access, snapshot.refresh
                    ),
                    pre_refresh_credential_snapshot_digest_v1=refresh_credential_snapshot_digest_v1(
                        snapshot.refresh
                    ),
                    old_refresh_token_identity_v1=identity,
                    refresh_token_identity_key_version=self._identity.key_version,
                    result_code="oauth_refresh_started",
                )
                started = AuditEventModel(
                    user_id=request.user_id,
                    task_id=None,
                    actor_type="system",
                    actor_id=None,
                    event_type="oauth.refresh_started",
                    event_metadata=fence.metadata(),
                    created_at=self._now(),
                )
                session.add(started)
                await session.flush()
                claim = OAuthRefreshClaim(request, snapshot, audit_record(started), fence, refresh)
                await assert_leases()
            # 此时 started 的 commit 已获 ACK；不在业务事务里等待 provider。
            attempt_id = UUID(fence.refresh_attempt_id)
            try:
                await assert_leases()
                tokens = await provider.refresh(claim.refresh_token)
                await assert_leases()
                self._validate_response(claim, provider, tokens)
                async with self._sessions() as session, session.begin():
                    await assert_leases()
                    await SqlAlchemyCredentialRotationRepository(
                        session, cipher=self._cipher, identity=self._identity
                    ).confirm(claim, tokens, completed_at=self._now())
                    await assert_leases()
            except Exception as error:  # started 后只能查询持久结果，不能重放。
                closed = await self.read_result(
                    user_id=request.user_id,
                    connection_id=request.connection_id,
                    attempt_id=attempt_id,
                )
                if closed is None:
                    if isinstance(error, OAuthRefreshError) and error.error_code in {
                        "oauth_refresh_claim_locked",
                        "oauth_refresh_claim_lost",
                        "oauth_credential_state_conflict",
                    }:
                        # 已提交 result 优先；只有不存在合法 closure 时才保留已证实的原因。
                        raise error from None
                    raise OAuthRefreshError() from None
                # 查询永远使用新 session；原 lease 丢失不会使已提交结果复活或阻止 ACK 核对。
                ready = await self.read_current(request)
                return replace(ready, refreshed=True, attempt_id=attempt_id)
            ready = await self.read_current(request)
            return replace(ready, refreshed=True, attempt_id=attempt_id)

    def _validate_response(
        self, claim: OAuthRefreshClaim, provider: OAuthRefreshProvider, tokens: OAuthTokenSet
    ) -> None:
        """重验 provider-neutral response 与实际 scope 覆盖；未知/缩水结果不能产生 confirmed。"""
        if type(tokens) is not OAuthTokenSet:
            raise OAuthRefreshError()
        tokens.__post_init__()
        base_scopes = provider.scopes_for(frozenset())
        required = provider.scopes_for(frozenset({claim.request.capability})) - base_scopes
        permitted = provider.scopes_for(frozenset(ConnectionCapability))
        if not (claim.snapshot.scopes | required).issubset(
            tokens.granted_scopes
        ) or not tokens.granted_scopes.issubset(permitted):
            raise OAuthRefreshError()
        if claim.request.source == "calendar_aad_preflight" and tokens.expires_in <= 900:
            raise OAuthRefreshError()
        canonical_utc(self._now() + timedelta(seconds=tokens.expires_in))
