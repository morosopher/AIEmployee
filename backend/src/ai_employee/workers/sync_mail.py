"""把 durable ``sync_mail`` 与 legacy ``sync_gmail`` 组合为受控邮件读取任务。"""

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from ai_employee.application.ports.mail import MailReader
from ai_employee.application.ports.oauth import OAuthProviderAdapter
from ai_employee.application.ports.oauth_refresh import (
    OAuthRefreshCoordinator,
    OAuthRefreshError,
    OAuthRefreshProvider,
    OAuthRefreshRequest,
)
from ai_employee.application.use_cases.sync_mail import (
    CoordinatedAccessTokenRefresh,
    MailConnectionNotFoundError,
    MailSyncStoreFactory,
    SyncMailUseCase,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.infrastructure.db.repositories.email import (
    SqlAlchemyMailSyncRepositoryFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import Metrics
from ai_employee.infrastructure.observability.sync import observe_google_sync
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.infrastructure.testing.scenarios import consume_test_scenario
from ai_employee.integrations.google.fake import FakeGmailReader, FakeGoogleOAuthClient
from ai_employee.integrations.google.gmail import GmailAdapter
from ai_employee.integrations.google.oauth import GoogleOAuthAdapter, GoogleOAuthClient
from ai_employee.integrations.microsoft.fake import (
    FakeMicrosoftMailReader,
    FakeMicrosoftOAuthAdapter,
)
from ai_employee.integrations.microsoft.mail import MicrosoftMailAdapter
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter
from ai_employee.integrations.registry import (
    LegacyGoogleMailReader,
    ProviderAdapterRegistry,
    build_oauth_security_services,
)


class MailSyncTaskStep:
    """在 DurableTaskRunner 租约内执行一个 scoped ``sync_mail`` 任务。"""

    name = "sync_mail"

    def __init__(
        self,
        *,
        session_factory: ManagedAsyncSessionMaker,
        cipher: AeadCipher,
        oauth: OAuthRefreshProvider | GoogleOAuthClient | FakeGoogleOAuthClient,
        reader: LegacyGoogleMailReader | FakeGmailReader | None = None,
        metrics: Metrics | None = None,
        refresh_coordinator: OAuthRefreshCoordinator | None = None,
        microsoft_oauth: OAuthProviderAdapter | None = None,
        microsoft_reader: MailReader | None = None,
    ) -> None:
        """注入 Google/Microsoft 进程资源；每个数据库操作仍由独立短事务创建。

        ``microsoft_oauth`` 与 ``microsoft_reader`` 为可选是为了保留 M1 Google 组合根和
        已持久化 legacy 任务的构造兼容；Microsoft 连接只有在明确注入 adapter 后才会进入
        Graph，缺失组装不会退回 Google 端点。
        """
        self._stores = SqlAlchemyMailSyncRepositoryFactory(session_factory)
        self._cipher = cipher
        self._oauth = oauth
        self._refresh_coordinator = refresh_coordinator
        self._reader = reader
        self._metrics = metrics
        self._microsoft_oauth = microsoft_oauth
        self._microsoft_reader = microsoft_reader

    async def execute(self, task: LeasedTask) -> None:
        """解密连接 token，组装固定 registry，并执行一个邮件 scope 同步。

        新任务必须携带非空 ``scope_key``。持久化 legacy ``sync_gmail`` 任务来自 M1 单邮箱
        架构，因此缺少该字段时只能回填可证明的固定 ``mailbox``，其他任务不得猜测 scope。
        Microsoft 连接只有在显式 ``microsoft_oauth`` 组装后才会进入 Graph；能力撤销时
        仅持久化 ``mail.read=action_required``，401 授权失效才降级整个连接。Microsoft 的
        ``mailbox`` 行会永久保留为目录发现触发器，使后续新增 folder 可被发现；它绝不作为
        Graph Delta scope。每轮目录读取必须先完整成功，随后才按返回顺序推进各 folder。
        """
        raw_connection_id = task.input_payload.get("connection_id")
        if not isinstance(raw_connection_id, str) or task.user_id is None:
            raise ValueError("sync_mail requires connection_id")
        raw_scope_key = task.input_payload.get("scope_key")
        if raw_scope_key is None and task.kind == "sync_gmail":
            scope_key = "mailbox"
        elif isinstance(raw_scope_key, str) and raw_scope_key != "":
            scope_key = raw_scope_key
        else:
            raise ValueError("sync_mail requires scope_key")

        connection_id = UUID(raw_connection_id)
        user_id = task.user_id
        async with self._stores() as store:
            credentials = await store.get_credentials(
                user_id=user_id,
                connection_id=connection_id,
            )
        if credentials is None:
            raise MailConnectionNotFoundError
        access_token = self._cipher.decrypt(
            credentials.access_token,
            self._credential_aad(user_id, connection_id, "access_token"),
        ).decode("utf-8")
        # 资源 adapter 只接收 access；refresh plaintext 仅在 coordinator 的受控内存解密。
        provider = (
            self._microsoft_oauth
            if credentials.provider == "microsoft"
            else (
                None
                if isinstance(self._oauth, (GoogleOAuthClient, FakeGoogleOAuthClient))
                else self._oauth
            )
        )
        refresh_access_token = CoordinatedAccessTokenRefresh(
            coordinator=self._refresh_coordinator,
            provider=provider,
            request=OAuthRefreshRequest(
                user_id=user_id,
                connection_id=connection_id,
                capability=ConnectionCapability.MAIL_READ,
                expected_access=credentials.access_snapshot,
            ),
        )

        async def mark_expired() -> None:
            """在授权永久失效后独立提交降级状态，阻止未来后台读取。"""
            async with self._stores() as store:
                await store.mark_expired(user_id=user_id, connection_id=connection_id)

        async def mark_mail_action_required(error_code: str) -> None:
            """只把邮件读取能力降级为 action_required，不误伤连接或日历能力。"""
            async with self._stores() as store:
                await store.mark_mail_capability_action_required(
                    user_id=user_id,
                    connection_id=connection_id,
                    error_code=error_code,
                )

        if credentials.provider == "microsoft":
            if self._microsoft_oauth is None:
                raise PermanentProviderError(
                    error_code="provider_read_adapter_unavailable",
                    message="Microsoft mail read adapter is unavailable",
                )
            microsoft_reader = self._microsoft_reader or MicrosoftMailAdapter(
                access_token=access_token,
                refresh_access_token=(refresh_access_token),
            )
            registry = ProviderAdapterRegistry(microsoft_mail=microsoft_reader)
            sync_case = SyncMailUseCase(
                cast(MailSyncStoreFactory, self._stores),
                registry,
                self._cipher,
            )
            try:
                if scope_key == "mailbox":
                    # discovery 不经过单-folder SyncMailUseCase，因此必须先复用同一
                    # Repository 授权检查；已关闭/撤销 mail.read 的排队任务不能继续
                    # 产生新的供应商读取。短事务只验证快照，不把 Graph I/O 放进锁内。
                    async with self._stores() as store:
                        discovery_state = await store.get_state(
                            user_id=user_id,
                            connection_id=connection_id,
                            scope_key=scope_key,
                        )
                    if discovery_state is None:
                        raise MailConnectionNotFoundError
                    # OAuth callback 建立的 mailbox 行会永久保留为周期目录发现触发器，
                    # 以便后续新增 folder 进入调度。Graph folder ID 是供应商 opaque 值，
                    # 因此占位值绝不能发给 Delta；目录读取先完整成功，随后每个 folder
                    # 才通过独立 use-case/事务创建或推进自己的 cursor。目录失败时循环
                    # 尚未开始，所有真实 folder cursor 均保持原值。
                    discovered_scopes = await microsoft_reader.list_sync_scopes()
                    seen_scope_keys: set[str] = set()
                    for discovered in discovered_scopes:
                        discovered_key = discovered.scope_key
                        if (
                            discovered_key == ""
                            or discovered_key == "mailbox"
                            or discovered_key in seen_scope_keys
                        ):
                            raise PermanentProviderError(
                                error_code="microsoft_mail_scope_invalid",
                                message="Microsoft mail folder discovery is invalid",
                            )
                        seen_scope_keys.add(discovered_key)
                    discovered_scopes = tuple(
                        sorted(discovered_scopes, key=lambda scope: scope.scope_key)
                    )
                    async with self._stores() as store:
                        await store.mark_directory_success(
                            user_id=user_id,
                            connection_id=connection_id,
                            completed_at=datetime.now(UTC),
                            # 只有完整目录读取、格式校验、去重与稳定排序全部完成后，才把
                            # 精确 scope tuple 交给短事务建立 placeholder；任意未验证 key
                            # 都不能先于目录成功事实进入 PostgreSQL。
                            folder_scope_keys=tuple(
                                discovered.scope_key for discovered in discovered_scopes
                            ),
                        )
                    first_folder_error: Exception | None = None
                    for discovered in discovered_scopes:
                        try:
                            await sync_case.execute(
                                user_id=user_id,
                                connection_id=connection_id,
                                scope_key=discovered.scope_key,
                            )
                        except (TransientProviderError, PermanentProviderError) as error:
                            # 已成功 folder 的事务不能回滚；继续尝试其余 folder，最后抛出
                            # 第一个固定错误，让 Durable Worker 重试失败 scope。
                            if first_folder_error is None:
                                first_folder_error = error
                    if first_folder_error is not None:
                        raise first_folder_error
                else:
                    await sync_case.execute(
                        user_id=user_id,
                        connection_id=connection_id,
                        scope_key=scope_key,
                    )
            except UserActionRequiredError as error:
                if error.error_code == "microsoft_mail_permission_required":
                    await mark_mail_action_required(error.error_code)
                elif error.error_code == "microsoft_reauthorization_required":
                    await mark_expired()
                raise
            return

        google_reader: LegacyGoogleMailReader = (
            self._reader.for_user(user_id)
            if isinstance(self._reader, FakeGmailReader)
            else self._reader
        ) or GmailAdapter(
            access_token=access_token,
            refresh_access_token=(refresh_access_token),
            mark_expired=mark_expired,
        )
        registry = ProviderAdapterRegistry(google_mail=google_reader)
        try:
            await observe_google_sync(
                metrics=self._metrics,
                # 保留 M1 Google 指标 label，避免 Task 7 把指标协议迁移混入 source port 重构。
                resource="gmail",
                operation=lambda: SyncMailUseCase(
                    cast(MailSyncStoreFactory, self._stores),
                    registry,
                    self._cipher,
                ).execute(
                    user_id=user_id,
                    connection_id=connection_id,
                    scope_key=scope_key,
                ),
            )
        except OAuthRefreshError:
            # unknown fence 保持 connected，后续只能由显式授权恢复，不能伪装为撤销。
            raise
        except UserActionRequiredError:
            # Fake 与真实适配器都经同一撤销事实入口，避免测试路径绕过持久化语义。
            await mark_expired()
            raise

    @staticmethod
    def _credential_aad(user_id: UUID, connection_id: UUID, kind: str) -> bytes:
        """保持与 OAuth callback 一致的 token AAD，阻止跨连接密文替换。"""
        return f"{user_id}:{connection_id}:{kind}".encode("ascii")


def build_mail_sync_task_step(
    *,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings,
    metrics: Metrics | None = None,
) -> MailSyncTaskStep:
    """从进程配置构造邮件 durable task step，不向任务载荷泄露 Secret。"""
    security = build_oauth_security_services(
        session_factory=session_factory, master_key_file=settings.app_master_key_file
    )
    cipher = security.cipher
    if settings.app_test_mode:
        fixture = (
            Path(__file__).parents[3] / "tests" / "contract" / "fixtures" / "gmail_initial.json"
        )
        return MailSyncTaskStep(
            session_factory=session_factory,
            cipher=cipher,
            refresh_coordinator=security.coordinator,
            oauth=FakeGoogleOAuthClient(),
            reader=FakeGmailReader(
                fixture,
                scenario_consumer=lambda user_id: consume_test_scenario(
                    redis_url=settings.redis_url,
                    user_id=user_id,
                ),
            ),
            metrics=metrics,
            microsoft_oauth=FakeMicrosoftOAuthAdapter(
                client_id=settings.microsoft_client_id or "test-mode-microsoft-client",
                redirect_uri=settings.microsoft_redirect_uri
                or "https://app.example.test/microsoft/callback",
            ),
            microsoft_reader=FakeMicrosoftMailReader(),
        )
    client_secret = settings.read_secret_file(settings.google_client_secret_file).get_secret_value()
    oauth = GoogleOAuthAdapter(
        settings.google_client_id,
        client_secret,
        settings.google_redirect_uri,
    )
    microsoft_oauth: MicrosoftOAuthAdapter | None = None
    if settings.microsoft_client_id and settings.microsoft_redirect_uri:
        microsoft_secret = settings.read_secret_file(
            settings.microsoft_client_secret_file
        ).get_secret_value()
        microsoft_oauth = MicrosoftOAuthAdapter(
            settings.microsoft_client_id,
            microsoft_secret,
            settings.microsoft_redirect_uri,
        )
    return MailSyncTaskStep(
        session_factory=session_factory,
        cipher=cipher,
        refresh_coordinator=security.coordinator,
        oauth=oauth,
        metrics=metrics,
        microsoft_oauth=microsoft_oauth,
    )


__all__ = ["MailSyncTaskStep", "build_mail_sync_task_step"]
