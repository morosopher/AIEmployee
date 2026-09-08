"""封装 Google OAuth 2.0 的渐进授权与兼容的旧客户端 HTTP 边界。"""

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Final, cast
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
    OAuthPostExchangeVerificationError,
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
# Google token/token-info 响应在部分文档示例中把 ``email`` 回显为旧的 userinfo.email
# URI。该兼容别名只允许出现在响应归一化边界，授权 URL 仍严格使用冻结的 ``email``。
GOOGLE_RESPONSE_SCOPE_ALIASES: Final[Mapping[str, str]] = MappingProxyType(
    {"https://www.googleapis.com/auth/userinfo.email": "email"}
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
    raw_scopes = frozenset(value.split())
    if not raw_scopes or any(len(scope) > MAX_OAUTH_SCOPE_LENGTH for scope in raw_scopes):
        raise ValueError(f"Google {field_name} is invalid")
    scopes = frozenset(GOOGLE_RESPONSE_SCOPE_ALIASES.get(scope, scope) for scope in raw_scopes)
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


def _google_retry_after(response: httpx.Response) -> int | None:
    """只解析有限的整数 Retry-After，不把供应商响应正文带入领域错误。"""
    try:
        value = int(response.headers.get("Retry-After", ""))
        # DomainError 会把该值规范为有限浮点；先在适配器边界过滤超大整数，
        # 避免供应商畸形 Header 令错误分类本身抛出 OverflowError/ValueError。
        if value >= 0:
            float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def _google_http_error(
    *,
    status_code: int,
    operation: str,
    retry_after: int | None,
) -> DomainError:
    """把 Google HTTP 状态转换为固定、脱敏的领域错误。

    Args:
        status_code: 供应商 HTTP 状态码；只保留整数状态，不传递响应对象。
        operation: 内部固定操作名，不得包含 URL、请求参数或供应商正文。
        retry_after: 已收窄的 Retry-After 秒数。

    Returns:
        面向应用层的稳定错误分类；错误消息和元数据不含供应商原文。
    """
    if status_code in {401, 403} or (operation in {"token info", "id_token info"} and status_code == 400):
        return UserActionRequiredError(
            error_code="google_reauthorization_required",
            message="Google authorization requires user action",
        )
    if status_code == 429 or status_code >= 500:
        return TransientProviderError(
            error_code="google_oauth_unavailable",
            message="Google OAuth is temporarily unavailable",
            retry_after=retry_after,
        )
    return PermanentProviderError(
        error_code="google_oauth_rejected",
        message="Google OAuth request was rejected",
    )


async def _google_request(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    operation: str,
    **kwargs: Any,
) -> httpx.Response:
    """执行单次 OAuth HTTP 请求并在脱离异常上下文后返回安全领域错误。

    ``kwargs`` 只在这个第三方 HTTP 客户端边界使用 ``Any``，调用方仍传入固定的
    ``params``、``headers`` 或表单字段；不让该类型穿过适配器返回值。

    ``httpx`` 异常会保存完整 request（可能含 query token、Authorization header 或
    refresh token body）。先记录固定分类，再在 ``except`` 块外抛出新的领域错误，避免
    这些对象通过 traceback、``__context__`` 或 API generic 500 逸出。
    """
    request_error: DomainError | None = None
    response: httpx.Response | None = None
    try:
        if method == "GET":
            response = await client.get(url, **kwargs)
        elif method == "POST":
            response = await client.post(url, **kwargs)
        else:  # pragma: no cover - 调用方只使用固定 GET/POST，防御未来误用。
            request_error = PermanentProviderError(
                error_code="google_oauth_invalid_operation",
                message="Google OAuth operation is invalid",
            )
    except httpx.HTTPStatusError as error:
        # 某些 transport/测试 double 会在返回响应前直接抛出 HTTPStatusError；不能让其
        # request、query、正文或异常文本越过 adapter。只读取状态与已收窄的 Retry-After，
        # 并在离开 except 后再抛出新的领域错误以清除原始 ``__context__``。
        error_response = error.response
        if error_response is None:
            request_error = PermanentProviderError(
                error_code="google_oauth_invalid_response",
                message="Google OAuth response is invalid",
            )
        else:
            request_error = _google_http_error(
                status_code=error_response.status_code,
                operation=operation,
                retry_after=_google_retry_after(error_response),
            )
    except httpx.TimeoutException:
        request_error = TransientProviderError(
            error_code="google_oauth_timeout",
            message="Google OAuth request timed out",
        )
    except httpx.RequestError:
        request_error = TransientProviderError(
            error_code="google_oauth_request_failed",
            message="Google OAuth request failed",
        )

    if request_error is not None:
        raise request_error
    if response is None:  # pragma: no cover - 仅防御未来分支遗漏。
        raise PermanentProviderError(
            error_code="google_oauth_invalid_response",
            message="Google OAuth response is invalid",
        )
    if not response.is_success:
        raise _google_http_error(
            status_code=response.status_code,
            operation=operation,
            retry_after=_google_retry_after(response),
        )
    return response


def _read_google_json_object_safe(
    response: httpx.Response,
    operation: str,
) -> Mapping[str, object]:
    """把成功响应收窄为 JSON object，并丢弃 malformed 异常上下文。"""
    payload: Mapping[str, object] | None = None
    malformed = False
    try:
        payload = _read_google_json_object(response, operation)
    except (TypeError, ValueError):
        malformed = True
    if malformed or payload is None:
        raise PermanentProviderError(
            error_code="google_oauth_invalid_response",
            message="Google OAuth response is invalid",
        )
    return payload


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
        """一次交换授权码并核验实际 scope，缺省 scope 时只读查询 token-info。

        POST 结果未知保留原临时失败；已收到 token 响应后的只读临时失败使用
        OAuthPostExchangeVerificationError，供恢复用例安全收敛且不重放 code。
        """
        normalized_code = _require_google_text(code, "authorization code")
        normalized_verifier = _require_google_text(verifier, "PKCE verifier")
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            response = await _google_request(
                client,
                method="POST",
                url=GOOGLE_TOKEN_URL,
                operation="token exchange",
                data={
                    "code": normalized_code,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": self._redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": normalized_verifier,
                },
            )
            payload = _read_google_json_object_safe(response, "token")
            try:
                return await self._token_set_from_payload(payload, client=client)
            except TransientProviderError as error:
                # token POST 已明确成功；缺 scope 时的 token-info GET 只是只读验证。
                # 在 except 外创建端口标记，避免附带可能含请求上下文的原始异常链。
                verification_code, retry_after = error.error_code, error.retry_after
            raise OAuthPostExchangeVerificationError(
                error_code=verification_code, retry_after=retry_after
            )

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
            # 摘要来自一次性 OAuth attempt 的数据库事实；先验证固定字节长度，避免
            # ``hmac.compare_digest`` 对损坏的类型抛出裸 TypeError 或接受可变对象。
            if type(expected_nonce_hash) is not bytes or len(expected_nonce_hash) != sha256().digest_size:
                raise UserActionRequiredError(
                    error_code="google_oidc_nonce_mismatch",
                    message="Google OIDC verification requires user action",
                )
            if token.id_token is None:
                raise UserActionRequiredError(
                    error_code="google_oidc_nonce_missing",
                    message="Google OIDC verification requires user action",
                )
            async with httpx.AsyncClient(timeout=_google_timeout()) as client:
                info = await self._fetch_token_info(
                    client,
                    parameter_name="id_token",
                    parameter_value=token.id_token,
                    operation="id_token info",
                )
                nonce: str | None = None
                nonce_malformed = False
                try:
                    nonce = _require_google_text(info.get("nonce"), "OIDC nonce")
                except (TypeError, ValueError):
                    nonce_malformed = True
                if nonce_malformed or nonce is None:
                    raise UserActionRequiredError(
                        error_code="google_oidc_nonce_mismatch",
                        message="Google OIDC verification requires user action",
                    )
                observed_hash = sha256(nonce.encode("utf-8")).digest()
                if not hmac.compare_digest(observed_hash, expected_nonce_hash):
                    raise UserActionRequiredError(
                        error_code="google_oidc_nonce_mismatch",
                        message="Google OIDC verification requires user action",
                    )

        normalized_access = _require_google_text(token.access_token, "access_token")
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            response = await _google_request(
                client,
                method="GET",
                url=GOOGLE_USERINFO_URL,
                operation="userinfo",
                headers={"Authorization": f"Bearer {normalized_access}"},
            )
            payload = _read_google_json_object_safe(response, "userinfo")
        identity_malformed = False
        provider_account_id: str | None = None
        account_email: str | None = None
        try:
            provider_account_id = _require_google_text(
                payload.get("sub"),
                "userinfo subject",
                max_length=MAX_PROVIDER_ACCOUNT_ID_LENGTH,
            )
            account_email = _require_google_email(payload.get("email"), "userinfo email")
        except (TypeError, ValueError):
            identity_malformed = True
        if identity_malformed or provider_account_id is None or account_email is None:
            raise PermanentProviderError(
                error_code="google_oauth_invalid_response",
                message="Google OAuth response is invalid",
            )
        account = OAuthAccount(
            provider_account_id=provider_account_id,
            account_email=account_email,
            provider_tenant_id="",
            account_type="google",
        )
        return account

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """刷新 access token；Google 未轮换 refresh token 时返回 ``None``。"""
        normalized_refresh = _require_google_text(refresh_token, "refresh_token")
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            response = await _google_request(
                client,
                method="POST",
                url=GOOGLE_TOKEN_URL,
                operation="token refresh",
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": normalized_refresh,
                },
            )
            payload = _read_google_json_object_safe(response, "token")
            return await self._token_set_from_payload(payload, client=client)

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """调用 Google 撤销端点；只有明确 2xx 成功才返回 ``REVOKED``。"""
        normalized_token = _require_google_text(token, "revoke token")
        async with httpx.AsyncClient(timeout=_google_timeout()) as client:
            await _google_request(
                client,
                method="POST",
                url=GOOGLE_REVOKE_URL,
                operation="revoke",
                data={"token": normalized_token},
            )
        return OAuthRevocationResult(OAuthRevocationStatus.REVOKED)

    async def _token_set_from_payload(
        self,
        payload: Mapping[str, object],
        *,
        client: httpx.AsyncClient,
    ) -> OAuthTokenSet:
        """把 token JSON 与 token-info 实际 scope 收窄为端口值对象。

        供应商可以返回任意 JSON 类型；所有 ``TypeError``/``ValueError`` 只在适配器内
        作为 malformed 响应处理。故意在 ``except`` 块结束后再创建领域错误，避免 Python
        把原始异常（其中可能包含 token 的局部上下文）挂到 ``__context__`` 上。
        """
        token_set: OAuthTokenSet | None = None
        malformed = False
        try:
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
                granted_scopes = _normalize_google_scope_string(
                    info.get("scope"), "token-info scope"
                )
            id_token_value = payload.get("id_token")
            id_token = (
                _require_google_text(id_token_value, "id_token")
                if id_token_value is not None
                else None
            )
            token_set = OAuthTokenSet(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=expires_in,
                granted_scopes=granted_scopes,
                id_token=id_token,
            )
        except (TypeError, ValueError):
            malformed = True
        if malformed or token_set is None:
            raise PermanentProviderError(
                error_code="google_oauth_invalid_response",
                message="Google OAuth response is invalid",
            )
        return token_set

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
        response = await _google_request(
            client,
            method="GET",
            url=GOOGLE_TOKEN_INFO_URL,
            operation=operation,
            params={parameter_name: normalized},
        )
        return _read_google_json_object_safe(response, operation)


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
