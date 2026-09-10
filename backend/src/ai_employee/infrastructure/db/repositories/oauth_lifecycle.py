"""以已授权的短事务物理锁清理 OAuth 历史，不加载当前 credential lineage。

SELECT/DELETE 角色没有行锁所需 UPDATE，故按冻结协议先取两表 EXCLUSIVE NOWAIT。
随后只读行身份，复用 writer 的 started 事务互斥与应用层严格 result-union parser。
本模块不取得 session lease、不提交调用方事务、不解密、不改变任何 refresh 协议。
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.credential_rotation import (
    ConfirmedV1,
    CredentialReplacedV1,
    OAuthRefreshAuditRecord,
    RecoveryUnsatisfiedV1,
    parse_automatic_started,
    parse_oauth_refresh_result,
)
from ai_employee.infrastructure.db.models.sources import (
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories.credential_rotation import lock_refresh_audit_event
from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import read_refresh_events
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


@dataclass(frozen=True, slots=True)
class OAuthCleanupIdentity:
    """受更强物理锁保护的行身份；明确不包含密文、expiry、scope 或 generation。"""

    connection_id: UUID
    access_id: UUID | None
    refresh_id: UUID | None


async def lock_oauth_cleanup_identity(
    session: AsyncSession,
    *,
    user_id: UUID,
    connection_id: UUID,
) -> OAuthCleanupIdentity | None:
    """按连接表→凭据表 NOWAIT，再按 connection→access→refresh 读取用户限定身份。

    Args:
        session: 只处理一个实际候选的短事务；调用方负责整体提交/回滚。
        user_id: 所有读取/后续删除必须显式绑定的用户。
        connection_id: 预先观察到的候选，锁后重新验证归属，不能按 provider 猜默认连接。

    Raises:
        DBAPIError: 表锁争用 SQLSTATE 55P03；调用方必须先退出事务再有界返回，不能
            忙等或继续无锁删除。其他 SQL 错误不降级为可忽略竞争。
    """
    await session.execute(text("LOCK TABLE oauth_connections IN EXCLUSIVE MODE NOWAIT"))
    await session.execute(text("LOCK TABLE encrypted_credentials IN EXCLUSIVE MODE NOWAIT"))
    owned = await session.scalar(
        select(OAuthConnectionModel.id).where(
            OAuthConnectionModel.user_id == user_id,
            OAuthConnectionModel.id == connection_id,
        )
    )
    if owned is None:
        return None
    ids: list[UUID | None] = []
    for kind in ("access_token", "refresh_token"):
        ids.append(
            await session.scalar(
                select(EncryptedCredentialModel.id).where(
                    EncryptedCredentialModel.user_id == user_id,
                    EncryptedCredentialModel.connection_id == connection_id,
                    EncryptedCredentialModel.credential_kind == kind,
                )
            )
        )
    return OAuthCleanupIdentity(owned, ids[0], ids[1])


def oauth_cleanup_lock_contended(error: DBAPIError) -> bool:
    """仅识别 PostgreSQL NOWAIT 的稳定 SQLSTATE，不回显可能携带私密参数的异常。"""
    return getattr(error.orig, "sqlstate", None) == "55P03"


async def lock_cleanup_refresh_events(
    session: AsyncSession,
    *,
    user_id: UUID,
    connection_id: UUID,
) -> tuple[OAuthRefreshAuditRecord, ...]:
    """在两表锁之后依 original→recovery 共用唯一审计 mutex，然后重新读取完整组。

    隐私删除还需清理未闭合/损坏历史，因此本函数只提供同步，不把任何格式当作
    关闭证明；普通 retention 在返回后仍必须经过同一严格 parser。
    """
    records = await read_refresh_events(session, user_id=user_id, connection_id=connection_id)
    for event_type in ("oauth.refresh_started", "oauth.refresh_recovery_authorization_started"):
        for record in records:
            if record.event_type == event_type:
                await lock_refresh_audit_event(session, event_id=record.event_id)
    # 已有 ORM identity map 不能代替锁后数据库事实；只清除本次读取的缓存，不做隐式提交。
    session.expire_all()
    return await read_refresh_events(session, user_id=user_id, connection_id=connection_id)


@dataclass(frozen=True, slots=True)
class _ClosedGroup:
    """一个共享 parser 已证明闭合的原子删除集合；unsatisfied 不包含原 automatic。"""

    automatic_id: int
    recovery_id: int | None
    delete_ids: tuple[int, ...]


def _closed_groups(
    records: tuple[OAuthRefreshAuditRecord, ...],
    *,
    user_id: UUID,
    connection_id: UUID,
    key_version: int,
    cutoff: datetime,
) -> tuple[_ClosedGroup, ...]:
    """仅编排共享 parser 的三种闭合组，不实现第二套 metadata/schema matcher。

    先清理 unsatisfied pair，最后才移除 original。其他尚存 recovery 仍需要原始行来
    证明自身关联，因此不提前移除其原始事实；坏格式/重复闭合均 fail closed。
    闭合唯一性必须先使用完整历史判断，再独立检查整组是否过期，避免 cutoff 隐藏新冲突。
    """
    groups: list[_ClosedGroup] = []
    for automatic in records:
        original = parse_automatic_started(
            automatic,
            user_id=user_id,
            connection_id=connection_id,
            key_version=key_version,
        )
        if original is None:
            continue
        recoveries = tuple(
            record
            for record in records
            if (
                record.event_type == "oauth.refresh_recovery_authorization_started"
                and record.metadata.get("refresh_attempt_id") == original.refresh_attempt_id
            )
        )
        closing: list[_ClosedGroup] = []
        for result in records:
            parsed = parse_oauth_refresh_result(
                result,
                automatic=automatic,
                user_id=user_id,
                connection_id=connection_id,
                key_version=key_version,
            )
            if isinstance(parsed, ConfirmedV1):
                if not recoveries:
                    closing.append(
                        _ClosedGroup(
                            automatic.event_id,
                            None,
                            (
                                automatic.event_id,
                                result.event_id,
                            ),
                        )
                    )
                continue
            for recovery in recoveries:
                parsed = parse_oauth_refresh_result(
                    result,
                    automatic=automatic,
                    recovery=recovery,
                    user_id=user_id,
                    connection_id=connection_id,
                    key_version=key_version,
                )
                if isinstance(parsed, RecoveryUnsatisfiedV1):
                    matches = sum(
                        isinstance(
                            parse_oauth_refresh_result(
                                candidate,
                                automatic=automatic,
                                recovery=recovery,
                                user_id=user_id,
                                connection_id=connection_id,
                                key_version=key_version,
                            ),
                            (RecoveryUnsatisfiedV1, CredentialReplacedV1),
                        )
                        for candidate in records
                    )
                    if matches == 1:
                        groups.append(
                            _ClosedGroup(
                                automatic.event_id,
                                recovery.event_id,
                                (
                                    recovery.event_id,
                                    result.event_id,
                                ),
                            )
                        )
                elif isinstance(parsed, CredentialReplacedV1) and len(recoveries) == 1:
                    closing.append(
                        _ClosedGroup(
                            automatic.event_id,
                            recovery.event_id,
                            (
                                automatic.event_id,
                                recovery.event_id,
                                result.event_id,
                            ),
                        )
                    )
        if len(closing) == 1:
            groups.extend(closing)
    # 年龄只决定已证明完整的组能否整组删除，不能改变同一事实集的闭合或冲突结论。
    expired_ids = {record.event_id for record in records if record.created_at < cutoff}
    return tuple(group for group in groups if set(group.delete_ids) <= expired_ids)


class OAuthLifecycleCleanup:
    """按连接/事件组独立短事务处理 cutoff，表锁竞争使本轮有界结束。"""

    def __init__(self, sessions: ManagedAsyncSessionMaker, *, key_version: int = 1) -> None:
        """注入固定 APP key version；它来自组合配置，绝不读取后续 credential 行猜测。"""
        self._sessions = sessions
        self._key_version = key_version

    async def clean_user(self, *, user_id: UUID, cutoff: datetime, batch_size: int) -> int:
        """只对已解析的实际候选取锁，返回已提交的关闭组数；未知/坏组完整保留。"""
        after: UUID | None = None
        deleted = 0
        while True:
            async with self._sessions() as session:
                statement = select(OAuthConnectionModel.id).where(
                    OAuthConnectionModel.user_id == user_id
                )
                if after is not None:
                    statement = statement.where(OAuthConnectionModel.id > after)
                connection_ids = (
                    await session.scalars(
                        statement.order_by(OAuthConnectionModel.id).limit(batch_size)
                    )
                ).all()
            if not connection_ids:
                return deleted
            for connection_id in connection_ids:
                async with self._sessions() as session:
                    observed = await read_refresh_events(
                        session, user_id=user_id, connection_id=connection_id
                    )
                candidates = _closed_groups(
                    observed,
                    user_id=user_id,
                    connection_id=connection_id,
                    key_version=self._key_version,
                    cutoff=cutoff,
                )
                for candidate in candidates[:batch_size]:
                    try:
                        async with self._sessions.begin() as session:
                            identity = await lock_oauth_cleanup_identity(
                                session,
                                user_id=user_id,
                                connection_id=connection_id,
                            )
                            if identity is None:
                                continue
                            records = await lock_cleanup_refresh_events(
                                session,
                                user_id=user_id,
                                connection_id=connection_id,
                            )
                            current = _closed_groups(
                                records,
                                user_id=user_id,
                                connection_id=connection_id,
                                key_version=self._key_version,
                                cutoff=cutoff,
                            )
                            if candidate not in current:
                                continue
                            await session.execute(
                                delete(AuditEventModel).where(
                                    AuditEventModel.user_id == user_id,
                                    AuditEventModel.id.in_(candidate.delete_ids),
                                    AuditEventModel.created_at < cutoff,
                                )
                            )
                        deleted += 1
                    except DBAPIError as error:
                        if not oauth_cleanup_lock_contended(error):
                            raise
                        return deleted
            after = connection_ids[-1]
