"""固定 Google/Microsoft 的无命令隐私 GET，不复用需要完整命令的写 resolver。

响应只用于一次有限观察；最小 ID/ETag、存在或 404 都不足证明精确审批命令结果，
因此新观察永远 unknown。客户端不读取响应正文、不分页、不 refresh，也不缓存 token。
"""

import asyncio
from urllib.parse import quote

import httpx
from cryptography.exceptions import InvalidTag
from sqlalchemy.exc import DBAPIError

from ai_employee.application.ports.encryption import (
    EncryptionBoundaryError,
    EncryptionKeyVersionError,
)
from ai_employee.application.ports.oauth import OAuthRevocationResult, OAuthRevocationStatus
from ai_employee.application.ports.trusted_actions import ProviderWriteOutcome
from ai_employee.application.use_cases.auth import Clock
from ai_employee.application.use_cases.privacy import (
    PrivacyDeletionBinding,
    PrivacyReconciliationTarget,
)
from ai_employee.domain.actions import ProviderWriteOutcomeKind
from ai_employee.infrastructure.db.repositories.oauth_lifecycle import oauth_cleanup_lock_contended
from ai_employee.infrastructure.db.repositories.privacy_reconciliation import (
    SqlAlchemyPrivacyReconciliationStore,
)
from ai_employee.infrastructure.security.encryption import AeadCipher


class FakePrivacyRevoker:
    """APP_TEST_MODE 固定使用的无网络撤销端口；明确保留 Microsoft delegated 不支持语义。"""

    def __init__(self, *, provider: str) -> None:
        """固定两种供应商，禁止将任意 provider 字符串转成外部请求。"""
        if provider not in {"google", "microsoft"}:
            raise ValueError("privacy supports only Google and Microsoft")
        self._provider = provider

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """只返回合成结果，不保存、记录或转发传入 token。"""
        del token
        return OAuthRevocationResult(
            OAuthRevocationStatus.UNSUPPORTED
            if self._provider == "microsoft"
            else OAuthRevocationStatus.REVOKED,
            "microsoft_token_revoke_unsupported" if self._provider == "microsoft" else None,
        )


class PrivacyProviderReader:
    """借用窄持久准入和单凭据 cipher，只对精确连接执行一次有界读取。"""

    def __init__(
        self,
        *,
        store: SqlAlchemyPrivacyReconciliationStore,
        cipher: AeadCipher,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """transport 仅供合成 HTTP 契约测试；生产默认使用有明确超时的标准客户端。"""
        self._store, self._cipher, self._clock, self._transport = store, cipher, clock, transport

    async def reconcile(
        self,
        *,
        binding: PrivacyDeletionBinding,
        target: PrivacyReconciliationTarget,
    ) -> ProviderWriteOutcome:
        """重验持久资格后解密当前 access，在总时限内 GET，始终诚实返回 unknown。

        缺少资源定位、当前 access 或只读能力时是明确零网络结果；绑定/租约异常继续
        抛给删除 Worker，不能伪装成已核对。任何响应均不提供安全写重试授权。
        """
        route = self._route(target)
        if route is None:
            return self._unknown(target, "privacy_reconciliation_location_unavailable")
        token: str | None = None
        try:
            try:
                encrypted = await self._store.read_access(
                    binding=binding, target=target, now=self._clock.now()
                )
            except DBAPIError as error:
                if not oauth_cleanup_lock_contended(error):
                    raise
                return self._unknown(target, "privacy_reconciliation_access_unavailable")
            if encrypted is None:
                return self._unknown(target, "privacy_reconciliation_access_unavailable")
            try:
                token = self._cipher.decrypt(
                    encrypted,
                    (f"{binding.user_id}:{target.dispatch.connection_id}:access_token").encode(
                        "ascii"
                    ),
                ).decode("utf-8")
            except (
                InvalidTag,
                UnicodeDecodeError,
                EncryptionKeyVersionError,
                EncryptionBoundaryError,
            ):
                return self._unknown(target, "privacy_reconciliation_access_unavailable")
            if (
                not token
                or len(token) > 8192
                or any(char.isspace() or ord(char) < 32 for char in token)
            ):
                return self._unknown(target, "privacy_reconciliation_access_unavailable")
            url, params = route
            try:
                # 不调用 response.json/aread：供应商即使忽略 fields 也不能把完整正文
                # 带入内存、异常或审计；一次 GET 的状态本身仍只有 unknown 含义。
                async with (
                    asyncio.timeout(10),
                    httpx.AsyncClient(
                        timeout=httpx.Timeout(5, connect=3),
                        follow_redirects=False,
                        transport=self._transport,
                    ) as client,
                    client.stream(
                        "GET", url, params=params, headers={"Authorization": f"Bearer {token}"}
                    ),
                ):
                    return self._unknown(target, "privacy_reconciliation_unproven")
            except (httpx.HTTPError, TimeoutError):
                return self._unknown(target, "privacy_reconciliation_read_failed")
        finally:
            token = None

    @staticmethod
    def _route(target: PrivacyReconciliationTarget) -> tuple[str, dict[str, str]] | None:
        """只允许四种固定动作对应的单资源路径，不猜测搜索窗口或接受可变 base URL。"""
        snapshot = target.dispatch
        resource = target.resource_id
        if not PrivacyProviderReader._safe_identifier(resource):
            return None
        assert resource is not None
        encoded = quote(resource, safe="")
        if snapshot.action == "mail.send":
            if snapshot.provider == "google":
                return f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{encoded}", {
                    "format": "minimal",
                    "fields": "id",
                }
            if snapshot.provider == "microsoft":
                return f"https://graph.microsoft.com/v1.0/me/messages/{encoded}", {"$select": "id"}
        elif snapshot.action in {"calendar.create", "calendar.update", "calendar.restore"}:
            if not PrivacyProviderReader._safe_identifier(snapshot.calendar_id):
                return None
            assert snapshot.calendar_id is not None
            calendar = quote(snapshot.calendar_id, safe="")
            if snapshot.provider == "google":
                return (
                    f"https://www.googleapis.com/calendar/v3/calendars/{calendar}/events/{encoded}",
                    {"fields": "id,etag"},
                )
            if snapshot.provider == "microsoft":
                return (
                    f"https://graph.microsoft.com/v1.0/me/calendars/{calendar}/events/{encoded}",
                    {"$select": "id,changeKey"},
                )
        return None

    @staticmethod
    def _safe_identifier(value: str | None) -> bool:
        """拒绝空白、控制字符、超长或会被 HTTP 客户端规范化的 dot path。"""
        return (
            value is not None
            and 0 < len(value) <= 1024
            and value not in {".", ".."}
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        )

    @staticmethod
    def _unknown(target: PrivacyReconciliationTarget, reason: str) -> ProviderWriteOutcome:
        """使用已有规范结果类型，明确无 retryable/Retry-After/伪造 provider result。"""
        return ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.UNKNOWN,
            retryable=False,
            retry_after_seconds=None,
            provider_resource_id=None,
            provider_request_id=None,
            correlation_id=str(target.dispatch.execution.execution_id),
            provider_url=None,
            error_code=reason,
        )
