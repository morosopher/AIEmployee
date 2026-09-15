"""把 Google 只读 API 的项目配置失败收窄为不含供应商响应的稳定领域错误。"""

import httpx

from ai_employee.domain.errors import PermanentProviderError


def raise_if_google_api_disabled(response: httpx.Response) -> None:
    """只根据已知的结构化 403 证据报告服务未启用，不解释供应商自由文本。

    Args:
        response: Gmail 或 Calendar 固定只读端点的 HTTP 响应。

    Raises:
        PermanentProviderError: 项目尚未启用 API；用户修正配置后可显式重试。此错误
            不属于 Token 过期，不能触发自动刷新或连接撤销。
    """
    if response.status_code != 403:
        return
    try:
        payload: object = response.json()
    except ValueError:
        return
    if not isinstance(payload, dict) or not isinstance(error := payload.get("error"), dict):
        return
    legacy = error.get("errors")
    details = error.get("details")
    legacy_disabled = isinstance(legacy, list) and any(
        isinstance(item, dict) and item.get("reason") == "accessNotConfigured" for item in legacy
    )
    service_disabled = isinstance(details, list) and any(
        isinstance(item, dict)
        and item.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo"
        and item.get("domain") == "googleapis.com"
        and item.get("reason") == "SERVICE_DISABLED"
        for item in details
    )
    if legacy_disabled or service_disabled:
        # 响应可能含项目编号、账户与原始请求；异常只包含固定可操作错误。
        raise PermanentProviderError(
            error_code="google_api_not_enabled",
            message="Enable the Google API in the OAuth application's project before retrying",
        )
