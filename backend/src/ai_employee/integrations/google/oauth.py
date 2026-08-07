"""封装 Google OAuth 2.0 的最小只读授权 URL 与 HTTP 交换边界。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from unicodedata import category
from urllib.parse import urlencode

import httpx

from ai_employee.application.ports.oauth import (
    MAX_ACCOUNT_EMAIL_LENGTH,
    MAX_OAUTH_EXPIRES_IN,
    MAX_OAUTH_TOKEN_LENGTH,
    MAX_PROVIDER_ACCOUNT_ID_LENGTH,
)

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_SCOPES = [
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


@dataclass(frozen=True, slots=True)
class GoogleTokenResponse:
    """规范化 token 端点返回，避免供应商字典离开适配器层。"""

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
    *, client_id: str, redirect_uri: str, state: str, code_challenge: str
) -> str:
    """构建固定 scope、离线授权和 S256 PKCE 的 Google 授权地址。

    Args:
        client_id: Google OAuth 客户端标识。
        redirect_uri: 与 Google 控制台已登记的精确回调地址。
        state: 本地一次性随机 state 原文，只在浏览器往返中出现。
        code_challenge: PKCE verifier 的 SHA-256 Base64URL 编码。

    Returns:
        不含 client secret 或任何用户 Token 的授权 URL。
    """
    return f"{GOOGLE_AUTHORIZATION_URL}?{
        urlencode(
            {
                'client_id': client_id,
                'redirect_uri': redirect_uri,
                'response_type': 'code',
                'scope': ' '.join(GOOGLE_SCOPES),
                'state': state,
                'access_type': 'offline',
                'prompt': 'consent',
                'code_challenge': code_challenge,
                'code_challenge_method': 'S256',
            }
        )
    }"


class GoogleOAuthClient:
    """使用显式短超时调用 Google token、userinfo 与 revoke 端点。"""

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        """保存运行时配置；secret 从调用方 SecretStr 解包且从不记录。"""
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    async def exchange_code(self, code: str, verifier: str) -> GoogleTokenResponse:
        """以授权码和 PKCE verifier 交换短期/刷新令牌。

        Raises:
            httpx.HTTPStatusError: Google 返回非成功状态时抛出，调用方映射为脱敏错误。
            ValueError: 返回缺少所需字段或字段类型错误时抛出。
        """
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
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
        """读取 OpenID 用户资料并收窄为连接唯一键。

        Raises:
            httpx.HTTPStatusError: 供应商拒绝资料读取时抛出。
            ValueError: 响应没有可持久化的 subject 或 email 时抛出。
        """
        normalized_access = _require_google_text(access_token, "access_token")
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
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
        """以已有 refresh token 交换新的 access token，不假设 Google 必定轮换 refresh token。

        Args:
            refresh_token: 仅在受控内存存在的 Google refresh token 明文。

        Returns:
            适配器已收窄的 access、可选 refresh 与有效期。

        Raises:
            httpx.HTTPStatusError: token 端点返回非成功状态时保留供调用方分类。
            TypeError: 响应缺少 required access token 或 expires_in 时抛出。
        """
        normalized_refresh_input = _require_google_text(refresh_token, "refresh_token")
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
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
        normalized_token = _require_google_text(
            token,
            "revoke token",
            max_length=MAX_OAUTH_TOKEN_LENGTH,
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.post(
                "https://oauth2.googleapis.com/revoke", data={"token": normalized_token}
            )
            response.raise_for_status()
