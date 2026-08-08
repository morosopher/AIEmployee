"""实现 Microsoft delegated OIDC/OAuth 的安全、供应商中立适配器边界。

本模块只负责 OAuth 授权、令牌交换/刷新、OIDC discovery/JWKS 验签和 Graph ``/me``
身份读取；供应商 JSON 不会离开集成层。PyJWT 依赖采用 MIT 许可证且有持续维护，
仅用于经过签名、audience、issuer、exp 与 nonce 校验的 ID token；任何未验证 claims
都不会进入连接身份，也不会提供通用 JWT parser。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, cast
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt import InvalidTokenError
from jwt.algorithms import RSAAlgorithm
from jwt.exceptions import InvalidKeyError, PyJWTError

from ai_employee.application.ports.oauth import (
    MAX_ACCOUNT_EMAIL_LENGTH,
    MAX_OAUTH_EXPIRES_IN,
    MAX_OAUTH_SCOPE_LENGTH,
    MAX_OAUTH_TOKEN_LENGTH,
    MAX_PROVIDER_ACCOUNT_ID_LENGTH,
    MAX_PROVIDER_TENANT_ID_LENGTH,
    OAuthAccount,
    OAuthAuthorizationRequest,
    OAuthProvider,
    OAuthProviderAdapter,
    OAuthRevocationResult,
    OAuthRevocationStatus,
    OAuthTokenSet,
)
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import (
    DomainError,
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)

MICROSOFT_AUTHORIZATION_URL: Final = (
    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
)
MICROSOFT_TOKEN_URL: Final = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_DISCOVERY_URL: Final = (
    "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration"
)
MICROSOFT_GRAPH_ME_URL: Final = "https://graph.microsoft.com/v1.0/me"
MICROSOFT_JWKS_URL: Final = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
_MICROSOFT_LIVE_JWKS_URL: Final = "https://login.live.com/common/discovery/v2.0/keys"

# Microsoft v2 delegated OAuth 的基础身份与离线访问 scope。集合和映射均冻结，防止
# 组合根或测试在运行时追加 Contacts、应用权限或 Mail.ReadWrite 等越界数据源。
MICROSOFT_BASE_SCOPES: Final[frozenset[str]] = frozenset(
    {"openid", "profile", "email", "offline_access"}
)
MICROSOFT_CAPABILITY_SCOPES: Final[Mapping[ConnectionCapability, frozenset[str]]] = (
    MappingProxyType(
        {
            ConnectionCapability.MAIL_READ: frozenset({"Mail.Read"}),
            ConnectionCapability.MAIL_SEND: frozenset({"Mail.Send"}),
            ConnectionCapability.CALENDAR_READ: frozenset({"Calendars.Read"}),
            ConnectionCapability.CALENDAR_WRITE: frozenset({"Calendars.ReadWrite"}),
        }
    )
)
MICROSOFT_ALLOWED_SCOPES: Final[frozenset[str]] = frozenset(
    set(MICROSOFT_BASE_SCOPES).union(*MICROSOFT_CAPABILITY_SCOPES.values())
)
# 旧调用方/契约测试需要一个稳定列表；新 provider-neutral 用例使用 ``scopes_for``。
MICROSOFT_SCOPES: Final[list[str]] = [
    "openid",
    "profile",
    "email",
    "offline_access",
    "Mail.Read",
    "Calendars.Read",
]

MICROSOFT_PERSONAL_TENANT_ID: Final = "9188040d-6c67-4c5b-b112-36a304b66dad"
MICROSOFT_DISCOVERY_CACHE_TTL_SECONDS: Final[float] = 300.0
MICROSOFT_MAX_JWKS_KEYS: Final[int] = 32
MICROSOFT_TIMEOUT_SECONDS: Final[float] = 10.0
MICROSOFT_CONNECT_TIMEOUT_SECONDS: Final[float] = 3.0

_TENANT_TEXT_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
_ISSUER_TEMPLATE_RE = re.compile(
    r"^https://(?P<host>login\.microsoftonline\.com|login\.live\.com)/(?P<tenant>[^/]+)/v2\.0/?$"
)
_ALLOWED_JWKS_HOSTS = frozenset({"login.microsoftonline.com", "login.live.com"})


class _OidcNonceMismatch(Exception):
    """内部标记，避免把已验签 token 的 nonce 失败误报为签名失败。"""


def _require_microsoft_text(
    value: object,
    field_name: str,
    *,
    max_length: int = MAX_OAUTH_TOKEN_LENGTH,
) -> str:
    """收窄 Microsoft opaque 文本，拒绝空白、控制字符与异常长度。"""
    if not isinstance(value, str):
        raise TypeError(f"Microsoft {field_name} is invalid")
    if value == "" or value.strip() != value or value.strip() == "":
        raise ValueError(f"Microsoft {field_name} is invalid")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"Microsoft {field_name} is invalid")
    if len(value) > max_length:
        raise ValueError(f"Microsoft {field_name} is invalid")
    return value


def _require_positive_expiry(value: object) -> int:
    """校验 token 有效期，避免异常数字进入日期运算。"""
    if type(value) is not int or not 1 <= value <= MAX_OAUTH_EXPIRES_IN:
        raise ValueError("Microsoft expires_in is invalid")
    return value


def _require_json_object(payload: object, operation: str) -> Mapping[str, object]:
    """将第三方 JSON 顶层收窄为 object，避免列表/字符串泄漏 AttributeError。"""
    if not isinstance(payload, Mapping):
        raise TypeError(f"Microsoft {operation} JSON object is invalid")
    return cast(Mapping[str, object], payload)


def _read_json_object(response: httpx.Response, operation: str) -> Mapping[str, object]:
    """读取 JSON object；解码异常只在适配器内部存在。"""
    try:
        return _require_json_object(response.json(), operation)
    except (TypeError, ValueError):
        raise PermanentProviderError(
            error_code="microsoft_oauth_invalid_response",
            message="Microsoft OAuth response is invalid",
        ) from None


def _normalize_scope_string(value: object, *, field_name: str = "scope") -> frozenset[str]:
    """解析空格分隔 scope 并 fail closed 拒绝未知权限。"""
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"Microsoft {field_name} is invalid")
    if len(value) > MAX_OAUTH_TOKEN_LENGTH:
        raise ValueError(f"Microsoft {field_name} is invalid")
    if any((char.isspace() and char != " ") or ord(char) < 32 for char in value):
        raise ValueError(f"Microsoft {field_name} is invalid")
    scopes = frozenset(value.split(" "))
    if not scopes or any(not scope or len(scope) > MAX_OAUTH_SCOPE_LENGTH for scope in scopes):
        raise ValueError(f"Microsoft {field_name} is invalid")
    if not scopes.issubset(MICROSOFT_ALLOWED_SCOPES):
        raise ValueError(f"Microsoft {field_name} is not allowed")
    return scopes


def _validate_requested_scopes(value: object) -> frozenset[str]:
    """验证授权 URL 的 scope 集合只来自冻结 M2 委托权限。"""
    if type(value) is not frozenset or not value:
        raise ValueError("Microsoft requested scopes are invalid")
    normalized = frozenset(
        _require_microsoft_text(scope, "requested scope", max_length=MAX_OAUTH_SCOPE_LENGTH)
        for scope in value
    )
    if not normalized.issubset(MICROSOFT_ALLOWED_SCOPES):
        raise ValueError("Microsoft requested scopes are not allowed")
    return normalized


def _ordered_scopes(scopes: frozenset[str]) -> tuple[str, ...]:
    """按固定身份、邮件、日历顺序生成确定性授权 URL。"""
    order = {
        "openid": 0,
        "profile": 1,
        "email": 2,
        "offline_access": 3,
        "Mail.Read": 4,
        "Mail.Send": 5,
        "Calendars.Read": 6,
        "Calendars.ReadWrite": 7,
    }
    return tuple(sorted(scopes, key=lambda item: (order.get(item, 99), item)))


def _timeout() -> httpx.Timeout:
    """所有 Microsoft 请求使用显式连接与总超时。"""
    return httpx.Timeout(MICROSOFT_TIMEOUT_SECONDS, connect=MICROSOFT_CONNECT_TIMEOUT_SECONDS)


def _retry_after(response: httpx.Response) -> int | None:
    """只接受有限非负整数 Retry-After，不保存供应商正文。"""
    try:
        value = int(response.headers.get("Retry-After", ""))
        if value < 0:
            return None
        float(value)
        return value
    except (TypeError, ValueError, OverflowError):
        return None


def _http_error(*, status_code: int, operation: str, retry_after: int | None) -> DomainError:
    """将 Microsoft HTTP 状态转换为稳定、无正文领域错误。"""
    if status_code == 429 or status_code >= 500:
        return TransientProviderError(
            error_code="microsoft_oauth_unavailable",
            message="Microsoft OAuth is temporarily unavailable",
            retry_after=retry_after,
        )
    if status_code in {401, 403}:
        return UserActionRequiredError(
            error_code="microsoft_reauthorization_required",
            message="Microsoft authorization requires user action",
        )
    if operation == "token exchange" and status_code == 400:
        return PermanentProviderError(
            error_code="microsoft_oauth_rejected",
            message="Microsoft OAuth request was rejected",
        )
    return PermanentProviderError(
        error_code="microsoft_oauth_rejected",
        message="Microsoft OAuth request was rejected",
    )


def _requires_admin_consent(response: httpx.Response) -> bool:
    """仅检查错误 JSON 中的固定 AADSTS65001 代码，不保存或回显描述文本。"""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, Mapping):
        return False
    candidates: list[object] = [payload.get("error"), payload.get("error_codes")]
    candidates.append(payload.get("error_description"))
    for candidate in candidates:
        if isinstance(candidate, str) and "65001" in candidate:
            return True
        if isinstance(candidate, list) and any(str(item) == "65001" for item in candidate):
            return True
    return False


async def _request(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    operation: str,
    **kwargs: Any,
) -> httpx.Response:
    """执行固定 GET/POST 请求并清除 HTTPX 异常中的敏感请求上下文。"""
    error: DomainError | None = None
    response: httpx.Response | None = None
    try:
        if method == "GET":
            response = await client.get(url, **kwargs)
        elif method == "POST":
            response = await client.post(url, **kwargs)
        else:
            error = PermanentProviderError(
                error_code="microsoft_oauth_invalid_operation",
                message="Microsoft OAuth operation is invalid",
            )
    except httpx.TimeoutException:
        error = TransientProviderError(
            error_code="microsoft_oauth_timeout",
            message="Microsoft OAuth request timed out",
        )
    except httpx.HTTPStatusError as exc:
        response = exc.response
        error = _http_error(
            status_code=response.status_code,
            operation=operation,
            retry_after=_retry_after(response),
        )
    except httpx.RequestError:
        error = TransientProviderError(
            error_code="microsoft_oauth_request_failed",
            message="Microsoft OAuth request failed",
        )
    if error is not None:
        raise error
    if response is None:
        raise PermanentProviderError(
            error_code="microsoft_oauth_invalid_response",
            message="Microsoft OAuth response is invalid",
        )
    if not response.is_success:
        if response.status_code == 400 and _requires_admin_consent(response):
            raise UserActionRequiredError(
                error_code="microsoft_admin_consent_required",
                message="Microsoft administrator consent is required",
            )
        raise _http_error(
            status_code=response.status_code,
            operation=operation,
            retry_after=_retry_after(response),
        )
    return response


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scopes: frozenset[str] | None = None,
    oidc_nonce: str | None = None,
) -> str:
    """构建 common v2 Microsoft 授权 URL。

    Args:
        client_id: Microsoft 应用客户端 ID。
        redirect_uri: 与注册应用精确匹配的 HTTPS 回调地址。
        state: 一次性 OAuth CSRF state。
        code_challenge: S256 PKCE challenge。
        scopes: 本次实际请求的冻结 delegated scope 集合。
        oidc_nonce: OIDC nonce 原文，只在浏览器往返中出现。

    Returns:
        不含 client secret、access token 或 refresh token 的授权 URL。
    """
    requested = _validate_requested_scopes(
        scopes if scopes is not None else frozenset(MICROSOFT_SCOPES)
    )
    params = {
        "client_id": _require_microsoft_text(client_id, "client_id"),
        "redirect_uri": _require_microsoft_text(redirect_uri, "redirect_uri"),
        "response_type": "code",
        "response_mode": "query",
        "scope": " ".join(_ordered_scopes(requested)),
        "state": _require_microsoft_text(state, "state", max_length=512),
        "code_challenge": _require_microsoft_text(code_challenge, "code_challenge", max_length=512),
        "code_challenge_method": "S256",
        # 渐进授权必须让 Microsoft 重新评估新增 delegated scope；不会扩大 scope 集合。
        "prompt": "consent",
    }
    if oidc_nonce is not None:
        params["nonce"] = _require_microsoft_text(oidc_nonce, "nonce", max_length=512)
    return f"{MICROSOFT_AUTHORIZATION_URL}?{urlencode(params)}"


@dataclass(frozen=True, slots=True)
class _DiscoveryDocument:
    """经验证的 OIDC discovery 公共端点投影。"""

    issuer: str
    jwks_uri: str


@dataclass(frozen=True, slots=True)
class _CachedDiscovery:
    """带单调时间戳的有界 discovery/JWKS 缓存项。"""

    fetched_at: float
    document: _DiscoveryDocument
    jwks: tuple[Mapping[str, object], ...]


class MicrosoftOAuthAdapter(OAuthProviderAdapter):
    """实现 Microsoft 个人/工作学校账户的 delegated OIDC/OAuth。

    所有供应商响应在本类内完成类型收窄；``fetch_account`` 只有在 ID token 的签名、
    audience、expiration、issuer、tenant 与 nonce 全部通过后才调用 Graph ``/me``，并
    以 ``tenant:graph_user_id`` 作为不可变 provider account key。
    """

    provider = OAuthProvider.MICROSOFT

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        """保存非敏感客户端配置，并初始化 300 秒有界 discovery 缓存。"""
        self._client_id = _require_microsoft_text(client_id, "client_id")
        self._client_secret = _require_microsoft_text(client_secret, "client_secret")
        self._redirect_uri = _require_microsoft_text(redirect_uri, "redirect_uri")
        self._discovery_cache: _CachedDiscovery | None = None

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """返回精确能力对应的 Microsoft delegated scope 并集。"""
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
        """使用请求中的精确 scope、S256 PKCE 和 OIDC nonce 构建授权地址。"""
        return build_authorization_url(
            client_id=self._client_id,
            redirect_uri=self._redirect_uri,
            state=request.state,
            code_challenge=request.code_challenge,
            scopes=_validate_requested_scopes(request.requested_scopes),
            oidc_nonce=request.oidc_nonce,
        )

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """交换授权码并严格核对 Microsoft 实际授予 scope。"""
        normalized_code = _require_microsoft_text(code, "authorization code")
        normalized_verifier = _require_microsoft_text(verifier, "PKCE verifier")
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await _request(
                client,
                method="POST",
                url=MICROSOFT_TOKEN_URL,
                operation="token exchange",
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": self._redirect_uri,
                    "grant_type": "authorization_code",
                    "code": normalized_code,
                    "code_verifier": normalized_verifier,
                },
            )
            payload = _read_json_object(response, "token")
        return self._token_set_from_payload(payload)

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """验证 OIDC ID token 后读取 Graph ``/me`` 的稳定用户 ID 与邮箱。"""
        if (
            type(expected_nonce_hash) is not bytes
            or len(expected_nonce_hash) != hashlib.sha256().digest_size
        ):
            raise UserActionRequiredError(
                error_code="microsoft_oidc_nonce_mismatch",
                message="Microsoft OIDC verification requires user action",
            )
        if token.id_token is None:
            raise UserActionRequiredError(
                error_code="microsoft_oidc_nonce_missing",
                message="Microsoft OIDC verification requires user action",
            )
        claims = await self._verify_id_token(token.id_token, expected_nonce_hash)
        tenant = _tenant_from_claims(claims)
        normalized_access = _require_microsoft_text(token.access_token, "access_token")
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await _request(
                client,
                method="GET",
                url=MICROSOFT_GRAPH_ME_URL,
                operation="Graph me",
                headers={"Authorization": f"Bearer {normalized_access}"},
                # Graph profile 默认字段可能包含不必要的个人资料；只读取建立连接所需
                # 的稳定 ID 与邮箱候选字段，遵守最小披露边界。
                params={"$select": "id,mail,userPrincipalName"},
            )
            payload = _read_json_object(response, "Graph me")
        try:
            graph_id = _require_microsoft_text(
                payload.get("id"),
                "Graph user id",
                max_length=MAX_PROVIDER_ACCOUNT_ID_LENGTH,
            )
            if ":" in graph_id:
                # provider_account_id 使用 tenant:graph_id 规范键；拒绝分隔符注入，避免
                # 两个租户/用户组合被规范化为同一可歧义字符串。
                raise ValueError("Graph user id contains identity separator")
            mailbox = payload.get("mail") or payload.get("userPrincipalName")
            email = _require_email(mailbox)
        except (TypeError, ValueError):
            raise PermanentProviderError(
                error_code="microsoft_oauth_invalid_response",
                message="Microsoft OAuth response is invalid",
            ) from None
        provider_account_id = _require_microsoft_text(
            f"{tenant}:{graph_id}",
            "provider account id",
            max_length=MAX_PROVIDER_ACCOUNT_ID_LENGTH,
        )
        issuer_value = str(claims.get("iss", ""))
        account_type = (
            "personal"
            if tenant.casefold() == MICROSOFT_PERSONAL_TENANT_ID
            or issuer_value.startswith("https://login.live.com/")
            else "work_school"
        )
        return OAuthAccount(
            provider_account_id=provider_account_id,
            account_email=email,
            provider_tenant_id=tenant,
            account_type=account_type,
        )

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """刷新 access token；Microsoft 未返回新 refresh token 时保留旧密文。"""
        normalized_refresh = _require_microsoft_text(refresh_token, "refresh_token")
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await _request(
                client,
                method="POST",
                url=MICROSOFT_TOKEN_URL,
                operation="token refresh",
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": normalized_refresh,
                },
            )
            payload = _read_json_object(response, "token")
        return self._token_set_from_payload(payload)

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """明确报告 Microsoft delegated OAuth 没有安全窄 revoke 端点。

        ``token`` 仅用于端口形状验证；绝不调用广泛的 ``revokeSignInSessions`` 或
        directory-level ``oauth2PermissionGrants``，断开时由应用先删除本地凭据。
        """
        _require_microsoft_text(token, "revoke token")
        return OAuthRevocationResult(
            OAuthRevocationStatus.UNSUPPORTED,
            "microsoft_token_revoke_unsupported",
        )

    def _token_set_from_payload(self, payload: Mapping[str, object]) -> OAuthTokenSet:
        """把 token JSON 收窄为端口值对象，拒绝缺失/越界 scope。"""
        try:
            access = _require_microsoft_text(payload.get("access_token"), "access_token")
            expires = _require_positive_expiry(payload.get("expires_in"))
            scopes = _normalize_scope_string(payload.get("scope"))
            refresh_value = payload.get("refresh_token")
            refresh = (
                _require_microsoft_text(refresh_value, "refresh_token")
                if refresh_value is not None
                else None
            )
            id_token_value = payload.get("id_token")
            id_token = (
                _require_microsoft_text(id_token_value, "id_token")
                if id_token_value is not None
                else None
            )
            return OAuthTokenSet(
                access_token=access,
                refresh_token=refresh,
                expires_in=expires,
                granted_scopes=scopes,
                id_token=id_token,
            )
        except (TypeError, ValueError):
            raise PermanentProviderError(
                error_code="microsoft_oauth_invalid_response",
                message="Microsoft OAuth response is invalid",
            ) from None

    async def _verify_id_token(
        self,
        raw_id_token: str,
        expected_nonce_hash: bytes,
    ) -> Mapping[str, object]:
        """通过 discovery/JWKS 验证 ID token 签名和全部关键 claims。"""
        normalized = _require_microsoft_text(raw_id_token, "id_token")
        try:
            header = jwt.get_unverified_header(normalized)
            kid = _require_microsoft_text(header.get("kid"), "JWT key id", max_length=255)
            algorithm = header.get("alg")
            if algorithm != "RS256":
                raise ValueError("unsupported JWT algorithm")
        except (InvalidTokenError, TypeError, ValueError):
            raise UserActionRequiredError(
                error_code="microsoft_oidc_signature_invalid",
                message="Microsoft OIDC verification requires user action",
            ) from None

        document, keys = await self._discovery_and_keys()
        key_payload = next((key for key in keys if key.get("kid") == kid), None)
        if key_payload is None:
            # Key rotation can make a cached JWKS stale; refresh once, still bounded by one call.
            self._discovery_cache = None
            document, keys = await self._discovery_and_keys()
            key_payload = next((key for key in keys if key.get("kid") == kid), None)
        if key_payload is None:
            raise UserActionRequiredError(
                error_code="microsoft_oidc_signature_invalid",
                message="Microsoft OIDC verification requires user action",
            )
        try:
            public_key = cast(
                RSAPublicKey,
                RSAAlgorithm.from_jwk(json.dumps(key_payload, separators=(",", ":"))),
            )
        except (InvalidKeyError, PyJWTError, TypeError, ValueError):
            # JWKS 本身是供应商响应，不可信 key 参数不能变成 500 或把 PyJWT 原文
            # 暴露给 API；在验签前统一归类为固定的 invalid_response。
            raise PermanentProviderError(
                error_code="microsoft_oidc_invalid_response",
                message="Microsoft OIDC response is invalid",
            ) from None
        try:
            # 先验证签名、audience、exp 和必需 claims；不使用未验证 payload。issuer 需要
            # 从已验证的 tid 计算模板，因此暂时关闭 issuer 检查，随后立即做精确白名单比对。
            claims = jwt.decode(
                normalized,
                key=public_key,
                algorithms=["RS256"],
                audience=self._client_id,
                options={"verify_iss": False, "require": ["exp", "iss", "aud", "nonce", "tid"]},
            )
            if not isinstance(claims, Mapping):
                raise TypeError("claims object is invalid")
            tenant = _tenant_from_claims(claims)
            expected_issuer = _issuer_for_tenant(document.issuer, tenant)
            verified_issuer = _require_microsoft_text(claims.get("iss"), "issuer", max_length=512)
            allowed_issuers = {expected_issuer}
            # consumers token 在 Microsoft 文档与历史响应中可能使用 login.live.com；
            # 该主机本身是固定的个人账户 issuer 形式，绝不接受其他外部主机。
            allowed_issuers.add(
                expected_issuer.replace(
                    "https://login.microsoftonline.com/",
                    "https://login.live.com/",
                )
            )
            if verified_issuer not in allowed_issuers:
                raise ValueError("issuer mismatch")
            nonce = _require_microsoft_text(claims.get("nonce"), "OIDC nonce", max_length=512)
            if not hmac.compare_digest(
                hashlib.sha256(nonce.encode("utf-8")).digest(), expected_nonce_hash
            ):
                raise _OidcNonceMismatch
            return cast(Mapping[str, object], claims)
        except _OidcNonceMismatch:
            raise UserActionRequiredError(
                error_code="microsoft_oidc_nonce_mismatch",
                message="Microsoft OIDC verification requires user action",
            ) from None
        except (InvalidTokenError, PyJWTError):
            raise UserActionRequiredError(
                error_code="microsoft_oidc_signature_invalid",
                message="Microsoft OIDC verification requires user action",
            ) from None
        except (TypeError, ValueError, KeyError):
            raise UserActionRequiredError(
                error_code="microsoft_oidc_issuer_invalid",
                message="Microsoft OIDC verification requires user action",
            ) from None

    async def _discovery_and_keys(
        self,
    ) -> tuple[_DiscoveryDocument, tuple[Mapping[str, object], ...]]:
        """读取并缓存 discovery/JWKS，缓存生命周期固定且有界。"""
        now = time.monotonic()
        cached = self._discovery_cache
        if cached is not None and now - cached.fetched_at < MICROSOFT_DISCOVERY_CACHE_TTL_SECONDS:
            return cached.document, cached.jwks
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await _request(
                client,
                method="GET",
                url=MICROSOFT_DISCOVERY_URL,
                operation="OIDC discovery",
            )
            payload = _read_json_object(response, "OIDC discovery")
            document = _parse_discovery(payload)
            jwks_response = await _request(
                client,
                method="GET",
                url=document.jwks_uri,
                operation="OIDC JWKS",
            )
            jwks_payload = _read_json_object(jwks_response, "OIDC JWKS")
        raw_keys = jwks_payload.get("keys")
        if not isinstance(raw_keys, list) or len(raw_keys) > MICROSOFT_MAX_JWKS_KEYS:
            raise PermanentProviderError(
                error_code="microsoft_oidc_invalid_response",
                message="Microsoft OIDC response is invalid",
            )
        keys: list[Mapping[str, object]] = []
        for raw in raw_keys:
            if isinstance(raw, Mapping) and raw.get("kty") == "RSA":
                # Microsoft common JWKS 的 RSA 条目可能省略可选 ``alg``；真正的签名算法
                # 仍在下方由 JWT header 与 ``jwt.decode(algorithms=["RS256"])`` 双重固定。
                # 显式声明其他算法的 key 不得进入信任集合，避免把 metadata 当作放宽依据。
                if "alg" in raw and raw.get("alg") != "RS256":
                    continue
                normalized_key = cast(Mapping[str, object], raw)
                try:
                    public_key = cast(
                        RSAPublicKey,
                        RSAAlgorithm.from_jwk(
                            json.dumps(normalized_key, separators=(",", ":"))
                        ),
                    )
                    # Microsoft 生产 OIDC RSA key 至少应达到 2048 bit；过短或非 RSA
                    # 参数即使 cryptography 勉强接受，也不应进入可缓存的信任集合。
                    if not isinstance(public_key, RSAPublicKey) or public_key.key_size < 2048:
                        raise ValueError("RSA key size is invalid")
                except (InvalidKeyError, PyJWTError, TypeError, ValueError):
                    raise PermanentProviderError(
                        error_code="microsoft_oidc_invalid_response",
                        message="Microsoft OIDC response is invalid",
                    ) from None
                keys.append(normalized_key)
        if not keys:
            raise PermanentProviderError(
                error_code="microsoft_oidc_invalid_response",
                message="Microsoft OIDC response is invalid",
            )
        frozen = _CachedDiscovery(time.monotonic(), document, tuple(keys))
        self._discovery_cache = frozen
        return frozen.document, frozen.jwks


def _parse_discovery(payload: Mapping[str, object]) -> _DiscoveryDocument:
    """校验 discovery issuer/JWKS URI，阻止响应驱动 SSRF。"""
    try:
        issuer = _require_microsoft_text(payload.get("issuer"), "issuer", max_length=512)
        jwks_uri = _require_microsoft_text(payload.get("jwks_uri"), "jwks_uri", max_length=512)
        parsed = httpx.URL(jwks_uri)
        # Discovery 是供应商响应驱动的网络边界；只接受规格固定的 common JWKS URL，
        # 从而同时拒绝信任主机上的任意路径、query、fragment、端口或 userinfo。保留
        # login.live.com 的同构固定端点以支持个人账户 issuer，但不接受动态 tenant 路径。
        if (
            parsed.scheme != "https"
            or parsed.host not in _ALLOWED_JWKS_HOSTS
            or jwks_uri not in {MICROSOFT_JWKS_URL, _MICROSOFT_LIVE_JWKS_URL}
        ):
            raise ValueError("JWKS URI is not an allowed fixed endpoint")
        if "{tenantid}" in issuer:
            if not re.fullmatch(
                r"https://(?:login\.microsoftonline\.com|login\.live\.com)/\{tenantid\}/v2\.0/?",
                issuer,
            ):
                raise ValueError("issuer template is invalid")
        elif _ISSUER_TEMPLATE_RE.match(issuer) is None:
            raise ValueError("issuer template is invalid")
        return _DiscoveryDocument(issuer=issuer, jwks_uri=jwks_uri)
    except (TypeError, ValueError):
        raise PermanentProviderError(
            error_code="microsoft_oidc_invalid_response",
            message="Microsoft OIDC response is invalid",
        ) from None


def _tenant_from_claims(claims: Mapping[str, object]) -> str:
    """从已验证 tid claim 收窄租户标识，拒绝跨 issuer 注入。"""
    tenant = _require_microsoft_text(
        claims.get("tid"),
        "tenant id",
        max_length=MAX_PROVIDER_TENANT_ID_LENGTH,
    )
    if not _TENANT_TEXT_RE.fullmatch(tenant):
        raise ValueError("tenant id is invalid")
    return tenant


def _issuer_for_tenant(template: str, tenant: str) -> str:
    """把 discovery issuer 模板解析为当前租户期望 issuer。"""
    if "{tenantid}" in template:
        expected = template.replace("{tenantid}", tenant)
    else:
        parsed = _ISSUER_TEMPLATE_RE.match(template)
        if parsed is None:
            raise ValueError("issuer template is invalid")
        expected = f"https://{parsed.group('host')}/{tenant}/v2.0"
    return expected.rstrip("/")


def _require_email(value: object) -> str:
    """校验 Graph mailbox 地址的最小稳定结构。"""
    email = _require_microsoft_text(value, "mailbox", max_length=MAX_ACCOUNT_EMAIL_LENGTH)
    if email.count("@") != 1:
        raise ValueError("Microsoft mailbox is invalid")
    local, domain = email.split("@")
    if not local or not domain:
        raise ValueError("Microsoft mailbox is invalid")
    return email


def classify_microsoft_callback_error(
    *,
    error: str | None,
    error_description: str | None,
    error_codes: str | None,
) -> DomainError | None:
    """把 Microsoft callback 的同意/重新授权失败映射为固定错误码。

    只有明确的 ``AADSTS65001``/管理员同意证据才返回 409 对应的错误码；普通
    ``interaction_required`` 返回 403 对应的重新授权错误。原始描述只用于本地分类，
    不进入异常消息、metadata、数据库或日志。其他未知 callback error 交给上层通用失败处理。
    """
    normalized_error = error.casefold() if isinstance(error, str) else ""
    normalized_codes = error_codes.casefold() if isinstance(error_codes, str) else ""
    normalized_description = (
        error_description.casefold() if isinstance(error_description, str) else ""
    )
    # ``interaction_required`` 既可能表示普通登录交互，也可能表示租户管理员同意缺失。
    # 只有固定代码或明确的 consent/admin-consent 词组才能证明后者，普通交互则要求用户
    # 重新授权；两条路径都只返回稳定错误码，原始 description 永不回显。
    explicit_consent_evidence = (
        "65001" in normalized_codes
        or "65001" in normalized_description
        or any(
            marker in normalized_codes or marker in normalized_description
            for marker in (
                "admin consent",
                "administrator consent",
                "consent required",
                "consent_required",
            )
        )
    )
    if (
        explicit_consent_evidence
        or "65001" in normalized_error
        or normalized_error
        in {
            "consent_required",
            "admin_consent_required",
        }
    ):
        return UserActionRequiredError(
            error_code="microsoft_admin_consent_required",
            message="Microsoft administrator consent is required",
        )
    if normalized_error == "interaction_required":
        return UserActionRequiredError(
            error_code="microsoft_reauthorization_required",
            message="Microsoft authorization requires user action",
        )
    return None
