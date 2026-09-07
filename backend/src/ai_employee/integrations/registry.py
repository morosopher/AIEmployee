"""提供固定 Google/Microsoft 读取与可信写动作注册表及 M1 兼容包装。

基础注册表仅接受组合根显式注入的 ``*_action`` slot；生产组合使用相同固定动作集合，
按认证用户和冻结命令连接惰性读取凭据。构造与 slot 检查没有 Secret/网络 I/O，只有通过
应用层门禁后的解析才创建 adapter；仅有 preflight 的对象不会被提升为真实写权限。
"""

import asyncio
import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from email.errors import HeaderParseError
from email.headerregistry import AddressHeader, HeaderRegistry
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast
from uuid import UUID

from cryptography.exceptions import InvalidTag

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.oauth_refresh_identity import (
    OAuthRefreshIdentity,
    validated_token_text,
)
from ai_employee.application.ports.calendar import (
    CalendarDirectoryPage,
    CalendarEvent,
    CalendarReader,
    CalendarSyncPage,
)
from ai_employee.application.ports.encryption import (
    EncryptionBoundaryError,
    EncryptionKeyVersionError,
)
from ai_employee.application.ports.mail import MailReader, MailScope, MailSyncPage
from ai_employee.application.ports.oauth_refresh import (
    OAuthRefreshError,
    OAuthRefreshProvider,
    OAuthRefreshReady,
    OAuthRefreshRequest,
)
from ai_employee.application.ports.trusted_actions import (
    ApprovalPreflightResult,
    ExecutionReference,
    ProviderWriteOutcome,
    TrustedActionAdapter,
    TrustedActionDispatchSnapshot,
    TrustedActionPreflight,
    durable_retry_summary_is_valid,
    oauth_retry_pending_is_valid,
    oauth_write_refresh_attempt_id,
)
from ai_employee.config import Settings
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import (
    InternalInvariantError,
    PermanentProviderError,
    StateConflictError,
)
from ai_employee.domain.mail_actions import (
    MailMode,
    MailSendCommand,
    normalize_mail_recipients,
    normalize_mailbox_address,
)
from ai_employee.infrastructure.db.repositories.email import (
    MailConnectionCredentials,
    MailReplySourceHeaders,
    SqlAlchemyMailSyncRepository,
)
from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
    SqlAlchemyOAuthRefreshCoordinator,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.google.calendar_write import GoogleCalendarWriteAdapter
from ai_employee.integrations.google.gmail_write import GmailWriteAdapter
from ai_employee.integrations.google.oauth import GoogleOAuthAdapter
from ai_employee.integrations.microsoft.calendar_write import MicrosoftCalendarWriteAdapter
from ai_employee.integrations.microsoft.mail_write import (
    MicrosoftMailWriteAdapter,
    MicrosoftReplyRecipientFact,
)
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter


@dataclass(frozen=True, slots=True)
class OAuthSecurityServices:
    """承载由同一次 Secret 读取构造的 AEAD、固定 identity 和共享自动协调器。

    三个服务都不向 repr 暴露内部状态；不保留第二份 root Secret 或支持 keyring。
    """

    cipher: AeadCipher = field(repr=False)
    identity: OAuthRefreshIdentity = field(repr=False)
    coordinator: SqlAlchemyOAuthRefreshCoordinator = field(repr=False)


def build_oauth_security_services(
    *,
    session_factory: ManagedAsyncSessionMaker,
    master_key_file: Path,
    key_version: int = 1,
) -> OAuthSecurityServices:
    """从单次读取/解码的 root 构造 API、Worker、CLI 共用的凭据安全服务。

    Args:
        session_factory: 组合根拥有生命周期的会话工厂，不在此建立数据库连接。
        master_key_file: 既有 APP_MASTER_KEY_FILE，内容不得输出或写入审计。
        key_version: 当前固定 AEAD 版本；identity 仅消费 cipher 的公开只读属性。

    Raises:
        ValueError: Secret 编码或长度不合法，错误不得回显其内容。
        OSError: 既有 Secret 文件不可读取。
    """
    try:
        encoded = master_key_file.read_text(encoding="utf-8").strip().encode("ascii")
        root = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (UnicodeError, ValueError):
        raise ValueError("master key file must contain Base64URL data") from None
    cipher = AeadCipher(root, key_version=key_version)
    identity = OAuthRefreshIdentity(root, key_version=cipher.key_version)
    return OAuthSecurityServices(
        cipher,
        identity,
        SqlAlchemyOAuthRefreshCoordinator(
            session_factory=session_factory, cipher=cipher, identity=identity
        ),
    )


class UnsupportedProviderError(PermanentProviderError):
    """表示持久连接 provider 不属于 M2 固定 Google/Microsoft 集合。"""

    def __init__(self) -> None:
        """构造不回显未知 provider 原值的稳定安全错误。"""
        super().__init__(
            error_code="unsupported_provider",
            message="Connection provider is unsupported",
        )


class ProviderAdapterUnavailableError(InternalInvariantError):
    """表示受支持 provider 的当前进程尚未组装对应读取适配器。"""

    def __init__(self) -> None:
        """构造不泄露凭据、scope 或供应商响应的稳定组装错误。"""
        super().__init__(
            error_code="provider_read_adapter_unavailable",
            message="Provider read adapter is unavailable",
        )


class LegacyGoogleMailReader(Protocol):
    """描述 Task 7 以前 Gmail 适配器的最小分页形状。"""

    def initial_pages(self) -> AsyncIterator[MailSyncPage]: ...
    def history_pages(self, cursor: str) -> AsyncIterator[MailSyncPage]: ...


class LegacyGoogleCalendarReader(Protocol):
    """描述 Task 7 以前固定 primary Calendar 适配器的最小分页形状。"""

    def initial_pages(self) -> AsyncIterator[CalendarSyncPage]: ...
    def sync_pages(self, cursor: str) -> AsyncIterator[CalendarSyncPage]: ...


class _GoogleCalendarProviderNeutralReader(Protocol):
    """描述 Task 12 后 Google 适配器的目录与分日历端口。"""

    def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]: ...

    def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]: ...

    def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]: ...

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None: ...


class _GoogleMailCompatibilityAdapter:
    """把 M1 单 mailbox Gmail 读取器适配为 provider-neutral 邮件端口。"""

    def __init__(self, reader: LegacyGoogleMailReader) -> None:
        self._reader = reader

    async def list_sync_scopes(self) -> tuple[MailScope, ...]:
        """返回 M1 架构唯一且稳定的 ``mailbox`` scope。"""
        return (MailScope("mailbox", "Mailbox", "mailbox"),)

    def initial_pages(self, scope_key: str, *, since: datetime) -> AsyncIterator[MailSyncPage]:
        """验证唯一 mailbox 后委托旧适配器的七日初始读取。"""
        del since
        self._require_mailbox(scope_key)
        return self._reader.initial_pages()

    def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """验证唯一 mailbox 后把 provider-neutral cursor 委托为 Gmail history。"""
        self._require_mailbox(scope_key)
        return self._reader.history_pages(cursor)

    @staticmethod
    def _require_mailbox(scope_key: str) -> None:
        """拒绝让旧单 mailbox 适配器读取无法表达的其他 folder scope。"""
        if scope_key != "mailbox":
            raise ProviderAdapterUnavailableError


class _GoogleCalendarCompatibilityAdapter:
    """把 M1 固定 primary Calendar 读取器适配为显式日历 ID 端口。"""

    def __init__(
        self, reader: LegacyGoogleCalendarReader | _GoogleCalendarProviderNeutralReader
    ) -> None:
        self._reader = reader

    async def directory_pages(
        self, cursor: str | None = None
    ) -> AsyncIterator[CalendarDirectoryPage]:
        """委托 Task 12 目录端口；旧 primary reader 继续 fail closed。"""
        if hasattr(self._reader, "directory_pages"):
            async for page in self._reader.directory_pages(cursor):
                yield page
            return
        del cursor
        if False:
            yield CalendarDirectoryPage((), None, None, full_snapshot=True)

    def initial_pages(self, calendar_id: str) -> AsyncIterator[CalendarSyncPage]:
        """按 provider calendar ID 委托新适配器，旧 reader 仅允许 primary。"""
        if hasattr(self._reader, "directory_pages"):
            reader = cast(_GoogleCalendarProviderNeutralReader, self._reader)
            return reader.initial_pages(calendar_id)
        self._require_primary(calendar_id)
        return self._reader.initial_pages()

    def sync_pages(self, calendar_id: str, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """按 provider calendar ID 委托新适配器，旧 reader 仅允许 primary。"""
        if hasattr(self._reader, "directory_pages"):
            reader = cast(_GoogleCalendarProviderNeutralReader, self._reader)
            return reader.sync_pages(calendar_id, cursor)
        self._require_primary(calendar_id)
        return self._reader.sync_pages(cursor)

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """委托新适配器精确 GET；旧 reader 不用列表结果伪造当前事件。"""
        if hasattr(self._reader, "directory_pages"):
            reader = cast(_GoogleCalendarProviderNeutralReader, self._reader)
            return await reader.get_current_event(calendar_id, provider_event_id)
        del calendar_id, provider_event_id
        raise ProviderAdapterUnavailableError

    @staticmethod
    def _require_primary(calendar_id: str) -> None:
        """拒绝让旧固定 URL 适配器静默读取其他 provider calendar ID。"""
        if calendar_id != "primary":
            raise ProviderAdapterUnavailableError


class ProviderAdapterRegistry:
    """保存固定 Google/Microsoft 读适配器与四类可信动作，不提供动态注册。

    Google 与 Microsoft Calendar 各自的同一个显式 action adapter 都绑定到
    ``calendar.create``、``calendar.update`` 和 ``calendar.restore`` 三个固定键；未注入
    action slot 时，preflight 兼容对象仍不能被 ``trusted_action_adapter`` 当作真实写入
    adapter 返回。
    """

    __slots__ = (
        "_calendar_readers",
        "_mail_readers",
        "_trusted_action_adapters",
        "_trusted_action_preflights",
    )

    def __init__(
        self,
        *,
        google_mail: LegacyGoogleMailReader | None = None,
        microsoft_mail: MailReader | None = None,
        google_calendar: LegacyGoogleCalendarReader
        | _GoogleCalendarProviderNeutralReader
        | None = None,
        microsoft_calendar: CalendarReader | None = None,
        google_mail_preflight: TrustedActionPreflight | None = None,
        google_calendar_preflight: TrustedActionPreflight | None = None,
        microsoft_mail_preflight: TrustedActionPreflight | None = None,
        microsoft_calendar_preflight: TrustedActionPreflight | None = None,
        google_mail_action: TrustedActionAdapter | None = None,
        google_calendar_action: TrustedActionAdapter | None = None,
        microsoft_mail_action: TrustedActionAdapter | None = None,
        microsoft_calendar_action: TrustedActionAdapter | None = None,
    ) -> None:
        """以显式固定 slot 冻结读适配器、预检与四类真实动作 adapter。

        ``*_action`` 同时实现无副作用 preflight；显式 ``*_preflight`` 仅保留 Task 18
        审批提交兼容性，不能因具有相似方法名而自动获得真实写权限。
        """
        self._mail_readers = MappingProxyType(
            {
                "google": (
                    _GoogleMailCompatibilityAdapter(google_mail)
                    if google_mail is not None
                    else None
                ),
                "microsoft": microsoft_mail,
            }
        )
        self._calendar_readers = MappingProxyType(
            {
                "google": (
                    _GoogleCalendarCompatibilityAdapter(google_calendar)
                    if google_calendar is not None
                    else None
                ),
                "microsoft": microsoft_calendar,
            }
        )
        self._trusted_action_preflights = MappingProxyType(
            {
                # 不使用 ``or`` 选择 slot：adapter 可能定义业务意义上的 falsey
                # ``__bool__``，但只要显式注入就必须保持真实动作优先级，不能悄然退回
                # 仅 preflight 的兼容对象。
                ("google", "mail.send"): (
                    google_mail_action if google_mail_action is not None else google_mail_preflight
                ),
                ("google", "calendar.create"): (
                    google_calendar_action
                    if google_calendar_action is not None
                    else google_calendar_preflight
                ),
                ("google", "calendar.update"): (
                    google_calendar_action
                    if google_calendar_action is not None
                    else google_calendar_preflight
                ),
                ("google", "calendar.restore"): (
                    google_calendar_action
                    if google_calendar_action is not None
                    else google_calendar_preflight
                ),
                ("microsoft", "mail.send"): (
                    microsoft_mail_action
                    if microsoft_mail_action is not None
                    else microsoft_mail_preflight
                ),
                ("microsoft", "calendar.create"): (
                    microsoft_calendar_action
                    if microsoft_calendar_action is not None
                    else microsoft_calendar_preflight
                ),
                ("microsoft", "calendar.update"): (
                    microsoft_calendar_action
                    if microsoft_calendar_action is not None
                    else microsoft_calendar_preflight
                ),
                ("microsoft", "calendar.restore"): (
                    microsoft_calendar_action
                    if microsoft_calendar_action is not None
                    else microsoft_calendar_preflight
                ),
            }
        )
        self._trusted_action_adapters = MappingProxyType(
            {
                ("google", "mail.send"): google_mail_action,
                ("google", "calendar.create"): google_calendar_action,
                ("google", "calendar.update"): google_calendar_action,
                ("google", "calendar.restore"): google_calendar_action,
                ("microsoft", "mail.send"): microsoft_mail_action,
                ("microsoft", "calendar.create"): microsoft_calendar_action,
                ("microsoft", "calendar.update"): microsoft_calendar_action,
                ("microsoft", "calendar.restore"): microsoft_calendar_action,
            }
        )

    def mail_reader(self, *, provider: str, connection_id: UUID, scope_key: str) -> MailReader:
        """按固定 provider 键返回连接任务已组装的邮件读取端口。"""
        del connection_id, scope_key
        if provider not in self._mail_readers:
            raise UnsupportedProviderError
        reader = self._mail_readers[provider]
        if reader is None:
            raise ProviderAdapterUnavailableError
        return reader

    def calendar_reader(
        self, *, provider: str, connection_id: UUID, scope_key: str
    ) -> CalendarReader:
        """按固定 provider 键返回连接任务已组装的日历读取端口。"""
        del connection_id, scope_key
        if provider not in self._calendar_readers:
            raise UnsupportedProviderError
        reader = self._calendar_readers[provider]
        if reader is None:
            raise ProviderAdapterUnavailableError
        return reader

    def trusted_action_preflight(
        self,
        *,
        provider: str,
        action: str,
    ) -> TrustedActionPreflight:
        """按精确 provider/action 返回启动时固定的无副作用审批预检。

        未知 provider、超出四种 M2 动作或当前进程未组装适配器都使用同一内容无关
        错误，避免调用方把缺失动作误判为可批准或从错误中探测能力配置。
        """
        preflight = self._trusted_action_preflights.get((provider, action))
        if preflight is None or preflight.provider != provider:
            raise StateConflictError(
                error_code="provider_action_unavailable",
                message="provider action is unavailable",
            )
        return preflight

    def trusted_action_adapter(
        self,
        *,
        provider: str,
        action: str,
    ) -> TrustedActionAdapter:
        """返回固定真实动作 adapter，未知、动态或 provider 错配均 fail closed。"""
        adapter = self._trusted_action_adapters.get((provider, action))
        if adapter is None or adapter.provider != provider:
            raise StateConflictError(
                error_code="provider_action_unavailable",
                message="provider action is unavailable",
            )
        return adapter


__all__ = [
    "ProviderAdapterRegistry",
    "ProviderAdapterUnavailableError",
    "UnsupportedProviderError",
]


class _UnboundTrustedAction:
    """供启动/readiness 检查识别固定 slot；真实调用必须先提供已验证用户上下文。"""

    def __init__(self, provider: str) -> None:
        """只保存固定供应商名，不读取配置、Secret 或数据库。"""
        self.provider = provider

    def validate_for_approval(self, command: TrustedCommand) -> ApprovalPreflightResult:
        """缺少用户归属的预检安全拒绝，不能猜测默认账户或收件人事实。"""
        del command
        raise StateConflictError(
            error_code="connection_scope_missing", message="Connection credentials are unavailable"
        )

    async def execute(self, command: TrustedCommand) -> ProviderWriteOutcome:
        """禁止未解析用户的 slot 发送真实请求。"""
        self.validate_for_approval(command)
        raise AssertionError("unreachable")

    async def reconcile(
        self, command: TrustedCommand, execution: ExecutionReference
    ) -> ProviderWriteOutcome:
        """禁止未解析用户的 slot 读取供应商结果。"""
        del execution
        self.validate_for_approval(command)
        raise AssertionError("unreachable")


class CredentialBoundProviderRegistry(ProviderAdapterRegistry):
    """API 与 Worker 共用的四个固定写 slot，按显式用户/命令连接惰性解析凭据。

    构造及 readiness 无任何 I/O。每次解析只拥有短数据库 session，真实适配器在自己的
    HTTP 方法内创建并关闭 client。Microsoft 直接回复缺少精确源收件人 proof 时仍由
    既有 adapter 拒绝；不能把用户可编辑的命令收件人冒充供应商来源事实。
    """

    def __init__(self, *, session_factory: ManagedAsyncSessionMaker, settings: Settings) -> None:
        """冻结进程资源引用和四个固定 slot，不加载任何凭据。"""
        super().__init__(
            google_mail_action=_UnboundTrustedAction("google"),
            google_calendar_action=_UnboundTrustedAction("google"),
            microsoft_mail_action=_UnboundTrustedAction("microsoft"),
            microsoft_calendar_action=_UnboundTrustedAction("microsoft"),
        )
        self._sessions, self._settings = session_factory, settings

    async def _security(self) -> OAuthSecurityServices:
        """在受控线程读取同一 Secret，避免阻塞 API/Worker 事件循环。"""
        return await asyncio.to_thread(
            build_oauth_security_services,
            session_factory=self._sessions,
            master_key_file=self._settings.app_master_key_file,
        )

    async def validate_trusted_action_connection(
        self,
        *,
        user_id: UUID,
        provider: str,
        connection_id: UUID,
        action: str,
    ) -> None:
        """首次 claim 前按标识符检查本地可用性，不提前解密冻结命令或读取 Secret。

        调用方已通过用户、审批、哈希、写开关、能力、账户允许列表和租约门禁。本方法
        只检查精确连接的持久凭据存在；返回不授予写权限，等待后调用方仍须重查数据库
        时间，claim 后的 resolver 和 request-start 继续校验凭据及当前写授权。
        """
        await self._connection_credentials(
            user_id=user_id, provider=provider, connection_id=connection_id, action=action
        )

    async def _connection_credentials(
        self, *, user_id: UUID, provider: str, connection_id: UUID, action: str
    ) -> MailConnectionCredentials:
        """在短只读 session 复制精确凭据投影，供 claim 可用性与后续 resolver 共用。"""
        if self._settings.app_test_mode:
            raise StateConflictError(
                error_code="provider_action_unavailable",
                message="Production actions are disabled in test mode",
            )
        self.trusted_action_adapter(provider=provider, action=action)
        async with self._sessions() as session:
            credentials = await SqlAlchemyMailSyncRepository(session).get_credentials(
                user_id=user_id,
                connection_id=connection_id,
                expected_provider=provider,
                required_capability="mail.read" if action == "mail.send" else "calendar.read",
            )
        if credentials is None:
            raise StateConflictError(
                error_code="connection_scope_missing",
                message="Connection credentials are unavailable",
            )
        return credentials

    async def resolve_trusted_action_adapter(
        self,
        *,
        user_id: UUID,
        provider: str,
        command: TrustedCommand,
    ) -> TrustedActionAdapter:
        """把原 claim/认证用户与冻结 command.connection_id 作为唯一凭据选择条件。

        未连接、缺少只读 scope 或 AEAD 无效都产生固定 connection_scope_missing；不从
        默认账户、命令正文或第三方对象推导所有者。APP_TEST_MODE 必须显式注入 Fake，
        本生产 resolver 在测试模式不构造任何真实 HTTP adapter。
        """
        credentials = await self._connection_credentials(
            user_id=user_id,
            provider=provider,
            connection_id=command.connection_id,
            action=command.action,
        )
        account_email = credentials.account_email
        source: MailReplySourceHeaders | None = None
        if (
            provider == "microsoft"
            and isinstance(command, MailSendCommand)
            and command.mode is not MailMode.NEW
            and command.source_thread_id is not None
            and command.source_message_id is not None
        ):
            async with self._sessions() as session:
                source = await SqlAlchemyMailSyncRepository(session).get_reply_source_headers(
                    user_id=user_id,
                    connection_id=command.connection_id,
                    provider=provider,
                    source_thread_id=command.source_thread_id,
                    source_message_id=command.source_message_id,
                )
        security = await self._security()
        try:
            access = validated_token_text(
                security.cipher.decrypt(
                    credentials.access_token,
                    f"{user_id}:{command.connection_id}:access_token".encode("ascii"),
                )
            )
        except (InvalidTag, EncryptionBoundaryError, EncryptionKeyVersionError, ValueError):
            raise StateConflictError(
                error_code="connection_scope_missing",
                message="Connection credentials are unavailable",
            ) from None
        if provider == "google":
            return (
                GmailWriteAdapter(access_token=access, account_email=account_email)
                if command.action == "mail.send"
                else GoogleCalendarWriteAdapter(access_token=access)
            )
        if isinstance(command, MailSendCommand):
            facts = _microsoft_reply_facts(source, mode=command.mode, account_email=account_email)
            return MicrosoftMailWriteAdapter(
                access_token=access, account_email=account_email, reply_recipient_facts=facts
            )
        return MicrosoftCalendarWriteAdapter(
            connection_id=command.connection_id, access_token=access
        )

    async def refresh_after_rejection(
        self, snapshot: TrustedActionDispatchSnapshot
    ) -> OAuthRefreshReady:
        """精确持久 401 对应一个稳定 grant；已有 confirmed 的重入只读当前 readiness。"""
        execution = snapshot.execution
        pending = oauth_retry_pending_is_valid(execution)
        if (
            snapshot.connection_id is None
            or self._settings.app_test_mode
            or execution.error_code != f"{snapshot.provider}_reauthorization_required"
            or (not pending and not durable_retry_summary_is_valid(execution.result_summary))
        ):
            raise OAuthRefreshError("oauth_credential_state_conflict")
        security = await self._security()
        attempt_id = oauth_write_refresh_attempt_id(execution)
        request = OAuthRefreshRequest(
            user_id=snapshot.user_id,
            connection_id=snapshot.connection_id,
            capability=ConnectionCapability.MAIL_SEND
            if snapshot.action == "mail.send"
            else ConnectionCapability.CALENDAR_WRITE,
            attempt_id=attempt_id,
        )
        if not pending:
            closed = await security.coordinator.read_result(
                user_id=snapshot.user_id,
                connection_id=snapshot.connection_id,
                attempt_id=attempt_id,
            )
            if closed is None:
                raise OAuthRefreshError()
            return replace(
                await security.coordinator.read_current(request),
                refreshed=True,
                attempt_id=attempt_id,
            )
        provider = await asyncio.to_thread(self._oauth_provider, snapshot.provider)
        return await security.coordinator.refresh(request, provider)

    def _oauth_provider(self, provider: str) -> OAuthRefreshProvider:
        """只为已通过持久 401 证明的固定供应商读取其唯一 OAuth client Secret。"""
        settings = self._settings
        if provider == "google":
            return GoogleOAuthAdapter(
                settings.google_client_id,
                settings.read_secret_file(settings.google_client_secret_file).get_secret_value(),
                settings.google_redirect_uri,
            )
        if provider == "microsoft":
            return MicrosoftOAuthAdapter(
                settings.microsoft_client_id,
                settings.read_secret_file(settings.microsoft_client_secret_file).get_secret_value(),
                settings.microsoft_redirect_uri,
            )
        raise OAuthRefreshError("oauth_credential_state_conflict")


def build_trusted_action_registry(
    *, session_factory: ManagedAsyncSessionMaker, settings: Settings
) -> CredentialBoundProviderRegistry:
    """构造 API/Worker 共用的固定生产 registry；调用本函数及 slot 检查均无 I/O。"""
    return CredentialBoundProviderRegistry(session_factory=session_factory, settings=settings)


def _microsoft_reply_facts(
    source: MailReplySourceHeaders | None,
    *,
    mode: MailMode,
    account_email: str,
) -> tuple[MicrosoftReplyRecipientFact, ...]:
    """从来源头而非命令收件人计算 Graph 直接 reply/replyAll 的唯一收件人证明。

    Reply-To 显式空时才回退 From。replyAll 保留 To/Cc 分类，去除当前账户与跨字段
    重复项，永不扩散源 Bcc。旧缓存或畸形头返回空事实，让既有纯预检拒绝审批。
    """
    if source is None or mode is MailMode.NEW:
        return ()
    try:
        headers = source.headers
        sender = _reply_header_addresses(headers["from"])
        reply_to = _reply_header_addresses(headers["reply-to"])
        if len(sender) != 1:
            return ()
        to = reply_to or sender
        cc: tuple[str, ...] = ()
        if mode is MailMode.REPLY_ALL:
            account = normalize_mailbox_address(account_email)
            to = tuple(
                address
                for address in (*to, *_reply_header_addresses(headers["to"]))
                if address != account
            )
            cc = tuple(
                address for address in _reply_header_addresses(headers["cc"]) if address != account
            )
        to, cc, bcc = normalize_mail_recipients(to, cc, ())
        return (
            MicrosoftReplyRecipientFact(
                connection_id=source.connection_id,
                source_thread_id=source.thread_id,
                source_message_id=source.message_id,
                mode=mode,
                to=to,
                cc=cc,
                bcc=bcc,
            ),
        )
    except (KeyError, AttributeError, HeaderParseError, IndexError, TypeError, ValueError):
        return ()


def _reply_header_addresses(value: str) -> tuple[str, ...]:
    """严格解析同步器生成的平面地址头；拒绝折叠、命名组或解析器自动修复的坏值。"""
    if type(value) is not str or "\r" in value or "\n" in value:
        raise ValueError("reply source header is invalid")
    parsed = HeaderRegistry()("to", value)
    if (
        not isinstance(parsed, AddressHeader)
        or parsed.defects
        or any(group.display_name is not None for group in parsed.groups)
    ):
        raise ValueError("reply source header is invalid")
    return tuple(normalize_mailbox_address(address.addr_spec) for address in parsed.addresses)
