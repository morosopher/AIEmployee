"""验证 Google OAuth 适配器拒绝空白 token、身份和 malformed JSON 边界。"""

import pytest
import respx

from ai_employee.integrations.google.oauth import (
    GOOGLE_TOKEN_URL,
    GOOGLE_USERINFO_URL,
    GoogleOAuthClient,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {"access_token": "", "expires_in": 3600},
        {"access_token": " ", "expires_in": 3600},
        {"access_token": "synthetic-access", "expires_in": 0},
        {"access_token": "synthetic-access", "expires_in": -1},
        {"access_token": "synthetic-access", "expires_in": 10**30},
    ),
)
async def test_google_token_exchange_rejects_blank_or_invalid_values(
    payload: dict[str, object],
) -> None:
    """Google token JSON 必须在 adapter 边界拒绝空 token 与非正有效期。"""
    client = GoogleOAuthClient(
        "synthetic-client",
        "synthetic-secret",
        "https://example.test/cb",
    )
    with respx.mock(assert_all_called=True) as mocked:
        mocked.post(GOOGLE_TOKEN_URL).respond(200, json=payload)
        with pytest.raises(ValueError):
            await client.exchange_code("synthetic-code", "synthetic-verifier")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {"sub": "", "email": "owner@example.test"},
        {"sub": " ", "email": "owner@example.test"},
        {"sub": "synthetic-subject", "email": ""},
        {"sub": "synthetic-subject", "email": " "},
    ),
)
async def test_google_userinfo_rejects_blank_identity_values(
    payload: dict[str, object],
) -> None:
    """Google userinfo 不得生成空 subject 或空邮箱连接身份。"""
    client = GoogleOAuthClient(
        "synthetic-client",
        "synthetic-secret",
        "https://example.test/cb",
    )
    with respx.mock(assert_all_called=True) as mocked:
        mocked.get(GOOGLE_USERINFO_URL).respond(200, json=payload)
        with pytest.raises(ValueError):
            await client.fetch_account("synthetic-access")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ([], None, "malformed-json"))
async def test_google_token_exchange_rejects_non_object_payload(payload: object) -> None:
    """token 顶层 JSON 必须是对象，不能让 ``list.get`` 变成裸 AttributeError。"""
    client = GoogleOAuthClient(
        "synthetic-client",
        "synthetic-secret",
        "https://example.test/cb",
    )
    with respx.mock(assert_all_called=True) as mocked:
        mocked.post(GOOGLE_TOKEN_URL).respond(200, json=payload)
        with pytest.raises((TypeError, ValueError), match="object"):
            await client.exchange_code("synthetic-code", "synthetic-verifier")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ([], None, "malformed-json"))
async def test_google_token_refresh_rejects_non_object_payload(payload: object) -> None:
    """refresh 共用 token 端点，也必须先收窄顶层 JSON 对象。"""
    client = GoogleOAuthClient(
        "synthetic-client",
        "synthetic-secret",
        "https://example.test/cb",
    )
    with respx.mock(assert_all_called=True) as mocked:
        mocked.post(GOOGLE_TOKEN_URL).respond(200, json=payload)
        with pytest.raises((TypeError, ValueError), match="object"):
            await client.refresh_token("synthetic-refresh")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ([], None, "malformed-json"))
async def test_google_userinfo_rejects_non_object_payload(payload: object) -> None:
    """userinfo 顶层 JSON 必须是对象，错误必须停在供应商边界。"""
    client = GoogleOAuthClient(
        "synthetic-client",
        "synthetic-secret",
        "https://example.test/cb",
    )
    with respx.mock(assert_all_called=True) as mocked:
        mocked.get(GOOGLE_USERINFO_URL).respond(200, json=payload)
        with pytest.raises((TypeError, ValueError), match="object"):
            await client.fetch_account("synthetic-access")


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ("", " ", "token\x00"))
async def test_google_revoke_rejects_malformed_input_without_network(token: str) -> None:
    """撤销输入必须先验证，空白/控制字符不能进入 Authorization 供应商边界。"""
    client = GoogleOAuthClient(
        "synthetic-client",
        "synthetic-secret",
        "https://example.test/cb",
    )
    with respx.mock(assert_all_called=False) as mocked:
        with pytest.raises((TypeError, ValueError), match="token"):
            await client.revoke(token)
        assert not mocked.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "first", "second"),
    (
        ("exchange", "", "synthetic-verifier"),
        ("exchange", "synthetic-code", "verifier\x00value"),
        ("refresh", "refresh\x00value", None),
        ("fetch", "a" * 10_000, None),
        ("revoke", "r" * 10_000, None),
    ),
)
async def test_google_client_rejects_malformed_inputs_before_network(
    operation: str,
    first: str,
    second: str | None,
) -> None:
    """授权码、PKCE 与 token 明文都要在创建 HTTP 请求前完成边界校验。"""
    client = GoogleOAuthClient(
        "synthetic-client",
        "synthetic-secret",
        "https://example.test/cb",
    )
    with respx.mock(assert_all_called=False) as mocked:
        with pytest.raises((TypeError, ValueError)):
            if operation == "exchange":
                assert second is not None
                await client.exchange_code(first, second)
            elif operation == "refresh":
                await client.refresh_token(first)
            elif operation == "fetch":
                await client.fetch_account(first)
            else:
                await client.revoke(first)
        assert not mocked.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {"access_token": "token\x00value", "expires_in": 3600},
        {"access_token": "a" * 10_000, "expires_in": 3600},
        {
            "access_token": "synthetic-access",
            "refresh_token": "refresh\x00value",
            "expires_in": 3600,
        },
    ),
)
async def test_google_token_exchange_rejects_control_or_oversized_tokens(
    payload: dict[str, object],
) -> None:
    """Google token JSON 不能把控制字符或异常长 opaque 值带入应用层。"""
    client = GoogleOAuthClient(
        "synthetic-client",
        "synthetic-secret",
        "https://example.test/cb",
    )
    with respx.mock(assert_all_called=True) as mocked:
        mocked.post(GOOGLE_TOKEN_URL).respond(200, json=payload)
        with pytest.raises((TypeError, ValueError), match="token"):
            await client.exchange_code("synthetic-code", "synthetic-verifier")


@pytest.mark.asyncio
async def test_google_userinfo_rejects_oversized_subject_and_invalid_email() -> None:
    """Google subject 与邮箱必须满足数据库长度和基本结构边界。"""
    client = GoogleOAuthClient(
        "synthetic-client",
        "synthetic-secret",
        "https://example.test/cb",
    )
    with respx.mock(assert_all_called=True) as mocked:
        mocked.get(GOOGLE_USERINFO_URL).respond(
            200,
            json={"sub": "s" * 256, "email": "not-an-email"},
        )
        with pytest.raises((TypeError, ValueError), match="userinfo"):
            await client.fetch_account("synthetic-access")
