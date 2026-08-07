"""定义供应商中立、不可变且不泄漏 SDK 类型的 OAuth 应用端口。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from unicodedata import category

from ai_employee.domain.connections import ConnectionCapability

MAX_OAUTH_TOKEN_LENGTH = 8192
"""单个 OAuth opaque token 的最大字符数，避免 malformed 响应放大内存/密文。"""

MAX_OAUTH_SCOPE_LENGTH = 512
"""单个供应商 scope 的最大字符数，覆盖正常 scope 且限制异常 JSON。"""

MAX_OAUTH_EXPIRES_IN = 10 * 365 * 24 * 60 * 60
"""接受的最大 token 有效期（十年），确保后续 ``timedelta`` 不会溢出。"""

MAX_PROVIDER_ACCOUNT_ID_LENGTH = 255
MAX_PROVIDER_TENANT_ID_LENGTH = 255
MAX_ACCOUNT_EMAIL_LENGTH = 320
MAX_ACCOUNT_TYPE_LENGTH = 32


def _require_boundary_text(
    value: object,
    field_name: str,
    *,
    allow_empty: bool = False,
    max_length: int | None = None,
) -> None:
    """校验供应商边界文本，拒绝类型错误、空白、控制字符和异常长值。

    Token 与身份字段都是供应商返回的安全关键事实；应用层不应静默 trim 后改变 opaque 值，
    因此发现首尾空白时直接拒绝，让适配器把 malformed 响应分类为失败。

    Args:
        value: 待校验的边界值。
        field_name: 仅用于稳定、无敏感内容的异常消息。
        allow_empty: 是否允许精确空串；仅 Google 的 tenant 兼容字段使用。
        max_length: 可选的字符数上限；不记录实际长度或原始内容。

    Raises:
        TypeError: 值不是字符串。
        ValueError: 值不是允许的规范文本。
    """
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if allow_empty and value == "":
        return
    if value == "" or value.strip() == "" or value != value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if any(char.isspace() or category(char).startswith("C") for char in value):
        raise ValueError(f"{field_name} contains invalid whitespace or control characters")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{field_name} exceeds maximum length")


class OAuthProvider(StrEnum):
    """当前 M2 允许在组合根固定装配的 OAuth 供应商。"""

    GOOGLE = "google"
    MICROSOFT = "microsoft"


@dataclass(frozen=True, slots=True)
class OAuthAuthorizationRequest:
    """供应商适配器构造授权 URL 所需的最小类型化参数。

    ``state``、PKCE challenge 与 OIDC nonce 只在当前请求内短暂存在；调用方不得记录或
    回显这些值。``requested_scopes`` 已由适配器自己的能力映射得出，应用层不会解释
    Gmail、Graph 等供应商 scope 字符串。
    """

    state: str
    code_challenge: str
    requested_scopes: frozenset[str]
    oidc_nonce: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    """规范化 OAuth token 交换结果，保留供应商实际授予的 scope 事实。"""

    access_token: str
    refresh_token: str | None
    expires_in: int
    granted_scopes: frozenset[str]
    id_token: str | None = None

    def __post_init__(self) -> None:
        """拒绝 malformed token，避免空密文或无效过期时间进入持久化。"""
        _require_boundary_text(
            self.access_token,
            "access_token",
            max_length=MAX_OAUTH_TOKEN_LENGTH,
        )
        if self.refresh_token is not None:
            _require_boundary_text(
                self.refresh_token,
                "refresh_token",
                max_length=MAX_OAUTH_TOKEN_LENGTH,
            )
        if type(self.expires_in) is not int or not 1 <= self.expires_in <= MAX_OAUTH_EXPIRES_IN:
            raise ValueError("expires_in must be a positive bounded integer")
        if type(self.granted_scopes) is not frozenset:
            raise TypeError("granted_scopes must be a frozenset")
        for scope in self.granted_scopes:
            _require_boundary_text(
                scope,
                "granted_scope",
                max_length=MAX_OAUTH_SCOPE_LENGTH,
            )
        if self.id_token is not None:
            _require_boundary_text(
                self.id_token,
                "id_token",
                max_length=MAX_OAUTH_TOKEN_LENGTH,
            )


@dataclass(frozen=True, slots=True)
class OAuthAccount:
    """规范化连接唯一身份，不使用可变邮箱作为供应商主键。"""

    provider_account_id: str
    account_email: str
    provider_tenant_id: str
    account_type: str

    def __post_init__(self) -> None:
        """拒绝空身份键、邮箱和账户类型；Google tenant 仅允许精确空串。"""
        _require_boundary_text(
            self.provider_account_id,
            "provider_account_id",
            max_length=MAX_PROVIDER_ACCOUNT_ID_LENGTH,
        )
        _require_boundary_text(
            self.account_email,
            "account_email",
            max_length=MAX_ACCOUNT_EMAIL_LENGTH,
        )
        _require_boundary_text(
            self.provider_tenant_id,
            "provider_tenant_id",
            allow_empty=True,
            max_length=MAX_PROVIDER_TENANT_ID_LENGTH,
        )
        _require_boundary_text(
            self.account_type,
            "account_type",
            max_length=MAX_ACCOUNT_TYPE_LENGTH,
        )
        if self.account_email.count("@") != 1:
            raise ValueError("account_email must contain one @")
        local, domain = self.account_email.split("@")
        if not local or not domain:
            raise ValueError("account_email must contain a local part and domain")


class OAuthRevocationStatus(StrEnum):
    """区分供应商确认撤销与没有窄撤销端点两种可信结果。"""

    REVOKED = "revoked"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class OAuthRevocationResult:
    """返回不含 token、响应正文或账户资料的撤销结果。"""

    status: OAuthRevocationStatus
    error_code: str | None = None


class OAuthProviderAdapter(Protocol):
    """定义 OAuth 编排可调用的固定供应商适配器边界。

    网络、SDK、供应商端点和错误解析只能位于实现该端口的组合/集成层。传输异常必须按原有
    分类继续抛出，不能伪造 ``REVOKED`` 或 ``UNSUPPORTED`` 成功结果。
    """

    provider: OAuthProvider

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """返回满足精确能力集合所需的供应商 scope。"""
        ...

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """构造包含 state、PKCE 与可选 OIDC nonce 的授权 URL。"""
        ...

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """交换一次性授权码并返回规范化 token。"""
        ...

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """校验必要的 OIDC 绑定并读取规范化账户身份。"""
        ...

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """刷新 access token；未轮换 refresh token 时返回 ``None``。"""
        ...

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """撤销 token，或明确报告供应商不支持窄撤销端点。"""
        ...
