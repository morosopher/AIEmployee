"""执行隐私删除的 Worker 步骤，所有删除均在 PostgreSQL 有界事务内完成。"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Final, Protocol
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidTag
from sqlalchemy import delete, select, update

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings, get_settings
from ai_employee.infrastructure.db.models.briefs import (
    ConversationModel,
    DailyBriefItemModel,
    DailyBriefModel,
    LLMInvocationModel,
    MessageModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    EmailAnalysisModel,
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.google.fake import FakeGoogleOAuthClient
from ai_employee.integrations.google.oauth import GoogleOAuthClient

DEFAULT_PRIVACY_BATCH_SIZE: Final[int] = 100


class OAuthTokenRevoker(Protocol):
    """定义全数据删除可调用的最小供应商撤销边界。"""

    async def revoke(self, token: str) -> None:
        """尽力撤销单个 OAuth token，失败由删除 Worker 本地处理。"""
        ...


class AllDataDeletionCompleted(BaseException):
    """表示当前任务已被删除，Worker 入口必须直接确认消息且不再写 TaskRun。

    该哨兵故意不继承 ``Exception``，避免 ``DurableTaskRunner`` 的通用错误收敛逻辑在
    已删除任务上再次写入失败状态；只允许 ``execute_task`` 的外层组合边界捕获它。
    """


class PrivacyDeletionWorker:
    """以用户范围执行可重试的来源缓存或全数据删除。"""

    name = "privacy_deletion"

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        *,
        credential_cipher: AeadCipher | None = None,
        oauth_revoker: OAuthTokenRevoker | None = None,
    ) -> None:
        """注入专用会话、可选凭据解密器与供应商撤销端口。

        Args:
            session_factory: 由调用方拥有生命周期的 retention 专用会话工厂。
            credential_cipher: 生产组合根提供的 AEAD 解密器；仅 source-cache 测试可省略。
            oauth_revoker: Google 撤销适配器或本地 Fake；缺失时不尝试任何网络操作。
        """
        self._session_factory = session_factory
        self._credential_cipher = credential_cipher
        self._oauth_revoker = oauth_revoker

    async def execute(self, task: LeasedTask) -> None:
        """按可信任务种类执行删除，未知种类明确拒绝。"""
        if task.user_id is None:
            raise ValueError("privacy deletion requires a user id")
        if task.kind == "privacy.clear_source_cache":
            await self.clear_source_cache(user_id=task.user_id, batch_size=DEFAULT_PRIVACY_BATCH_SIZE)
            return
        if task.kind == "privacy.delete_all_data":
            request_id = task.input_payload.get("deletion_request_id")
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("privacy all-data deletion requires an opaque request id")
            await self.delete_all_data(
                user_id=task.user_id,
                request_id=request_id,
                batch_size=DEFAULT_PRIVACY_BATCH_SIZE,
            )
            raise AllDataDeletionCompleted
        raise ValueError("unsupported privacy task kind")

    async def clear_source_cache(self, *, user_id: UUID, batch_size: int) -> None:
        """删除可重新同步来源，并把 Gmail/Calendar 同步状态复位为首次同步。"""
        self._validate_batch_size(batch_size)
        await self._delete_user_rows(EmailAnalysisModel, user_id, batch_size)
        await self._delete_user_rows(EmailMessageModel, user_id, batch_size)
        await self._delete_user_rows(CalendarEventModel, user_id, batch_size)
        await self._delete_user_rows(EmailThreadModel, user_id, batch_size)
        # 简报只来源于 M1 的只读邮箱与日历缓存；清理缓存后它不再是可审计的有效派生物。
        await self._delete_briefs(user_id=user_id, batch_size=batch_size)
        async with self._session_factory.begin() as session:
            connection_ids = select(OAuthConnectionModel.id).where(OAuthConnectionModel.user_id == user_id)
            await session.execute(
                update(SyncCursorModel)
                .where(SyncCursorModel.connection_id.in_(connection_ids))
                .values(cursor=None, last_success_at=None, last_attempt_at=None, last_error_code=None)
            )

    async def delete_all_data(self, *, user_id: UUID, request_id: str, batch_size: int) -> None:
        """不可逆删除本地数据并匿名化用户；重试只保留一条无内容完成审计。"""
        self._validate_batch_size(batch_size)
        async with self._session_factory() as session:
            completed = await session.scalar(
                select(AuditEventModel.id).where(
                    AuditEventModel.user_id == user_id,
                    AuditEventModel.event_type == "privacy.deletion_completed",
                    AuditEventModel.event_metadata["request_id"].astext == request_id,
                )
            )
        if completed is not None:
            return

        # 外部撤销只在事务外尝试；网络/密文异常绝不能阻断本地凭据清除。
        await self._best_effort_revoke_google_tokens(user_id=user_id)
        await self._delete_user_rows(EncryptedCredentialModel, user_id, batch_size)
        await self.clear_source_cache(user_id=user_id, batch_size=batch_size)
        await self._delete_user_rows(MessageModel, user_id, batch_size)
        await self._delete_user_rows(ConversationModel, user_id, batch_size)
        await self._delete_user_rows(UserSessionModel, user_id, batch_size)
        await self._delete_task_graph(user_id=user_id, batch_size=batch_size)
        await self._delete_user_rows(OAuthConnectionModel, user_id, batch_size)
        await self._finalize_deleted_user(user_id=user_id, request_id=request_id)

    async def _best_effort_revoke_google_tokens(self, *, user_id: UUID) -> None:
        """每个 Google 连接至多撤销一次，并在失败时继续本地删除。

        只读取当前用户仍持久化的密文，优先 refresh token、缺失时才退回 access token。
        选择与撤销均在删除事务之外，既避免长事务持有数据库锁，也确保供应商不可用时
        本地删除仍可完成。没有配置依赖时保持无网络的 source-cache/直接 Worker 测试语义。
        """
        if self._credential_cipher is None or self._oauth_revoker is None:
            return
        async with self._session_factory() as session:
            credentials = (
                await session.scalars(
                    select(EncryptedCredentialModel)
                    .join(
                        OAuthConnectionModel,
                        OAuthConnectionModel.id == EncryptedCredentialModel.connection_id,
                    )
                    .where(
                        EncryptedCredentialModel.user_id == user_id,
                        OAuthConnectionModel.user_id == user_id,
                        OAuthConnectionModel.provider == "google",
                    )
                )
            ).all()

        selected: dict[UUID, EncryptedCredentialModel] = {}
        for credential in credentials:
            current = selected.get(credential.connection_id)
            if current is None or credential.credential_kind == "refresh_token":
                selected[credential.connection_id] = credential
        for credential in selected.values():
            try:
                token = self._credential_cipher.decrypt(
                    EncryptedValue(
                        ciphertext=credential.ciphertext,
                        nonce=credential.nonce,
                        key_version=credential.key_version,
                    ),
                    self._credential_aad(
                        user_id, credential.connection_id, credential.credential_kind
                    ),
                ).decode("utf-8")
                await self._oauth_revoker.revoke(token)
            except (httpx.HTTPError, InvalidTag, UnicodeDecodeError):
                # 删除权不能依赖 Google、网络或一条已损坏的本地密文；不记录 token 或异常原文。
                continue

    @staticmethod
    def _credential_aad(user_id: UUID, connection_id: UUID, kind: str) -> bytes:
        """复用 OAuth 回调和同步步骤的记录绑定 AAD 格式。"""
        return f"{user_id}:{connection_id}:{kind}".encode("ascii")

    async def _delete_briefs(self, *, user_id: UUID, batch_size: int) -> None:
        """先删简报条目再删父简报，避免不同数据库级联实现的差异。"""
        while True:
            async with self._session_factory.begin() as session:
                brief_ids = (await session.scalars(
                    select(DailyBriefModel.id).where(DailyBriefModel.user_id == user_id).limit(batch_size)
                )).all()
                if not brief_ids:
                    return
                await session.execute(delete(DailyBriefItemModel).where(DailyBriefItemModel.brief_id.in_(brief_ids)))
                await session.execute(delete(DailyBriefModel).where(DailyBriefModel.id.in_(brief_ids)))

    async def _delete_task_graph(self, *, user_id: UUID, batch_size: int) -> None:
        """按任务图根批量删除，先移除无任务外键的 Outbox 与审计事实。"""
        while True:
            async with self._session_factory.begin() as session:
                task_ids = (await session.scalars(
                    select(TaskRunModel.id).where(TaskRunModel.user_id == user_id).limit(batch_size)
                )).all()
                if not task_ids:
                    break
                await session.execute(delete(LLMInvocationModel).where(LLMInvocationModel.task_id.in_(task_ids)))
                await session.execute(delete(OutboxEventModel).where(OutboxEventModel.aggregate_id.in_(task_ids)))
                await session.execute(delete(AuditEventModel).where(AuditEventModel.task_id.in_(task_ids)))
                await session.execute(delete(TaskRunModel).where(TaskRunModel.id.in_(task_ids)))
        # 任务为空时仍可能有会话、认证或历史审计；全数据删除不应保留其内容。
        await self._delete_user_rows(AuditEventModel, user_id, batch_size)

    async def _finalize_deleted_user(self, *, user_id: UUID, request_id: str) -> None:
        """原子匿名化用户并写入一次不含来源内容的完成审计。"""
        now = datetime.now(UTC)
        async with self._session_factory.begin() as session:
            existing = await session.scalar(
                select(AuditEventModel.id).where(
                    AuditEventModel.user_id == user_id,
                    AuditEventModel.event_type == "privacy.deletion_completed",
                    AuditEventModel.event_metadata["request_id"].astext == request_id,
                ).with_for_update()
            )
            if existing is not None:
                return
            await session.execute(
                update(UserModel)
                .where(UserModel.id == user_id)
                .values(
                    email=f"deleted-{user_id}@invalid.local",
                    display_name="Deleted User",
                    password_hash=None,
                    is_active=False,
                    email_body_retention_days=30,
                    source_metadata_retention_days=180,
                    workspace_history_retention_days=365,
                )
            )
            session.add(AuditEventModel(
                user_id=user_id,
                task_id=None,
                event_type="privacy.deletion_completed",
                actor_type="system",
                actor_id=None,
                event_metadata={"operation": "all_data_deletion", "request_id": request_id, "completed_at": now.isoformat(), "trace_id": None},
                created_at=now,
            ))

    async def _delete_user_rows(self, model: type[Any], user_id: UUID, batch_size: int) -> None:
        """用主键子查询删除一个拥有 ``user_id`` 的表，保证每批独立提交。

        每次循环的事务仅包含一批记录，提交成功后才进入下一批。该明确的提交边界既限制
        删除时的锁范围，也让任务在进程中断后可以从剩余记录继续执行。

        Args:
            model: 仅在基础设施删除边界使用的 SQLAlchemy ORM 模型。
            user_id: 待删除数据所属的用户标识。
            batch_size: 单个事务允许删除的最大记录数。
        """
        identifier = model.id
        ownership = model.user_id
        while True:
            async with self._session_factory.begin() as session:
                ids = (await session.scalars(select(identifier).where(ownership == user_id).limit(batch_size))).all()
                if not ids:
                    return
                await session.execute(delete(model).where(identifier.in_(ids)))  # type: ignore[arg-type]
            # 只在事务成功提交后暴露批次边界，测试可以在这里模拟崩溃而不回滚已删除记录。
            await self._after_user_row_batch(
                model=model,
                user_id=user_id,
                deleted_count=len(ids),
            )

    async def _after_user_row_batch(
        self,
        *,
        model: type[Any],
        user_id: UUID,
        deleted_count: int,
    ) -> None:
        """提供已提交删除批次的测试观察点，生产实现不引入额外副作用。

        Args:
            model: 刚完成删除的 ORM 模型，用于测试精确选择崩溃阶段。
            user_id: 当前删除任务的用户标识。
            deleted_count: 已在本次事务中持久删除的记录数量。
        """
        del model, user_id, deleted_count

    @staticmethod
    def _validate_batch_size(batch_size: int) -> None:
        """拒绝零、负数或过大的删除批量，防止错误配置扩大锁范围。"""
        if not 1 <= batch_size <= 1_000:
            raise ValueError("privacy batch_size must be between 1 and 1000")


@lru_cache
def build_privacy_deletion_worker() -> PrivacyDeletionWorker:
    """使用 retention 专用 DSN 构建删除器，并把连接池所有权交给 Worker 入口。

    专用 DSN 必须由 Secret 文件注入；开发/测试未挂载该文件时只回退已验证的应用 DSN，
    避免为隐私任务读取第二套未受控配置。
    """
    settings = get_settings()
    if not settings.retention_database_url_file.is_file():
        if settings.app_env == "production":
            raise RuntimeError("RETENTION_DATABASE_URL_FILE is required in production")
        database_url = settings.database_url
    else:
        database_url = settings.read_secret_file(settings.retention_database_url_file).get_secret_value()
    return PrivacyDeletionWorker(
        build_session_factory(database_url),
        credential_cipher=AeadCipher.from_file(settings.app_master_key_file),
        oauth_revoker=_build_oauth_revoker(settings),
    )


def _build_oauth_revoker(settings: Settings) -> OAuthTokenRevoker:
    """按运行模式构造撤销适配器，测试模式严格使用本地 Fake。"""
    if settings.app_test_mode:
        return FakeGoogleOAuthClient()
    client_secret = settings.read_secret_file(settings.google_client_secret_file).get_secret_value()
    return GoogleOAuthClient(settings.google_client_id, client_secret, settings.google_redirect_uri)
