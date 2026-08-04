"""为测试模式提供绝不联网的 Google 合成端口。"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from ai_employee.application.ports.calendar import CalendarSyncPage
from ai_employee.application.ports.gmail import GmailSyncPage
from ai_employee.integrations.google.calendar import CalendarAdapter
from ai_employee.integrations.google.oauth import GoogleAccount, GoogleTokenResponse


class FakeGoogleOAuthClient:
    """返回固定合成帐户和 token，供 Playwright 稳定验证连接/重连状态。"""

    async def exchange_code(self, code: str, verifier: str) -> GoogleTokenResponse:
        """忽略浏览器往返内容，绝不向 Google 请求授权码。"""
        del code, verifier
        return GoogleTokenResponse("fake-access", "fake-refresh", 3600)

    async def fetch_account(self, access_token: str) -> GoogleAccount:
        """返回可预测且脱敏的测试帐户。"""
        del access_token
        return GoogleAccount("fake-google-subject", "test-mode@example.test")

    async def refresh_token(self, refresh_token: str) -> GoogleTokenResponse:
        """模拟连接恢复，不执行网络调用。"""
        del refresh_token
        return GoogleTokenResponse("fake-reconnected-access", "fake-refresh", 3600)

    async def revoke(self, token: str) -> None:
        """本地假撤销不产生外部副作用。"""
        del token


class FakeCalendarReader:
    """从仓库合成 fixture 装载固定 Calendar 页，永不创建 HTTP 客户端。"""

    def __init__(self, fixture: Path) -> None:
        self._fixture = fixture

    async def initial_pages(self) -> AsyncIterator[CalendarSyncPage]:
        """读取本地 JSON 并复用真实规范化逻辑，保证 fixture 与端口一致。"""
        payload = json.loads(self._fixture.read_text(encoding="utf-8"))
        adapter = CalendarAdapter(
            access_token="fake", user_timezone="UTC", now=lambda: datetime(2030, 1, 9, tzinfo=UTC)
        )
        yield CalendarSyncPage(
            tuple(adapter._normalize(item) for item in payload.get("items", [])),
            None,
            payload.get("nextSyncToken"),
        )

    async def sync_pages(self, cursor: str) -> AsyncIterator[CalendarSyncPage]:
        """增量模式使用同一脱敏 fixture，调用方只能观察稳定游标行为。"""
        del cursor
        async for page in self.initial_pages():
            yield page

    async def execute_request(self, parameters: dict[str, str]) -> object:
        """禁止通过 fake 绕过分页 API 访问网络。"""
        del parameters
        raise RuntimeError("test-mode Calendar reader never performs HTTP")


class FakeGmailReader:
    """占位 Gmail fake，明确拒绝未提供的 fixture 读取而非回退真实网络。"""

    async def initial_pages(self) -> AsyncIterator[GmailSyncPage]:
        """当前 fixture 由 Gmail 任务单独驱动；该 fake 不触发网络。"""
        if False:
            yield GmailSyncPage((), None, "")

    async def history_pages(self, cursor: str) -> AsyncIterator[GmailSyncPage]:
        """保持端口完整性并拒绝使用真实 Gmail。"""
        del cursor
        if False:
            yield GmailSyncPage((), None, "")

    async def execute_request(self, path: str, parameters: dict[str, str]) -> object:
        """明示测试模式不可联网。"""
        del path, parameters
        raise RuntimeError("test-mode Gmail reader never performs HTTP")
