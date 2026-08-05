"""验证供应商和模型失败保持明确、可恢复且不伪造完整简报。"""

import httpx
import pytest

from ai_employee.integrations.google.gmail import GmailAdapter


@pytest.mark.asyncio
async def test_gmail_429_honors_retry_after() -> None:
    """Gmail 429 的秒数提示必须变成内部临时错误的 retry_after。"""
    adapter = GmailAdapter(access_token="synthetic", refresh_access_token=_token, mark_expired=_none)
    response = httpx.Response(429, headers={"Retry-After": "17"}, request=httpx.Request("GET", "https://example.test"))
    assert adapter._retry_after(response) == 17


async def _token() -> str: return "synthetic"
async def _none() -> None: return None
