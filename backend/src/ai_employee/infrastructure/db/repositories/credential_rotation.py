"""实现已有连接自动刷新唯一的双行 credential CAS writer。

调用方拥有短事务及 commit；本仓储不执行网络、不隐式创建缺少的行，也不提供 upsert。
锁序固定 connection→access→refresh→matching started，凭据与 confirmed 必须同事务提交。
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Literal

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
    OAuthRefreshAuditRecord,
    parse_automatic_started,
)
from ai_employee.application.ports.encryption import EncryptedValue, Encryption
from ai_employee.application.ports.oauth import OAuthProvider, OAuthTokenSet
from ai_employee.application.ports.oauth_refresh import (
    OAuthRefreshClaim,
    OAuthRefreshError,
    OAuthRefreshRequest,
    OAuthRefreshSnapshot,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
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
        session: 已按 connection→access→refresh 顺序完成行锁的短业务事务。
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


async def load_refresh_snapshot(
    session: AsyncSession,
    request: OAuthRefreshRequest,
    *,
    lock: bool,
) -> tuple[OAuthRefreshSnapshot, EncryptedCredentialModel, EncryptedCredentialModel]:
    """显式用户过滤并按固定顺序读取/锁定完整凭据与当前连接能力。

    Returns:
        不可变 snapshot 与仍在当前事务内的 access/refresh ORM 行；ORM 不离开仓储层。

    Raises:
        OAuthRefreshError: 连接、用户、能力、scope 或完整双行不满足当前状态不变量。
    """
    query = select(OAuthConnectionModel).where(
        OAuthConnectionModel.id == request.connection_id,
        OAuthConnectionModel.user_id == request.user_id,
    )
    connection = await session.scalar(query.with_for_update() if lock else query)
    if connection is None or connection.status != "connected":
        raise OAuthRefreshError("oauth_credential_state_conflict")
    active = await session.scalar(
        select(UserModel.is_active).where(UserModel.id == request.user_id)
    )
    capability = await session.scalar(
        select(ConnectionCapabilityModel).where(
            ConnectionCapabilityModel.user_id == request.user_id,
            ConnectionCapabilityModel.connection_id == request.connection_id,
            ConnectionCapabilityModel.capability == request.capability.value,
        )
    )
    if active is not True or capability is None or capability.status != "enabled":
        raise OAuthRefreshError("oauth_credential_state_conflict")
    scopes = connection.scopes
    if (
        not isinstance(scopes, list)
        or not scopes
        or any(type(scope) is not str or not scope or scope.strip() != scope for scope in scopes)
    ):
        raise OAuthRefreshError("oauth_credential_state_conflict")
    if len(scopes) != len(set(scopes)) or not set(capability.actual_scopes).issubset(scopes):
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


class SqlAlchemyCredentialRotationRepository:
    """在调用方事务中按完整物理 snapshot 与原 fence 写入唯一自动刷新结果。"""

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
        encrypted_access = self._cipher.encrypt(
            tokens.access_token.encode("utf-8"), credential_aad(snapshot.access)
        )
        access.ciphertext, access.nonce, access.key_version = (
            encrypted_access.ciphertext,
            encrypted_access.nonce,
            encrypted_access.key_version,
        )
        access.token_expires_at, access.updated_at = expires_at, completed_at
        if changed and new_plaintext is not None:
            encrypted_refresh = self._cipher.encrypt(
                new_plaintext, credential_aad(snapshot.refresh)
            )
            refresh.ciphertext, refresh.nonce, refresh.key_version = (
                encrypted_refresh.ciphertext,
                encrypted_refresh.nonce,
                encrypted_refresh.key_version,
            )
            refresh.updated_at = completed_at
        await self.session.flush()
        post_access, post_refresh = credential_snapshot(access), credential_snapshot(refresh)
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
