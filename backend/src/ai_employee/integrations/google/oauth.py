"""封装 Google OAuth 2.0 的最小只读授权 URL 与 HTTP 交换边界。"""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


@dataclass(frozen=True, slots=True)
class GoogleTokenResponse:
    """规范化 token 端点返回，避免供应商字典离开适配器层。"""

    access_token: str
    refresh_token: str | None
    expires_in: int


@dataclass(frozen=True, slots=True)
class GoogleAccount:
    """规范化 OpenID 用户资料，仅保留连接归属所需的标识与邮箱。"""

    provider_account_id: str
    email: str


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
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": self._redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
            )
            response.raise_for_status()
            payload = response.json()
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        refresh_token = payload.get("refresh_token")
        if not isinstance(access_token, str) or not isinstance(expires_in, int):
            raise TypeError("Google token response is invalid")
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise TypeError("Google refresh token response is invalid")
        return GoogleTokenResponse(access_token, refresh_token, expires_in)

    async def fetch_account(self, access_token: str) -> GoogleAccount:
        """读取 OpenID 用户资料并收窄为连接唯一键。

        Raises:
            httpx.HTTPStatusError: 供应商拒绝资料读取时抛出。
            ValueError: 响应没有可持久化的 subject 或 email 时抛出。
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            payload = response.json()
        subject = payload.get("sub")
        email = payload.get("email")
        if not isinstance(subject, str) or not isinstance(email, str):
            raise TypeError("Google userinfo response is invalid")
        return GoogleAccount(subject, email)

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
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
            response.raise_for_status()
            payload = response.json()
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        rotated_refresh = payload.get("refresh_token")
        if not isinstance(access_token, str) or not isinstance(expires_in, int):
            raise TypeError("Google refresh response is invalid")
        if rotated_refresh is not None and not isinstance(rotated_refresh, str):
            raise TypeError("Google refresh response is invalid")
        return GoogleTokenResponse(access_token, rotated_refresh, expires_in)

    async def revoke(self, token: str) -> None:
        """尽力撤销 token；调用方必须保证本地断开不依赖网络成功。"""
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = await client.post(
                "https://oauth2.googleapis.com/revoke", data={"token": token}
            )
            response.raise_for_status()
