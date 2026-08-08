"""受双测试开关保护的合成来源 fixture 服务。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.encryption import AeadCipher

GOOGLE_MAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


class TestSupportFixtureService:
    """封装测试专用 ORM、加密和用户隔离操作，避免 HTTP 路由越过应用边界。

    该服务只在 ``APP_ENV=test`` 与 ``APP_TEST_MODE=true`` 的组合根中实例化。写入的
    凭据、邮件和任务均为合成数据；每次来源创建使用新的连接 ID，防止 E2E 被历史数据污染。
    """

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        create_task_use_case: CreateTaskUseCase,
        *,
        app_master_key_file: Path,
    ) -> None:
        """注入 API 生命周期拥有的会话工厂和既有创建任务用例。"""
        self._session_factory = session_factory
        self._create_task_use_case = create_task_use_case
        self._app_master_key_file = app_master_key_file

    async def seed_google_source(self, *, user_id: UUID) -> UUID:
        """为当前用户创建加密的合成 Google 来源，并返回新连接 ID。"""
        connection_id = uuid4()
        current = datetime.now(UTC)
        cipher = AeadCipher.from_file(self._app_master_key_file)
        access = cipher.encrypt(
            b"fake-access", f"{user_id}:{connection_id}:access_token".encode("ascii")
        )
        refresh = cipher.encrypt(
            b"fake-refresh", f"{user_id}:{connection_id}:refresh_token".encode("ascii")
        )
        async with self._session_factory.begin() as session:
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id=f"e2e-{connection_id}",
                    account_email="e2e-source@example.test",
                    scopes=[GOOGLE_MAIL_READ_SCOPE, GOOGLE_CALENDAR_READ_SCOPE],
                    status="connected",
                    last_error_code=None,
                )
            )
            # 连接模型未声明 relationship，先单独 flush 父表，保证后续凭据和游标
            # 的外键在数据库中已有目标；仍处于同一事务，失败会整体回滚。
            await session.flush()
            session.add_all(
                (
                    EncryptedCredentialModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        credential_kind="access_token",
                        ciphertext=access.ciphertext,
                        nonce=access.nonce,
                        key_version=access.key_version,
                    ),
                    EncryptedCredentialModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        credential_kind="refresh_token",
                        ciphertext=refresh.ciphertext,
                        nonce=refresh.nonce,
                        key_version=refresh.key_version,
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability="mail.read",
                        status="enabled",
                        actual_scopes=[GOOGLE_MAIL_READ_SCOPE],
                        last_verified_at=current,
                        last_error_code=None,
                    ),
                    ConnectionCapabilityModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        capability="calendar.read",
                        status="enabled",
                        actual_scopes=[GOOGLE_CALENDAR_READ_SCOPE],
                        last_verified_at=current,
                        last_error_code=None,
                    ),
                )
            )
            session.add_all(
                (
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="mail",
                        scope_key="mailbox",
                        cursor="synthetic-cursor",
                        last_success_at=current - timedelta(hours=1),
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        scope_key="primary",
                        cursor="synthetic-cursor",
                        last_success_at=current,
                    ),
                )
            )
            # 凭据与游标共同依赖已经落库的连接，先完成这一批次，再创建邮件线程。
            await session.flush()
            thread = EmailThreadModel(
                user_id=user_id,
                connection_id=connection_id,
                provider_thread_id=f"e2e-thread-{connection_id}",
                subject="Synthetic follow-up",
                participants=[],
                latest_message_at=current,
                provider_url="https://example.test/e2e-thread",
            )
            session.add(thread)
            await session.flush()
            session.add(
                EmailMessageModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    thread_id=thread.id,
                    provider_message_id=f"e2e-message-{connection_id}",
                    received_at=current,
                    sender={"email": "sender@example.test"},
                    recipients=[],
                    subject="Synthetic follow-up",
                    snippet="Synthetic approved source fact",
                    body_ciphertext=b"test",
                    body_nonce=b"0" * 12,
                    body_key_version=1,
                    labels=[],
                    headers={},
                    provider_url="https://example.test/e2e-message",
                )
            )
        return connection_id

    async def create_bound_brief_task(self, *, user_id: UUID, connection_id: UUID) -> UUID | None:
        """验证连接归属后创建冻结该连接范围的耐久测试简报任务。"""
        async with self._session_factory() as session:
            connection = await session.scalar(
                select(OAuthConnectionModel.id).where(
                    OAuthConnectionModel.id == connection_id,
                    OAuthConnectionModel.user_id == user_id,
                    OAuthConnectionModel.provider == "google",
                    OAuthConnectionModel.status == "connected",
                )
            )
        if connection is None:
            return None
        result = await self._create_task_use_case.execute(
            user_id=user_id,
            kind="daily_brief",
            input_payload={"schedule_kind": "test", "connection_id": str(connection_id)},
            idempotency_key=f"daily_brief:{user_id}:test:{uuid4()}",
        )
        return result.task_id

    async def task_is_owned_by(self, *, user_id: UUID, task_id: UUID) -> bool:
        """检查测试同步执行前的任务归属，保持 HTTP 认证之外的数据库隔离防线。"""
        async with self._session_factory() as session:
            task_id_result = await session.scalar(
                select(TaskRunModel.id).where(
                    TaskRunModel.id == task_id,
                    TaskRunModel.user_id == user_id,
                )
            )
        return task_id_result is not None
