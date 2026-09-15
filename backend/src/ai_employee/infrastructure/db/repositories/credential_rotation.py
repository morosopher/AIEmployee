"""实现已有连接自动刷新与显式恢复唯一的双行 credential CAS writer。

调用方拥有短事务及 commit；本仓储不执行网络、不隐式创建缺少的行，也不提供 upsert。
锁序固定 connection→access→refresh→original audit→recovery audit；审计使用共享事务
advisory mutex，不需要 audit UPDATE 权限。凭据与 confirmed/replacement 必须同事务提交，
未满足恢复只关闭对应 OAuthAttempt，保留原 automatic fence。
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidTag
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.calendar_aad_digests import (
    CredentialSnapshot,
    canonical_utc,
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
    OAuthRecoveryStartedV1,
    OAuthRefreshAuditRecord,
    OAuthRefreshStartedV1,
    RecoveryCapabilityTransition,
    RecoveryFailureCode,
    RecoveryUnsatisfiedV1,
    parse_automatic_started,
    parse_recovery_started,
)
from ai_employee.application.ports.encryption import (
    EncryptedValue,
    Encryption,
    EncryptionBoundaryError,
    EncryptionKeyVersionError,
)
from ai_employee.application.ports.oauth import OAuthProvider, OAuthTokenSet
from ai_employee.application.ports.oauth_refresh import (
    OAuthRecoveryAuthorization,
    OAuthRecoveryCandidate,
    OAuthRecoveryClaim,
    OAuthRecoveryRequest,
    OAuthRefreshClaim,
    OAuthRefreshError,
    OAuthRefreshRequest,
    OAuthRefreshSnapshot,
)
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthAttemptModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel


def credential_snapshot(row: EncryptedCredentialModel) -> CredentialSnapshot:
    """复制全部九列为不可变投影；缺失或未知 credential kind 不可被猜测修正。"""
    kind: Literal["access_token", "refresh_token"]
    if row.credential_kind == "access_token":
        kind = "access_token"
    elif row.credential_kind == "refresh_token":
        kind = "refresh_token"
    else:
        raise OAuthRefreshError("oauth_credential_state_conflict")
    return CredentialSnapshot(
        id=row.id,
        user_id=row.user_id,
        connection_id=row.connection_id,
        credential_kind=kind,
        ciphertext=row.ciphertext,
        nonce=row.nonce,
        key_version=row.key_version,
        token_expires_at=row.token_expires_at,
        updated_at=row.updated_at,
    )


def audit_record(row: AuditEventModel) -> OAuthRefreshAuditRecord:
    """把现有 AuditEvent 投影为严格 parser 可读取的无 ORM 值对象。"""
    return OAuthRefreshAuditRecord(
        event_id=row.id,
        user_id=row.user_id,
        event_type=row.event_type,
        created_at=row.created_at,
        metadata=dict(row.event_metadata),
        task_id=row.task_id,
        actor_type=row.actor_type,
    )


def credential_aad(snapshot: CredentialSnapshot) -> bytes:
    """保持 OAuth callback 的精确 user:connection:kind AAD，不能跨连接复用密文。"""
    return f"{snapshot.user_id}:{snapshot.connection_id}:{snapshot.credential_kind}".encode("ascii")


def encrypted_value(snapshot: CredentialSnapshot) -> EncryptedValue:
    """把物理投影收窄为 AEAD 原语的认证输入，不解密或记录内容。"""
    return EncryptedValue(snapshot.ciphertext, snapshot.nonce, snapshot.key_version)


async def lock_refresh_audit_event(session: AsyncSession, *, event_id: int) -> None:
    """取得按 AuditEvent.id 隔离的事务互斥，供自动 writer、恢复和清理共用。

    Args:
        session: writer 已按 connection→access→refresh 完成行锁的短事务；retention/privacy
            按清理协议先取得两表 EXCLUSIVE NOWAIT 的更强物理保护并依该顺序读取身份。
        event_id: 待确认或清理的既有 started 审计事件主键，必须为正整数。

    审计表只授予应用角色 SELECT/INSERT，不能使用需要 UPDATE 权限的物理行锁。
    所有修改该事件关闭关系的路径必须共用本 domain-separated advisory mutex，
    随后 fresh read 并严格比较 started；事务结束自动释放，不跨 provider 网络。
    """
    if type(event_id) is not int or event_id <= 0:
        raise OAuthRefreshError("oauth_credential_state_conflict")
    digest = hashlib.sha256(
        b"AIEMPLOYEE/oauth/refresh-audit-event/v1\x00" + str(event_id).encode("ascii")
    ).digest()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int.from_bytes(digest[:8], "big", signed=True)},
    )


async def lock_oauth_user(session: AsyncSession, *, user_id: UUID) -> bool:
    """在 OAuth 行锁或用户外键写入之前同步所属 user，返回锁后的活动状态。

    Args:
        session: 调用方拥有的短事务；涉及可信任务时必须已先取得 TaskRun 锁。
        user_id: 来自已验证请求或仅用于定位的 attempt 归属，不能由供应商响应改绑。

    Returns:
        只有存在且活动的用户返回 True。调用方保留各自已有 active 拒绝与错误分类；
        state 消费在锁后检查该值，以既有 state 拒绝协议阻断已提交删除屏障的用户；
        其他失败关闭路径仍复用同一用户锁，不新增授权状态或错误类型。

    此锁与 claim/request-start 的 user FOR UPDATE 同强度，避免持 OAuth 资源后再被
    OAuthAttempt/AuditEvent 的 user 外键 KEY SHARE 阻塞。锁持续到原事务结束，不跨网络。
    """
    active = await session.scalar(
        select(UserModel.is_active).where(UserModel.id == user_id).with_for_update()
    )
    return active is True


async def load_refresh_snapshot(
    session: AsyncSession,
    request: OAuthRefreshRequest | OAuthRecoveryRequest,
    *,
    lock: bool,
) -> tuple[OAuthRefreshSnapshot, EncryptedCredentialModel, EncryptedCredentialModel]:
    """显式用户过滤并按固定顺序读取/锁定完整凭据与当前连接能力。

    Args:
        session: 调用方拥有的短事务；本函数不提交或执行网络。
        request: automatic 请求校验 enabled 能力；recovery 请求只定位当前连接凭据。
        lock: 写入/恢复准入时为 True，按 user→connection→access→refresh 加行锁。

    Returns:
        不可变 snapshot 与仍在当前事务内的 access/refresh ORM 行；ORM 不离开仓储层。

    Raises:
        OAuthRefreshError: 连接、用户、能力、scope 或完整双行不满足当前状态不变量。
    """
    # 所有写入 snapshot 的后续 started/result 都可能触发 user 外键检查，必须在
    # 第一把连接锁之前取得 user；只读 readiness 保持普通 SELECT，不升级其锁模式。
    active = await lock_oauth_user(session, user_id=request.user_id) if lock else None
    query = select(OAuthConnectionModel).where(
        OAuthConnectionModel.id == request.connection_id,
        OAuthConnectionModel.user_id == request.user_id,
    )
    connection = await session.scalar(query.with_for_update() if lock else query)
    if connection is None or connection.status != "connected":
        raise OAuthRefreshError("oauth_credential_state_conflict")
    if not lock:
        active = await session.scalar(
            select(UserModel.is_active).where(UserModel.id == request.user_id)
        )
    capability = None
    if isinstance(request, OAuthRefreshRequest):
        capability = await session.scalar(
            select(ConnectionCapabilityModel).where(
                ConnectionCapabilityModel.user_id == request.user_id,
                ConnectionCapabilityModel.connection_id == request.connection_id,
                ConnectionCapabilityModel.capability == request.capability.value,
            )
        )
        if capability is None or capability.status != "enabled":
            raise OAuthRefreshError("oauth_credential_state_conflict")
    # 恢复 start 已把 requested 状态改为 authorizing，不能借 automatic 的 enabled 检查
    # 阻断用户授权码；用户、connected、scope、完整双行与 AEAD 仍逐项校验。
    if active is not True:
        raise OAuthRefreshError("oauth_credential_state_conflict")
    scopes = connection.scopes
    if (
        not isinstance(scopes, list)
        or not scopes
        or any(type(scope) is not str or not scope or scope.strip() != scope for scope in scopes)
    ):
        raise OAuthRefreshError("oauth_credential_state_conflict")
    if len(scopes) != len(set(scopes)) or (
        capability is not None and not set(capability.actual_scopes).issubset(scopes)
    ):
        raise OAuthRefreshError("oauth_credential_state_conflict")
    rows = []
    for kind in ("access_token", "refresh_token"):
        statement = select(EncryptedCredentialModel).where(
            EncryptedCredentialModel.connection_id == request.connection_id,
            EncryptedCredentialModel.user_id == request.user_id,
            EncryptedCredentialModel.credential_kind == kind,
        )
        row = await session.scalar(statement.with_for_update() if lock else statement)
        if row is None:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        rows.append(row)
    access, refresh = rows
    try:
        provider = OAuthProvider(connection.provider)
        snapshot = OAuthRefreshSnapshot(
            user_id=request.user_id,
            connection_id=request.connection_id,
            provider=provider,
            authorization_generation=connection.authorization_generation,
            scopes=frozenset(scopes),
            access=credential_snapshot(access),
            refresh=credential_snapshot(refresh),
        )
        credential_snapshot_digest_v1(snapshot.access, snapshot.refresh)
        if (
            snapshot.access.token_expires_at is None
            or snapshot.refresh.token_expires_at is not None
        ):
            raise ValueError("credential expiry state is invalid")
    except (ValueError, TypeError):
        raise OAuthRefreshError("oauth_credential_state_conflict") from None
    return snapshot, access, refresh


async def _refresh_events(
    session: AsyncSession, *, user_id: UUID, connection_id: UUID
) -> tuple[OAuthRefreshAuditRecord, ...]:
    """复用 Task27A 的五事件查询；延迟导入仅用于打破同一适配器内 writer/coordinator 依赖环。"""
    from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
        read_refresh_events,
    )

    return await read_refresh_events(session, user_id=user_id, connection_id=connection_id)


def _unresolved_fences(
    records: tuple[OAuthRefreshAuditRecord, ...],
    *,
    user_id: UUID,
    connection_id: UUID,
    key_version: int | None,
    current_identity: str | None = None,
) -> tuple[tuple[OAuthRefreshAuditRecord, OAuthRefreshStartedV1], ...]:
    """使用共享严格 union matcher 找未决 fence，同时保留已闭合 changed=true 的回滚防线。"""
    from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
        find_refresh_result,
    )

    if records and key_version is None:
        raise OAuthRefreshError("oauth_credential_state_conflict")
    unresolved = []
    for record in records:
        if record.event_type != "oauth.refresh_started":
            continue
        if key_version is None:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        parsed = parse_automatic_started(
            record, user_id=user_id, connection_id=connection_id, key_version=key_version
        )
        if parsed is None:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        closed = find_refresh_result(
            records,
            user_id=user_id,
            connection_id=connection_id,
            attempt_id=UUID(parsed.refresh_attempt_id),
            key_version=key_version,
        )
        if closed is None:
            unresolved.append((record, parsed))
        elif (
            current_identity is not None
            and isinstance(closed.metadata, (ConfirmedV1, CredentialReplacedV1))
            and closed.metadata.refresh_identity_changed
            and secrets.compare_digest(
                current_identity, closed.metadata.old_refresh_token_identity_v1
            )
        ):
            raise OAuthRefreshError("oauth_credential_state_conflict")
    return tuple(unresolved)


async def assert_connection_unfenced(
    session: AsyncSession, *, user_id: UUID, connection_id: UUID, key_version: int | None
) -> None:
    """普通 OAuth 保存前检查已锁定连接，禁止身份合并绕过未决自动 fence。

    Args:
        session: 调用方已持连接行锁的同一短事务。
        user_id: attempt 对应用户，所有事件查询继续显式限定归属。
        connection_id: 规范身份解析出的既有连接。
        key_version: 当前 identity 根版本；存在历史时禁止缺省推断。

    Raises:
        StateConflictError: 存在未决 fence，必须从原连接重新发起显式恢复。
        OAuthRefreshError: 已有历史的严格 schema 或固定 key version 验证失败。
    """
    records = await _refresh_events(session, user_id=user_id, connection_id=connection_id)
    if _unresolved_fences(
        records, user_id=user_id, connection_id=connection_id, key_version=key_version
    ):
        raise StateConflictError(
            error_code="oauth_refresh_recovery_requires_connection_start",
            message="Start OAuth recovery on the existing connection",
        )


class SqlAlchemyCredentialRotationRepository:
    """在调用方事务中按完整物理 snapshot 写入自动或显式恢复结果，绝不自行提交。"""

    def __init__(
        self, session: AsyncSession, *, cipher: Encryption, identity: OAuthRefreshIdentity
    ) -> None:
        """使用同一 root/version 的 cipher 与 fingerprint，不拥有 session/commit 生命周期。"""
        self.session = session
        self._cipher = cipher
        self._identity = identity
        if cipher.key_version != identity.key_version:
            raise OAuthRefreshError("oauth_credential_state_conflict")

    async def confirm(
        self, claim: OAuthRefreshClaim, tokens: OAuthTokenSet, *, completed_at: datetime
    ) -> ConfirmedV1:
        """完整 CAS 后原子写密文和 matching confirmed；missing/same 逐字节保留 refresh row。

        任何 CAS/identity/fence 不一致都抛出稳定冲突，由外层事务完整回滚。不同 token
        只确认本次 started，绝不创建 recovery consumption 或消费更早的 unknown fence。
        """
        snapshot, access, refresh = await load_refresh_snapshot(
            self.session, claim.request, lock=True
        )
        if snapshot != claim.snapshot:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        await lock_refresh_audit_event(self.session, event_id=claim.started.event_id)
        started_row = await self.session.scalar(
            select(AuditEventModel)
            .where(
                AuditEventModel.id == claim.started.event_id,
                AuditEventModel.user_id == claim.request.user_id,
                AuditEventModel.task_id.is_(None),
                AuditEventModel.event_type == "oauth.refresh_started",
            )
            .execution_options(populate_existing=True)
        )
        if started_row is None:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        started = audit_record(started_row)
        fence = parse_automatic_started(
            started,
            user_id=claim.request.user_id,
            connection_id=claim.request.connection_id,
            key_version=self._identity.key_version,
        )
        if fence != claim.fence or started != claim.started:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        existing = await self.session.scalar(
            select(AuditEventModel.id).where(
                AuditEventModel.user_id == claim.request.user_id,
                AuditEventModel.event_type.in_(
                    ("oauth.refresh_confirmed", "oauth.refresh_credential_replaced")
                ),
                AuditEventModel.event_metadata["refresh_attempt_id"].astext
                == claim.fence.refresh_attempt_id,
            )
        )
        if existing is not None:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        old_plaintext = self._cipher.decrypt(
            encrypted_value(snapshot.refresh), credential_aad(snapshot.refresh)
        )
        old_identity = self._identity.fingerprint(old_plaintext)
        if not secrets.compare_digest(old_identity, claim.fence.old_refresh_token_identity_v1):
            raise OAuthRefreshError("oauth_credential_state_conflict")
        new_plaintext = (
            tokens.refresh_token.encode("utf-8") if tokens.refresh_token is not None else None
        )
        disposition: Literal["missing", "same", "different"] = "missing"
        new_identity = old_identity
        if new_plaintext is not None:
            validated_token_text(new_plaintext)
            disposition = (
                "same" if secrets.compare_digest(old_plaintext, new_plaintext) else "different"
            )
            new_identity = self._identity.fingerprint(new_plaintext)
        changed = disposition == "different"
        expires_at = completed_at + timedelta(seconds=tokens.expires_in)
        created_at = max(completed_at, started.created_at + timedelta(microseconds=1))
        post_access, post_refresh = await self._write_credentials(
            snapshot,
            access,
            refresh,
            tokens,
            new_refresh_plaintext=new_plaintext if changed else None,
            completed_at=completed_at,
        )
        result = ConfirmedV1(
            result_schema_version="oauth_refresh_confirmed.v1",
            source=claim.fence.source,
            started_source=claim.fence.source,
            refresh_attempt_id=claim.fence.refresh_attempt_id,
            connection_digest=claim.fence.connection_digest,
            source_revision=claim.fence.source_revision,
            target_revision=claim.fence.target_revision,
            rollout_digest_v1=claim.fence.rollout_digest_v1,
            fence_generation=claim.fence.fence_generation,
            source_generation=claim.fence.fence_generation,
            pre_generation=claim.fence.fence_generation,
            post_generation=claim.fence.fence_generation,
            pre_credential_snapshot_digest_v1=claim.fence.pre_credential_snapshot_digest_v1,
            pre_refresh_credential_snapshot_digest_v1=claim.fence.pre_refresh_credential_snapshot_digest_v1,
            post_credential_snapshot_digest_v1=credential_snapshot_digest_v1(
                post_access, post_refresh
            ),
            post_refresh_credential_snapshot_digest_v1=refresh_credential_snapshot_digest_v1(
                post_refresh
            ),
            old_refresh_token_identity_v1=old_identity,
            new_refresh_token_identity_v1=new_identity,
            refresh_token_identity_key_version=self._identity.key_version,
            new_refresh_token_identity_key_version=self._identity.key_version,
            token_expires_at=canonical_utc(expires_at),
            refresh_token_disposition=disposition,
            refresh_identity_changed=changed,
            rollout_deadline_candidate=canonical_utc(expires_at - timedelta(seconds=900))
            if claim.request.source == "calendar_aad_preflight"
            else None,
            result_code="oauth_refresh_confirmed",
        )
        self.session.add(
            AuditEventModel(
                user_id=claim.request.user_id,
                task_id=None,
                event_type="oauth.refresh_confirmed",
                actor_type="system",
                actor_id=None,
                event_metadata=result.metadata(),
                created_at=created_at,
            )
        )
        await self.session.flush()
        return result

    async def _write_credentials(
        self,
        snapshot: OAuthRefreshSnapshot,
        access: EncryptedCredentialModel,
        refresh: EncryptedCredentialModel,
        tokens: OAuthTokenSet,
        *,
        new_refresh_plaintext: bytes | None,
        completed_at: datetime,
    ) -> tuple[CredentialSnapshot, CredentialSnapshot]:
        """唯一双行密文写入实现，自动与显式恢复共用相同 AEAD/expiry 规则。

        Args:
            snapshot: 调用方已成功 CAS 的完整物理快照，提供精确记录 AAD。
            access: 当前事务持锁的 access 行。
            refresh: 当前事务持锁的 refresh 行。
            tokens: 已校验的规范响应；本函数不能自行判断供应商结果是否可信。
            new_refresh_plaintext: 不同时才写入；None 保留旧 refresh 的全部物理字段。
            completed_at: 显式 UTC 持久时间，access 到期按规范响应计算。

        Returns:
            flush 后的两行不可变投影，供同事务结果事件绑定 post-digest。
        """
        encrypted_access = self._cipher.encrypt(
            tokens.access_token.encode("utf-8"), credential_aad(snapshot.access)
        )
        access.ciphertext, access.nonce, access.key_version = (
            encrypted_access.ciphertext,
            encrypted_access.nonce,
            encrypted_access.key_version,
        )
        access.token_expires_at = completed_at + timedelta(seconds=tokens.expires_in)
        access.updated_at = completed_at
        if new_refresh_plaintext is not None:
            encrypted_refresh = self._cipher.encrypt(
                new_refresh_plaintext, credential_aad(snapshot.refresh)
            )
            refresh.ciphertext, refresh.nonce, refresh.key_version = (
                encrypted_refresh.ciphertext,
                encrypted_refresh.nonce,
                encrypted_refresh.key_version,
            )
            refresh.updated_at = completed_at
        await self.session.flush()
        return credential_snapshot(access), credential_snapshot(refresh)

    def _plaintext(self, snapshot: OAuthRefreshSnapshot) -> tuple[bytes, str]:
        """先验证两行 exact-AAD/UTF-8/边界，再计算固定版本 identity，失败不回显输入。"""
        try:
            validated_token_text(
                self._cipher.decrypt(
                    encrypted_value(snapshot.access), credential_aad(snapshot.access)
                )
            )
            plaintext = self._cipher.decrypt(
                encrypted_value(snapshot.refresh), credential_aad(snapshot.refresh)
            )
            validated_token_text(plaintext)
            return plaintext, self._identity.fingerprint(plaintext)
        except (
            InvalidTag,
            EncryptionBoundaryError,
            EncryptionKeyVersionError,
            ValueError,
            TypeError,
        ):
            raise OAuthRefreshError("oauth_credential_state_conflict") from None

    async def recovery_candidate(
        self, *, user_id: UUID, connection_id: UUID
    ) -> OAuthRecoveryCandidate | None:
        """锁定当前连接/双行后，用 immutable closure 判定恰好一个原 fence。

        当前 identity 必须等于 old 且 S≥F；原摘要只绑定历史 fence，不拒绝之前合法的
        同明文重加密。当前完整 snapshot 会在 callback 网络前另行冻结，供网络后 CAS。

        Args:
            user_id: 显式发起渐进授权的连接归属用户。
            connection_id: 既有 connected 连接 ID。

        Returns:
            同事务持锁的当前 S/凭据/原 fence 候选；没有未决 fence 返回 None。

        Raises:
            OAuthRefreshError: AEAD/归属/固定 identity/代际/唯一 fence 任一不满足。
        """
        snapshot, _, _ = await load_refresh_snapshot(
            self.session, OAuthRecoveryRequest(user_id, connection_id), lock=True
        )
        plaintext, identity = self._plaintext(snapshot)
        records = await _refresh_events(self.session, user_id=user_id, connection_id=connection_id)
        unresolved = _unresolved_fences(
            records,
            user_id=user_id,
            connection_id=connection_id,
            key_version=self._identity.key_version,
            current_identity=identity,
        )
        if not unresolved:
            return None
        if len(unresolved) != 1:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        automatic, fence = unresolved[0]
        await lock_refresh_audit_event(self.session, event_id=automatic.event_id)
        records = await _refresh_events(self.session, user_id=user_id, connection_id=connection_id)
        if _unresolved_fences(
            records,
            user_id=user_id,
            connection_id=connection_id,
            key_version=self._identity.key_version,
            current_identity=identity,
        ) != unresolved or (
            snapshot.authorization_generation < fence.fence_generation
            or not secrets.compare_digest(identity, fence.old_refresh_token_identity_v1)
        ):
            raise OAuthRefreshError("oauth_credential_state_conflict")
        return OAuthRecoveryCandidate(snapshot, automatic, fence, plaintext)

    async def start_recovery(
        self,
        candidate: OAuthRecoveryCandidate,
        *,
        attempt_id: UUID,
        target_generation: int,
        occurred_at: datetime,
    ) -> OAuthRecoveryAuthorization:
        """复用 start 已持有的 connection→双行→原 audit 锁，原子绑定现有 attempt 的 T。

        Args:
            candidate: 同事务已验证的当前 S 与原 fence，不得在网络后重新构造。
            attempt_id: 既有渐进流程已创建的 connection-bound OAuthAttempt。
            target_generation: 该流程唯一一次 S→T 的结果。
            occurred_at: 显式 UTC 时间；相等时以微秒保证严格晚于原 started。

        Returns:
            新恢复 started 与原 automatic 的内存关联；本方法不拥有 commit。

        Raises:
            OAuthRefreshError: 当前 generation 或真实 attempt 不能证明 T=S+1。
        """
        snapshot, fence = candidate.snapshot, candidate.fence
        current_generation = await self.session.scalar(
            select(OAuthConnectionModel.authorization_generation).where(
                OAuthConnectionModel.user_id == snapshot.user_id,
                OAuthConnectionModel.id == snapshot.connection_id,
                OAuthConnectionModel.status == "connected",
            )
        )
        if (
            target_generation != snapshot.authorization_generation + 1
            or current_generation != target_generation
        ):
            raise OAuthRefreshError("oauth_credential_state_conflict")
        metadata = OAuthRecoveryStartedV1(
            recovery_schema_version="oauth_refresh_recovery_authorization.v1",
            source="progressive_recovery",
            oauth_attempt_id=str(attempt_id),
            refresh_attempt_id=fence.refresh_attempt_id,
            started_source=fence.source,
            connection_digest=fence.connection_digest,
            fence_generation=fence.fence_generation,
            source_generation=snapshot.authorization_generation,
            target_generation=target_generation,
            pre_credential_snapshot_digest_v1=fence.pre_credential_snapshot_digest_v1,
            pre_refresh_credential_snapshot_digest_v1=fence.pre_refresh_credential_snapshot_digest_v1,
            old_refresh_token_identity_v1=fence.old_refresh_token_identity_v1,
            refresh_token_identity_key_version=self._identity.key_version,
            result_code="oauth_refresh_recovery_authorization_started",
        )
        await self._check_attempt(
            snapshot.user_id, snapshot.connection_id, metadata, consumed=False
        )
        row = AuditEventModel(
            user_id=snapshot.user_id,
            task_id=None,
            event_type="oauth.refresh_recovery_authorization_started",
            actor_type="system",
            actor_id=None,
            event_metadata=metadata.metadata(),
            created_at=max(occurred_at, candidate.automatic.created_at + timedelta(microseconds=1)),
        )
        self.session.add(row)
        await self.session.flush()
        return OAuthRecoveryAuthorization(
            snapshot.user_id,
            snapshot.connection_id,
            candidate.automatic,
            audit_record(row),
            metadata,
        )

    async def _check_attempt(
        self,
        user_id: UUID,
        connection_id: UUID,
        metadata: OAuthRecoveryStartedV1,
        *,
        consumed: bool,
        requested_capabilities: frozenset[ConnectionCapability] | None = None,
    ) -> None:
        """查询既有 OAuthAttempt 的真实用户/目标/T，不能用外部传入 metadata 伪造恢复关联。"""
        attempt = await self.session.scalar(
            select(OAuthAttemptModel).where(
                OAuthAttemptModel.id == UUID(metadata.oauth_attempt_id),
                OAuthAttemptModel.user_id == user_id,
                OAuthAttemptModel.target_connection_id == connection_id,
            )
        )
        if (
            attempt is None
            or attempt.target_authorization_generation != metadata.target_generation
            or (attempt.consumed_at is not None) != consumed
            or attempt.invalidated_at is not None
            or not attempt.requested_capabilities
            or (
                requested_capabilities is not None
                and attempt.requested_capabilities
                != sorted(item.value for item in requested_capabilities)
            )
        ):
            raise OAuthRefreshError("oauth_credential_state_conflict")

    async def recovery_authorization(
        self, *, user_id: UUID, connection_id: UUID, attempt_id: UUID
    ) -> OAuthRecoveryAuthorization | None:
        """按 OAuthAttempt.id 读取恢复事实，用共享 parser 校验原 fence/F/S/T 与严格时序。

        Args:
            user_id: 已消费 state 对应用户。
            connection_id: attempt 冻结的精确连接。
            attempt_id: 已消费 OAuthAttempt ID，不从供应商 code 或账户邮箱推断。

        Returns:
            两条 started 的唯一合法关联；普通 attempt 没有恢复 started 时返回 None。

        Raises:
            OAuthRefreshError: 关联重复、损坏、归属错误或真实 attempt 未被消费。
        """
        records = await _refresh_events(self.session, user_id=user_id, connection_id=connection_id)
        starts = [
            row
            for row in records
            if row.event_type == "oauth.refresh_recovery_authorization_started"
            and row.metadata.get("oauth_attempt_id") == str(attempt_id)
        ]
        if not starts:
            return None
        if len(starts) != 1:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        started = starts[0]
        originals = [
            row
            for row in records
            if row.event_type == "oauth.refresh_started"
            and row.metadata.get("refresh_attempt_id") == started.metadata.get("refresh_attempt_id")
        ]
        if len(originals) != 1:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        automatic = originals[0]
        metadata = parse_recovery_started(
            started,
            automatic=automatic,
            user_id=user_id,
            connection_id=connection_id,
            key_version=self._identity.key_version,
        )
        if metadata is None:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        await self._check_attempt(user_id, connection_id, metadata, consumed=True)
        return OAuthRecoveryAuthorization(user_id, connection_id, automatic, started, metadata)

    async def _lock_authorization(
        self,
        authorization: OAuthRecoveryAuthorization,
        *,
        require_original_open: bool,
    ) -> None:
        """在 connection/access/refresh 已锁定后取得相同 audit mutex，再重读两条不可变事实。"""
        from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
            find_refresh_result,
        )

        await lock_refresh_audit_event(self.session, event_id=authorization.automatic.event_id)
        await lock_refresh_audit_event(self.session, event_id=authorization.started.event_id)
        reloaded = await self.recovery_authorization(
            user_id=authorization.user_id,
            connection_id=authorization.connection_id,
            attempt_id=UUID(authorization.metadata.oauth_attempt_id),
        )
        if reloaded != authorization:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        records = await _refresh_events(
            self.session, user_id=authorization.user_id, connection_id=authorization.connection_id
        )
        for attempt_id, recovery in (
            (authorization.metadata.oauth_attempt_id, True),
            (authorization.metadata.refresh_attempt_id, False),
        ):
            if not recovery and not require_original_open:
                continue
            if (
                find_refresh_result(
                    records,
                    user_id=authorization.user_id,
                    connection_id=authorization.connection_id,
                    attempt_id=UUID(attempt_id),
                    key_version=self._identity.key_version,
                    recovery=recovery,
                )
                is not None
            ):
                raise OAuthRefreshError("oauth_credential_state_conflict")

    async def freeze_recovery(
        self, authorization: OAuthRecoveryAuthorization
    ) -> OAuthRecoveryClaim:
        """授权码交换前按固定锁序冻结 current-T 双行，不要求等于原历史物理摘要。

        Args:
            authorization: 已消费 attempt 的严格恢复关联；调用方须持共享 session lease。

        Returns:
            网络后必须逐字段 CAS 的双行快照与受控旧明文。

        Raises:
            OAuthRefreshError: 原 fence、固定旧身份、当前 T 或两条 started 已不满足。
        """
        candidate = await self.recovery_candidate(
            user_id=authorization.user_id, connection_id=authorization.connection_id
        )
        if (
            candidate is None
            or candidate.automatic != authorization.automatic
            or candidate.snapshot.authorization_generation
            != authorization.metadata.target_generation
        ):
            raise OAuthRefreshError("oauth_credential_state_conflict")
        await self._lock_authorization(authorization, require_original_open=True)
        return OAuthRecoveryClaim(authorization, candidate.snapshot, candidate.refresh_plaintext)

    async def replace(
        self,
        claim: OAuthRecoveryClaim,
        tokens: OAuthTokenSet,
        *,
        completed_at: datetime,
    ) -> CredentialReplacedV1:
        """CAS 网络前快照，重查 T/两个 started 后写双行与 old≠new replacement proof。

        Args:
            claim: 网络前冻结的 T、完整双行与旧 refresh 明文；不得按响应重新取基线。
            tokens: 应用用例已验证规范身份和 requested scope 的响应，本层再次校验凭据。
            completed_at: 本次写入的显式 UTC 时间。

        Returns:
            绑定实际 post-digest/expiry 的 replacement 元数据；scope/能力仍由调用方
            在同一事务更新，整个事务未提交前不能解释为关闭事实。

        Raises:
            OAuthRefreshError: 完整 snapshot CAS、T、原 fence 或不同固定身份证明失败。
        """
        authorization, metadata = claim.authorization, claim.authorization.metadata
        snapshot, access, refresh = await load_refresh_snapshot(
            self.session,
            OAuthRecoveryRequest(authorization.user_id, authorization.connection_id),
            lock=True,
        )
        if (
            snapshot != claim.snapshot
            or snapshot.authorization_generation != metadata.target_generation
        ):
            raise OAuthRefreshError("oauth_credential_state_conflict")
        await self._lock_authorization(authorization, require_original_open=True)
        old_plaintext, old_identity = self._plaintext(snapshot)
        if not secrets.compare_digest(
            old_identity, metadata.old_refresh_token_identity_v1
        ) or not secrets.compare_digest(old_plaintext, claim.refresh_plaintext):
            raise OAuthRefreshError("oauth_credential_state_conflict")
        if type(tokens) is not OAuthTokenSet or tokens.refresh_token is None:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        tokens.__post_init__()
        new_plaintext = tokens.refresh_token.encode("utf-8")
        if secrets.compare_digest(old_plaintext, new_plaintext) or not snapshot.scopes.issubset(
            tokens.granted_scopes
        ):
            raise OAuthRefreshError("oauth_credential_state_conflict")
        new_identity = self._identity.fingerprint(new_plaintext)
        post_access, post_refresh = await self._write_credentials(
            snapshot,
            access,
            refresh,
            tokens,
            new_refresh_plaintext=new_plaintext,
            completed_at=completed_at,
        )
        result = CredentialReplacedV1(
            proof_schema_version="oauth_refresh_credential_replaced.v1",
            source="progressive_recovery",
            started_source=metadata.started_source,
            connection_digest=metadata.connection_digest,
            refresh_attempt_id=metadata.refresh_attempt_id,
            recovery_oauth_attempt_id=metadata.oauth_attempt_id,
            recovery_authorization_started_event_id=str(authorization.started.event_id),
            fence_generation=metadata.fence_generation,
            source_generation=metadata.source_generation,
            target_generation=metadata.target_generation,
            pre_generation=metadata.target_generation,
            post_generation=metadata.target_generation,
            pre_credential_snapshot_digest_v1=metadata.pre_credential_snapshot_digest_v1,
            pre_refresh_credential_snapshot_digest_v1=metadata.pre_refresh_credential_snapshot_digest_v1,
            post_credential_snapshot_digest_v1=credential_snapshot_digest_v1(
                post_access, post_refresh
            ),
            post_refresh_credential_snapshot_digest_v1=refresh_credential_snapshot_digest_v1(
                post_refresh
            ),
            old_refresh_token_identity_v1=old_identity,
            new_refresh_token_identity_v1=new_identity,
            refresh_token_identity_key_version=self._identity.key_version,
            new_refresh_token_identity_key_version=self._identity.key_version,
            refresh_identity_changed=True,
            token_expires_at=canonical_utc(completed_at + timedelta(seconds=tokens.expires_in)),
            result_code="oauth_refresh_credential_replaced",
        )
        await self._append_recovery_result(authorization, result, completed_at)
        return result

    async def unsatisfied(
        self,
        authorization: OAuthRecoveryAuthorization,
        *,
        requested_capabilities: frozenset[ConnectionCapability],
        capability_transition: RecoveryCapabilityTransition,
        error_code: RecoveryFailureCode,
        completed_at: datetime,
    ) -> RecoveryUnsatisfiedV1:
        """按固定锁序验证能力收敛后追加未满足结果，保留原 fence。

        Args:
            authorization: 原 automatic 与本次已消费恢复 attempt 的精确关联。
            requested_capabilities: attempt 冻结的非空规范能力集合。
            capability_transition: 同事务真实能力更新的结果，当前 T 只能 action_required。
            error_code: 固定安全分类码；不能包含供应商原始错误或凭据内容。
            completed_at: 显式 UTC 时间，追加事件严格晚于两个 started。

        Returns:
            不含 post/新 identity/expiry/deadline 的 unsatisfied 元数据；只关闭本次恢复。

        Raises:
            OAuthRefreshError: attempt、两个 started、目标 T 或实际能力状态相互矛盾。
        """
        # 此关闭路径不会经过 load_refresh_snapshot，且可由没有 session lease 的 error
        # callback 进入；必须独立在 connection/access/refresh/audit 之前同步 user。
        if not await lock_oauth_user(self.session, user_id=authorization.user_id):
            # 当前 T 和 stale T 都不得在屏障后追加普通关闭事实；session lease 仅证明
            # refresh 协调权，不能代替此短事务与删除赢家之间的用户行同步。
            raise OAuthRefreshError("oauth_credential_state_conflict")
        connection = await self.session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.user_id == authorization.user_id,
                OAuthConnectionModel.id == authorization.connection_id,
            )
            .with_for_update()
        )
        for kind in ("access_token", "refresh_token"):
            await self.session.scalar(
                select(EncryptedCredentialModel)
                .where(
                    EncryptedCredentialModel.user_id == authorization.user_id,
                    EncryptedCredentialModel.connection_id == authorization.connection_id,
                    EncryptedCredentialModel.credential_kind == kind,
                )
                .with_for_update()
            )
        await self._lock_authorization(authorization, require_original_open=False)
        metadata = authorization.metadata
        await self._check_attempt(
            authorization.user_id,
            authorization.connection_id,
            metadata,
            consumed=True,
            requested_capabilities=requested_capabilities,
        )
        is_current = (
            connection is not None
            and connection.status == "connected"
            and connection.authorization_generation == metadata.target_generation
        )
        if is_current != (capability_transition == "action_required"):
            raise OAuthRefreshError("oauth_credential_state_conflict")
        if is_current:
            rows = (
                await self.session.scalars(
                    select(ConnectionCapabilityModel).where(
                        ConnectionCapabilityModel.user_id == authorization.user_id,
                        ConnectionCapabilityModel.connection_id == authorization.connection_id,
                        ConnectionCapabilityModel.capability.in_(
                            tuple(item.value for item in requested_capabilities)
                        ),
                    )
                )
            ).all()
            if len(rows) != len(requested_capabilities) or any(
                row.status != "action_required" or row.last_error_code != error_code for row in rows
            ):
                raise OAuthRefreshError("oauth_credential_state_conflict")
        result = RecoveryUnsatisfiedV1.model_validate(
            {
                "source": "progressive_recovery",
                "started_source": metadata.started_source,
                "connection_digest": metadata.connection_digest,
                "refresh_attempt_id": metadata.refresh_attempt_id,
                "fence_generation": metadata.fence_generation,
                "source_generation": metadata.source_generation,
                "target_generation": metadata.target_generation,
                "pre_credential_snapshot_digest_v1": metadata.pre_credential_snapshot_digest_v1,
                "pre_refresh_credential_snapshot_digest_v1": metadata.pre_refresh_credential_snapshot_digest_v1,
                "old_refresh_token_identity_v1": metadata.old_refresh_token_identity_v1,
                "refresh_token_identity_key_version": metadata.refresh_token_identity_key_version,
                "recovery_oauth_attempt_id": metadata.oauth_attempt_id,
                "recovery_authorization_started_event_id": str(authorization.started.event_id),
                "result_schema_version": "oauth_refresh_recovery_unsatisfied.v1",
                "requested_capabilities": sorted(item.value for item in requested_capabilities),
                "capability_transition": capability_transition,
                "error_code": error_code,
                "result_code": "oauth_refresh_recovery_unsatisfied",
            }
        )
        await self._append_recovery_result(authorization, result, completed_at)
        return result

    async def _append_recovery_result(
        self,
        authorization: OAuthRecoveryAuthorization,
        result: RecoveryUnsatisfiedV1 | CredentialReplacedV1,
        completed_at: datetime,
    ) -> None:
        """事务内只追加内容无关结果，冻结时钟相同也保持严格晚于两个 started。"""
        self.session.add(
            AuditEventModel(
                user_id=authorization.user_id,
                task_id=None,
                actor_type="system",
                actor_id=None,
                event_type="oauth.refresh_credential_replaced"
                if isinstance(result, CredentialReplacedV1)
                else "oauth.refresh_recovery_unsatisfied",
                event_metadata=result.metadata(),
                created_at=max(
                    completed_at,
                    authorization.automatic.created_at + timedelta(microseconds=1),
                    authorization.started.created_at + timedelta(microseconds=1),
                ),
            )
        )
        await self.session.flush()
