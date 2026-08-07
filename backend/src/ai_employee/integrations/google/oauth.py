"""封装 Google OAuth 2.0 的渐进授权与兼容的旧客户端 HTTP 边界。"""

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Final, cast
from unicodedata import category
from urllib.parse import urlencode

import httpx

from ai_employee.application.ports.oauth import (
    MAX_ACCOUNT_EMAIL_LENGTH,
    MAX_OAUTH_EXPIRES_IN,
    MAX_OAUTH_SCOPE_LENGTH,
    MAX_OAUTH_TOKEN_LENGTH,
    MAX_PROVIDER_ACCOUNT_ID_LENGTH,
    OAuthAccount,
    OAuthAuthorizationRequest,
    OAuthProvider,
    OAuthProviderAdapter,
    OAuthRevocationResult,
    OAuthRevocationStatus,
    OAuthTokenSet,
)
from ai_employee.domain.connections import ConnectionCapability

GOOGLE_AUTHORIZATION_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"
GOOGLE_TOKEN_INFO_URL: Final = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_USERINFO_URL: Final = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_REVOKE_URL: Final = "https://oauth2.googleapis.com/revoke"

# 这些 scope 是 M2 唯一允许 Google 连接请求或持久化核验的权限。使用不可变集合，避免
# 组合根或测试在进程运行期间意外追加 Contacts、Gmail Draft 等越界数据源。
GOOGLE_BASE_SCOPES: Final[frozenset[str]] = frozenset({"openid", "email"})
GOOGLE_CAPABILITY_SCOPES: Final[Mapping[ConnectionCapability, frozenset[str]]] = MappingProxyType(
    {
        ConnectionCapability.MAIL_READ: frozenset(
            {"https://www.googleapis.com/auth/gmail.readonly"}
        ),
        ConnectionCapability.MAIL_SEND: frozenset({"https://www.googleapis.com/auth/gmail.send"}),
        ConnectionCapability.CALENDAR_READ: frozenset(
            {"https://www.googleapis.com/auth/calendar.readonly"}
        ),
        ConnectionCapability.CALENDAR_WRITE: frozenset(
            {"https://www.googleapis.com/auth/calendar.events"}
        ),
    }
)
GOOGLE_ALLOWED_SCOPES: Final[frozenset[str]] = frozenset(
    set(GOOGLE_BASE_SCOPES).union(*GOOGLE_CAPABILITY_SCOPES.values())
)

# M1 旧导入和同步代码仍读取该名称；保留稳定顺序而不是把 frozenset 直接暴露给旧调用方。
GOOGLE_SCOPES: Final[list[str]] = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def _require_google_text(
    value: object,
    field_name: str,
    *,
    max_length: int = MAX_OAUTH_TOKEN_LENGTH,
) -> str:
    """收窄 Google 文本，拒绝空白、控制字符和异常长 opaque 内容。"""
    if not isinstance(value, str):
        raise TypeError(f"Google {field_name} is invalid")
    if value == "" or value.strip() == "" or value != value.strip():
        raise ValueError(f"Google {field_name} is invalid")
    if any(char.isspace() or category(char).startswith("C") for char in value):
        raise ValueError(f"Google {field_name} is invalid")
    if len(value) > max_length:
        raise ValueError(f"Google {field_name} is invalid")
    return value


def _require_positive_expiry(value: object) -> int:
    """收窄 Google expires_in，避免异常整数令后续日期运算溢出。"""
    if type(value) is not int:
        raise TypeError("Google expires_in is invalid")
    if not 1 <= value <= MAX_OAUTH_EXPIRES_IN:
        raise ValueError("Google expires_in is invalid")
    return value


def _require_google_json_object(payload: object, operation: str) -> Mapping[str, object]:
    """确保供应商 JSON 顶层是 object，避免列表/字符串触发裸 ``AttributeError``。"""
    if not isinstance(payload, Mapping):
        raise TypeError(f"Google {operation} JSON object is invalid")
    return cast(Mapping[str, object], payload)


def _read_google_json_object(response: httpx.Response, operation: str) -> Mapping[str, object]:
    """读取并收窄 JSON；空响应或解码失败也只返回稳定的无正文边界错误。"""
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise TypeError(f"Google {operation} JSON object is invalid") from error
    return _require_google_json_object(payload, operation)


def _require_google_email(value: object, field_name: str) -> str:
    """校验 Google 账户邮箱的长度与最小稳定结构，不引入过度严格的 RFC 解析器。"""
    email = _require_google_text(
        value,
        field_name,
        max_length=MAX_ACCOUNT_EMAIL_LENGTH,
    )
    if email.count("@") != 1:
        raise ValueError(f"Google {field_name} is invalid")
    local, domain = email.split("@")
    if not local or not domain:
        raise ValueError(f"Google {field_name} is invalid")
    return email


def _normalize_google_scope_string(value: object, field_name: str = "scope") -> frozenset[str]:
    """规范化供应商 scope 字符串并拒绝越界或 malformed 权限。

    Google OAuth 返回的是以空格分隔的 scope 字符串。允许普通 ASCII 空格作为分隔符，
    但拒绝换行、制表符、控制字符、首尾空格和异常长单项，防止响应被注入日志或扩大
    后续权限判断；未知 scope 也 fail closed，不把供应商未来新增数据源默认为 M2 能力。
    """
    if not isinstance(value, str):
        raise TypeError(f"Google {field_name} is invalid")
    if value == "" or value.strip() == "" or value != value.strip():
        raise ValueError(f"Google {field_name} is invalid")
    if len(value) > MAX_OAUTH_TOKEN_LENGTH:
        raise ValueError(f"Google {field_name} is invalid")
    if any(category(char).startswith("C") or (char.isspace() and char != " ") for char in value):
        raise ValueError(f"Google {field_name} is invalid")
    scopes = frozenset(value.split())
    if not scopes or any(len(scope) > MAX_OAUTH_SCOPE_LENGTH for scope in scopes):
        raise ValueError(f"Google {field_name} is invalid")
    if not scopes.issubset(GOOGLE_ALLOWED_SCOPES):
        raise ValueError(f"Google {field_name} is not allowed")
    return scopes


def _validate_requested_scopes(scopes: object) -> frozenset[str]:
    """验证授权 URL 中的 requested scope 集合，不隐式添加任何数据源权限。"""
    if not isinstance(scopes, frozenset) or not scopes:
        raise ValueError("Google requested scopes are invalid")
    normalized = frozenset(
        _require_google_text(scope, "requested scope", max_length=MAX_OAUTH_SCOPE_LENGTH)
        for scope in scopes
    )
    if not normalized.issubset(GOOGLE_ALLOWED_SCOPES):
        raise ValueError("Google requested scopes are not allowed")
    return normalized


def _ordered_google_scopes(scopes: frozenset[str]) -> tuple[str, ...]:
    """按固定身份、邮件、日历顺序序列化 scope，避免 URL 与审计 diff 随机变化。"""
    order = {
        "openid": 0,
        "email": 1,
        "https://www.googleapis.com/auth/gmail.readonly": 2,
        "https://www.googleapis.com/auth/gmail.send": 3,
        "https://www.googleapis.com/auth/calendar.readonly": 4,
        "https://www.googleapis.com/auth/calendar.events": 5,
    }
    return tuple(sorted(scopes, key=lambda scope: (order.get(scope, 99), scope)))


def _google_timeout() -> httpx.Timeout:
    """为所有 Google OAuth 请求提供显式连接与总超时。"""
    return httpx.Timeout(10.0, connect=3.0)


@dataclass(frozen=True, slots=True)
class GoogleTokenResponse:
    """规范化旧客户端 token 端点返回，避免供应商字典离开适配器层。"""

    access_token: str
    refresh_token: str | None
    expires_in: int

    def __post_init__(self) -> None:
        """在旧客户端值对象边界再次拒绝空 token 与无效有效期。"""
        _require_google_text(
            self.access_token,
            "access_token",
            max_length=MAX_OAUTH_TOKEN_LENGTH,
        )
        if self.refresh_token is not None:
            _require_google_text(
                self.refresh_token,
                "refresh_token",
                max_length=MAX_OAUTH_TOKEN_LENGTH,
            )
        _require_positive_expiry(self.expires_in)


@dataclass(frozen=True, slots=True)
class GoogleAccount:
    """规范化 OpenID 用户资料，仅保留连接归属所需的标识与邮箱。"""

    provider_account_id: str
    email: str

    def __post_init__(self) -> None:
        """阻止空 subject 或邮箱形成可持久化 Google 连接身份。"""
        _require_google_text(
            self.provider_account_id,
            "userinfo subject",
            max_length=MAX_PROVIDER_ACCOUNT_ID_LENGTH,
        )
        _require_google_email(self.email, "userinfo email")


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scopes: frozenset[str] | None = None,
    oidc_nonce: str | None = None,
) -> str:
    """构建 Google 授权地址，并兼容 M1 的固定只读调用签名。

    Args:
        client_id: Google OAuth 客户端标识。
        redirect_uri: 与 Google 控制台登记的精确回调地址。
        state: 本地一次性随机 state 原文，只在浏览器往返中出现。
        code_challenge: PKCE verifier 的 SHA-256 Base64URL 编码。
        scopes: 本次请求的精确 scope 集合；缺省时使用 M1 只读集合。
        oidc_nonce: 可选的 OIDC nonce 原文；只进入 URL，不落库或日志。

    Returns:
        不含 client secret 或任何用户 Token 的授权 URL。
    """
    requested = (
        _validate_requested_scopes(scopes) if scopes is not None else frozenset(GOOGLE_SCOPES)
    )
    parameters: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_ordered_google_scopes(requested)),
        "state": state,
        "include_granted_scopes": "true",
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if oidc_nonce is not None:
        parameters["nonce"] = oidc_nonce
    return f"{GOOGLE_AUTHORIZATION_URL}?{urlencode(parameters)}"


class GoogleOAuthAdapter(OAuthProviderAdapter):
    """实现 M2 Google 渐进 scope、token-info 核验和 OIDC nonce 绑定。

    该类只输出 ``application.ports.oauth`` 的供应商中立值对象。Google SDK/HTTP 响应、
    access token 和 id_token 均在此边界内短暂存在；任何 scope 缺失或身份绑定无法通过
    Google HTTPS token-info 端点核验时都 fail closed。
    """

    provider = OAuthProvider.GOOGLE

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        """保存非敏感客户端配置；secret 仅随请求写入 HTTPS body，不记录。"""
        self._client_id = _require_google_text(client_id, "client_id")
        self._client_secret = _require_google_text(client_secret, "client_secret")
        self._redirect_uri = _require_google_text(redirect_uri, "redirect_uri")

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """返回精确能力对应的最小 Google scope 并集，不替调用方补依赖闭包。

        依赖闭包由 ``domain.connections`` 与连接用例计算；adapter 只解释自身固定映射，
        这样单独请求 ``mail.send`` 时也不会悄悄扩大到 Gmail 读取或 Calendar 数据源。
        """
        if not isinstance(capabilities, frozenset):
            raise TypeError("Google capabilities must be a frozenset")
        scopes = set(GOOGLE_BASE_SCOPES)
        for capability in capabilities:
            if type(capability) is not ConnectionCapability:
                raise ValueError("Google capability is invalid")
            try:
                scopes.update(GOOGLE_CAPABILITY_SCOPES[capability])
            except KeyError as error:
                raise ValueError("Google capability is invalid") from error
        return frozenset(scopes)

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """使用请求传入的 scope 集合构建离线、S256 PKCE 和 OIDC nonce URL。"""
        requested = _validate_requested_scopes(request.requested_scopes)
        return build_authorization_url(
            client_id=self._client_id,
            redirect_uri=self._redirect_uri,
            state=request.state,
            code_challenge=request.code_challenge,
            scopes=requested,
            oidc_nonce=request.oidc_nonce,
        )

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """交换授权码并核验实际授予 scope；缺省 scope 走 token-info 边界。"""
        normalized_code = _require_google_text(code, "authorization code")
        normalized_verifier = _require_google_text(verifier, "PKCE verifier")
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": normalized_code,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": self._redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": normalized_verifier,
                },
            )
            response.raise_for_status()
            payload = _read_google_json_object(response, "token")
            return await self._token_set_from_payload(payload, client=client)

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """在 userinfo 前验证 OIDC nonce，再返回供应商中立账户身份。

        ``id_token`` 不在本地做无签名 JWT base64 解码。Google token-info HTTPS 边界负责
        验证令牌签名并返回 nonce claim；缺少 id_token、claim 或摘要不匹配时直接拒绝。
        """
        if expected_nonce_hash is not None:
            if token.id_token is None:
                raise ValueError("Google OIDC nonce cannot be verified")
            async with httpx.AsyncClient(timeout=_google_timeout()) as client:
                info = await self._fetch_token_info(
                    client,
                    parameter_name="id_token",
                    parameter_value=token.id_token,
                    operation="id_token info",
                )
                nonce = _require_google_text(info.get("nonce"), "OIDC nonce")
                observed_hash = sha256(nonce.encode("utf-8")).digest()
                if not hmac.compare_digest(observed_hash, expected_nonce_hash):
                    raise ValueError("Google OIDC nonce cannot be verified")

        normalized_access = _require_google_text(token.access_token, "access_token")
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {normalized_access}"},
            )
            response.raise_for_status()
            payload = _read_google_json_object(response, "userinfo")
        account = OAuthAccount(
            provider_account_id=_require_google_text(
                payload.get("sub"),
                "userinfo subject",
                max_length=MAX_PROVIDER_ACCOUNT_ID_LENGTH,
            ),
            account_email=_require_google_email(payload.get("email"), "userinfo email"),
            provider_tenant_id="",
            account_type="google",
        )
        return account

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """刷新 access token；Google 未轮换 refresh token 时返回 ``None``。"""
        normalized_refresh = _require_google_text(refresh_token, "refresh_token")
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": normalized_refresh,
                },
            )
            response.raise_for_status()
            payload = _read_google_json_object(response, "token")
            return await self._token_set_from_payload(payload, client=client)

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """调用 Google 撤销端点；只有明确 2xx 成功才返回 ``REVOKED``。"""
        normalized_token = _require_google_text(token, "revoke token")
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            response = await client.post(GOOGLE_REVOKE_URL, data={"token": normalized_token})
            response.raise_for_status()
        return OAuthRevocationResult(OAuthRevocationStatus.REVOKED)

    async def _token_set_from_payload(
        self,
        payload: Mapping[str, object],
        *,
        client: httpx.AsyncClient,
    ) -> OAuthTokenSet:
        """把 token JSON 与 token-info 实际 scope 收窄为端口值对象。"""
        access_token = _require_google_text(payload.get("access_token"), "access_token")
        expires_in = _require_positive_expiry(payload.get("expires_in"))
        refresh_token_value = payload.get("refresh_token")
        refresh_token = (
            _require_google_text(refresh_token_value, "refresh_token")
            if refresh_token_value is not None
            else None
        )
        if "scope" in payload:
            granted_scopes = _normalize_google_scope_string(payload.get("scope"))
        else:
            info = await self._fetch_token_info(
                client,
                parameter_name="access_token",
                parameter_value=access_token,
                operation="token info",
            )
            granted_scopes = _normalize_google_scope_string(info.get("scope"), "token-info scope")
        id_token_value = payload.get("id_token")
        id_token = (
            _require_google_text(id_token_value, "id_token") if id_token_value is not None else None
        )
        return OAuthTokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            granted_scopes=granted_scopes,
            id_token=id_token,
        )

    async def _fetch_token_info(
        self,
        client: httpx.AsyncClient,
        *,
        parameter_name: str,
        parameter_value: str,
        operation: str,
    ) -> Mapping[str, object]:
        """通过 Google token-info HTTPS 边界核验 token，并脱敏所有失败对象。

        Google 只接受查询参数形式的 access/id token，而 ``httpx`` 原始异常会把完整 URL
        保存到消息和 ``request``。这里仅跨边界保留具体传输异常类型或 HTTP 状态码，并
        重新绑定到无查询串的 request；供应商正文、token 和原异常上下文都不会上浮。
        """
        normalized = _require_google_text(parameter_value, parameter_name)
        request_error_type: type[httpx.RequestError] | None = None
        status_code: int | None = None
        try:
            response = await client.get(
                GOOGLE_TOKEN_INFO_URL,
                params={parameter_name: normalized},
            )
        except httpx.RequestError as error:
            # 只复制异常类别，离开 ``except`` 后再抛出，避免 ``__context__`` 保留原始 URL。
            request_error_type = type(error)
        else:
            if response.is_success:
                return _read_google_json_object(response, operation)
            status_code = response.status_code

        safe_request = httpx.Request("GET", GOOGLE_TOKEN_INFO_URL)
        if request_error_type is not None:
            raise request_error_type(
                f"Google {operation} request failed",
                request=safe_request,
            )
        if status_code is None:  # pragma: no cover - 上述分支已穷尽，防御未来 httpx 行为变化。
            raise RuntimeError(f"Google {operation} request outcome is invalid")
        safe_response = httpx.Response(status_code, request=safe_request)
        raise httpx.HTTPStatusError(
            f"Google {operation} request failed",
            request=safe_request,
            response=safe_response,
        )


class GoogleOAuthClient:
    """使用显式短超时调用 Google token、userinfo 与 revoke 端点的 M1 兼容客户端。"""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        """保存运行时配置；secret 从调用方 SecretStr 解包且从不记录。"""
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    async def exchange_code(self, code: str, verifier: str) -> GoogleTokenResponse:
        """以授权码和 PKCE verifier 交换短期/刷新令牌。"""
        normalized_code = _require_google_text(
            code,
            "authorization code",
            max_length=MAX_OAUTH_TOKEN_LENGTH,
        )
        normalized_verifier = _require_google_text(
            verifier,
            "PKCE verifier",
            max_length=MAX_OAUTH_TOKEN_LENGTH,
        )
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": normalized_code,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": self._redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": normalized_verifier,
                },
            )
            response.raise_for_status()
            payload = _read_google_json_object(response, "token")
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        refresh_token = payload.get("refresh_token")
        normalized_access = _require_google_text(access_token, "access_token")
        normalized_expiry = _require_positive_expiry(expires_in)
        normalized_refresh = (
            _require_google_text(refresh_token, "refresh_token")
            if refresh_token is not None
            else None
        )
        return GoogleTokenResponse(normalized_access, normalized_refresh, normalized_expiry)

    async def fetch_account(self, access_token: str) -> GoogleAccount:
        """读取 OpenID 用户资料并收窄为旧客户端连接身份。"""
        normalized_access = _require_google_text(access_token, "access_token")
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {normalized_access}"},
            )
            response.raise_for_status()
            payload = _read_google_json_object(response, "userinfo")
        subject = payload.get("sub")
        email = payload.get("email")
        return GoogleAccount(
            _require_google_text(
                subject,
                "userinfo subject",
                max_length=MAX_PROVIDER_ACCOUNT_ID_LENGTH,
            ),
            _require_google_email(email, "userinfo email"),
        )

    async def refresh_token(self, refresh_token: str) -> GoogleTokenResponse:
        """以已有 refresh token 交换新的 access token，不假设 Google 必定轮换 refresh token。"""
        normalized_refresh_input = _require_google_text(refresh_token, "refresh_token")
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": normalized_refresh_input,
                },
            )
            response.raise_for_status()
            payload = _read_google_json_object(response, "token")
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        rotated_refresh = payload.get("refresh_token")
        normalized_access = _require_google_text(access_token, "access_token")
        normalized_expiry = _require_positive_expiry(expires_in)
        normalized_rotated_refresh = (
            _require_google_text(rotated_refresh, "refresh_token")
            if rotated_refresh is not None
            else None
        )
        return GoogleTokenResponse(
            normalized_access,
            normalized_rotated_refresh,
            normalized_expiry,
        )

    async def revoke(self, token: str) -> None:
        """尽力撤销 token；调用方必须保证本地断开不依赖网络成功。"""
        normalized_token = _require_google_text(token, "revoke token")
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            response = await client.post(GOOGLE_REVOKE_URL, data={"token": normalized_token})
            response.raise_for_status()
