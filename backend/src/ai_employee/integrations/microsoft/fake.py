"""提供 Microsoft 测试模式的离线 OAuth 与邮件读取适配器。

该模块只返回合成 token、账户、授权 URL、folder 与 Delta cursor，不导入或创建 HTTP
客户端。真实 Microsoft adapter 仅由关闭 ``APP_TEST_MODE`` 的组合根装配或显式注入；
测试模式组合根无条件覆盖两个供应商为内置 fake，避免误配访问 Google/Microsoft。
"""

from __future__ import annotations

import base64
import hashlib
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import datetime
from threading import Lock
from typing import ClassVar

from ai_employee.application.ports.mail import MailReader, MailScope, MailSyncPage
from ai_employee.application.ports.oauth import (
    OAuthAccount,
    OAuthAuthorizationRequest,
    OAuthProvider,
    OAuthProviderAdapter,
    OAuthRevocationResult,
    OAuthRevocationStatus,
    OAuthTokenSet,
)
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.integrations.microsoft.oauth import (
    MICROSOFT_BASE_SCOPES,
    MICROSOFT_CAPABILITY_SCOPES,
    build_authorization_url,
)


class FakeMicrosoftMailReader(MailReader):
    """返回固定空邮件页和独立 cursor 的测试模式离线读取器。

    测试模式只需证明目录发现、逐 scope cursor 与 Durable Worker 编排；不复制真实 Graph
    JSON，也不构造 HTTP 客户端。空消息页避免把伪造正文混入简报或草稿流程，同时仍通过
    一个稳定合成 folder 验证 Microsoft 首次 discovery 路径。
    """

    provider = "microsoft"
    _SCOPE_KEY = "fake-microsoft-mailbox"
    _CURSOR = "fake-microsoft-mail-delta-v1"

    async def list_sync_scopes(self) -> tuple[MailScope, ...]:
        """返回一个固定、非 Drafts/Junk/Deleted 的合成收件 scope。"""
        return (MailScope(self._SCOPE_KEY, "Synthetic Microsoft Inbox", "inbox"),)

    def initial_pages(
        self,
        scope_key: str,
        *,
        since: datetime,
    ) -> AsyncIterator[MailSyncPage]:
        """为固定 scope 返回带最终 cursor 的空初始页。"""
        del since
        self._require_scope(scope_key)
        return self._pages()

    def sync_pages(self, scope_key: str, cursor: str) -> AsyncIterator[MailSyncPage]:
        """只接受该 fake 自己生成的 cursor，并返回幂等空增量页。"""
        self._require_scope(scope_key)
        if cursor != self._CURSOR:
            raise ValueError("Fake Microsoft mail cursor is invalid")
        return self._pages()

    async def _pages(self) -> AsyncIterator[MailSyncPage]:
        """输出单个空页面，使应用层仍能原子创建或推进真实 cursor 行。"""
        yield MailSyncPage((), None, self._CURSOR)

    @classmethod
    def _require_scope(cls, scope_key: str) -> None:
        """拒绝测试调用方把任意 provider scope 送入固定 fake。"""
        if scope_key != cls._SCOPE_KEY:
            raise ValueError("Fake Microsoft mail scope is invalid")


class FakeMicrosoftOAuthAdapter(OAuthProviderAdapter):
    """返回固定 Microsoft 合成事实的测试适配器。

    所有方法都在进程内完成；``exchange_code`` 与 ``refresh`` 不会解析输入中的供应商
    数据，也不会根据输入构造网络请求。scope 映射仍复用真实适配器的冻结常量，确保
    测试模式不能通过 fake 请求 M2 之外的权限。
    """

    provider = OAuthProvider.MICROSOFT
    _MAX_SCOPE_RECORDS: ClassVar[int] = 256
    _pending_scopes: ClassVar[OrderedDict[str, frozenset[str]]] = OrderedDict()
    _refresh_scopes: ClassVar[OrderedDict[str, frozenset[str]]] = OrderedDict()
    _scope_lock: ClassVar[Lock] = Lock()

    def __init__(self, *, client_id: str, redirect_uri: str) -> None:
        """保存只用于生成合成授权 URL 的公开配置。"""
        self._client_id = client_id
        self._redirect_uri = redirect_uri

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """按与真实 Microsoft adapter 相同的能力映射返回固定 delegated scope。"""
        if type(capabilities) is not frozenset:
            raise TypeError("Microsoft capabilities must be a frozenset")
        scopes = set(MICROSOFT_BASE_SCOPES)
        for capability in capabilities:
            if type(capability) is not ConnectionCapability:
                raise ValueError("Microsoft capability is invalid")
            try:
                scopes.update(MICROSOFT_CAPABILITY_SCOPES[capability])
            except KeyError as error:
                raise ValueError("Microsoft capability is invalid") from error
        return frozenset(scopes)

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """使用真实端点格式构造 URL，但不进行任何网络访问。"""
        authorization_url = build_authorization_url(
            client_id=self._client_id,
            redirect_uri=self._redirect_uri,
            state=request.state,
            code_challenge=request.code_challenge,
            scopes=request.requested_scopes,
            oidc_nonce=request.oidc_nonce,
        )
        # OAuth 端口的 exchange_code 只收到 verifier；以 S256 challenge 作为短期键，
        # 让跨请求重建的 fake 仍能复现本次实际选择。只保存 scope 集合，不保存 state、
        # verifier 或 token，并在交换时一次消费，且总量有界，避免测试进程内累积秘密。
        with self._scope_lock:
            self._remember(self._pending_scopes, request.code_challenge, request.requested_scopes)
        return authorization_url

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """返回固定 token；参数只为匹配端口形状，绝不创建 HTTP 客户端。"""
        del code
        challenge = _code_challenge(verifier)
        with self._scope_lock:
            granted_scopes = self._pending_scopes.pop(challenge, None)
            if granted_scopes is None:
                # 未从 fake 授权 URL 发起的直接调用只能得到身份基础 scope，fail closed
                # 而不是凭空打开邮件或日历读取能力。
                granted_scopes = frozenset(MICROSOFT_BASE_SCOPES)
            refresh_token = _refresh_token_for_scopes(granted_scopes)
            self._remember(self._refresh_scopes, refresh_token, granted_scopes)
        return OAuthTokenSet(
            access_token="fake-microsoft-access",
            refresh_token=refresh_token,
            expires_in=3600,
            granted_scopes=granted_scopes,
        )

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """返回固定脱敏账户；token/nonce 不会被写入日志或发往外部。"""
        del token, expected_nonce_hash
        return OAuthAccount(
            provider_account_id="synthetic-tenant:synthetic-microsoft-user",
            account_email="test-mode@microsoft.example.test",
            provider_tenant_id="synthetic-tenant",
            account_type="work_school",
        )

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """返回固定刷新 token，测试模式不触发供应商请求。"""
        with self._scope_lock:
            granted_scopes = self._refresh_scopes.get(refresh_token)
            if granted_scopes is None:
                granted_scopes = frozenset(MICROSOFT_BASE_SCOPES)
            refreshed_token = _refresh_token_for_scopes(granted_scopes)
        return OAuthTokenSet(
            access_token="fake-microsoft-refreshed-access",
            refresh_token=refreshed_token,
            expires_in=3600,
            granted_scopes=granted_scopes,
        )

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """本地 fake 断开只报告不支持窄撤销，不调用任何 Graph 广泛端点。"""
        del token
        return OAuthRevocationResult(
            OAuthRevocationStatus.UNSUPPORTED,
            "microsoft_token_revoke_unsupported",
        )

    @classmethod
    def _remember(
        cls,
        records: OrderedDict[str, frozenset[str]],
        key: str,
        scopes: frozenset[str],
    ) -> None:
        """写入有界 scope 记录；调用方必须已持有类级锁。"""
        records[key] = frozenset(scopes)
        records.move_to_end(key)
        while len(records) > cls._MAX_SCOPE_RECORDS:
            records.popitem(last=False)


def _code_challenge(verifier: str) -> str:
    """按 PKCE S256 从 verifier 重建 fake scope 记录键。"""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _refresh_token_for_scopes(scopes: frozenset[str]) -> str:
    """为不同合成 scope 集合生成互不混淆的刷新 token 键。

    默认双读集合保留历史 fake 的固定 token 文本；受限集合使用短哈希后缀，避免多个
    测试用户共享一个固定 key 后互相覆盖 scope 事实。后缀不是凭据，也不从真实数据派生。
    """
    default_scopes = frozenset(
        {
            *MICROSOFT_BASE_SCOPES,
            "Mail.Read",
            "Calendars.Read",
        }
    )
    if scopes == default_scopes:
        return "fake-microsoft-refresh"
    digest = hashlib.sha256("\x00".join(sorted(scopes)).encode("utf-8")).hexdigest()[:16]
    return f"fake-microsoft-refresh-{digest}"


__all__ = ["FakeMicrosoftMailReader", "FakeMicrosoftOAuthAdapter"]
