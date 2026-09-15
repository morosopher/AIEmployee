"""以合成 HTTP 验证 Google 项目未启用 API 时的稳定配置错误与零授权副作用。"""

import httpx
import pytest
import respx

from ai_employee.domain.errors import PermanentProviderError
from ai_employee.integrations.google.calendar import GOOGLE_CALENDAR_LIST_URL, GoogleCalendarAdapter
from ai_employee.integrations.google.gmail import GMAIL_API_BASE_URL, GmailAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["mail", "calendar"])
@pytest.mark.parametrize("reason_shape", ["legacy", "error_info"])
@respx.mock
async def test_disabled_google_api_is_a_configuration_error_without_refresh_or_revocation(
    resource: str, reason_shape: str, caplog: pytest.LogCaptureFixture
) -> None:
    """服务未启用不能伪装成内部故障或 token 撤销，也不能自动刷新或重试读取。"""
    error: dict[str, object] = {"message": "synthetic-private-provider-message"}
    if reason_shape == "legacy":
        error["errors"] = [{"reason": "accessNotConfigured"}]
    else:
        error["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "domain": "googleapis.com",
                "reason": "SERVICE_DISABLED",
            }
        ]
    url = f"{GMAIL_API_BASE_URL}/messages" if resource == "mail" else GOOGLE_CALENDAR_LIST_URL
    route = respx.get(url).respond(403, json={"error": error})

    async def reject_refresh() -> str:
        """配置错误不授予刷新 token 的调用权。"""
        raise AssertionError("service disabled must not refresh credentials")

    async def reject_expiry() -> None:
        """供应商没有否定 OAuth 授权，不得把连接标为过期。"""
        raise AssertionError("service disabled must not expire the connection")

    with pytest.raises(PermanentProviderError) as raised:
        if resource == "mail":
            adapter = GmailAdapter(
                access_token="synthetic-access",
                refresh_access_token=reject_refresh,
                mark_expired=reject_expiry,
            )
            _ = [page async for page in adapter.initial_pages()]
        else:
            calendar = GoogleCalendarAdapter(
                access_token="synthetic-access",
                user_timezone="UTC",
                refresh_access_token=reject_refresh,
                mark_expired=reject_expiry,
            )
            _ = [page async for page in calendar.directory_pages()]

    assert raised.value.error_code == "google_api_not_enabled"
    assert route.call_count == 1
    assert "synthetic-private-provider-message" not in str(raised.value) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["mail", "calendar"])
@respx.mock
async def test_unrelated_google_forbidden_is_not_misclassified_as_api_disabled(resource: str) -> None:
    """只有结构化服务未启用证据可映射；一般 403 继续交给既有失败路径处理。"""
    url = f"{GMAIL_API_BASE_URL}/messages" if resource == "mail" else GOOGLE_CALENDAR_LIST_URL
    respx.get(url).respond(403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}})
    with pytest.raises(httpx.HTTPStatusError):
        if resource == "mail":
            _ = [page async for page in GmailAdapter(access_token="synthetic").initial_pages()]
        else:
            _ = [
                page
                async for page in GoogleCalendarAdapter(
                    access_token="synthetic", user_timezone="UTC"
                ).directory_pages()
            ]
