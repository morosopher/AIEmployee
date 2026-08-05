"""验证供应商和模型失败保持明确、可恢复且不伪造完整简报。"""

from datetime import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel
from ai_employee.infrastructure.db.repositories.email import SqlAlchemyGmailSyncRepositoryFactory
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.integrations.google.gmail import GmailAdapter


@pytest.mark.asyncio
async def test_gmail_429_honors_retry_after() -> None:
    """Gmail 429 的秒数提示必须变成内部临时错误的 retry_after。"""
    adapter = GmailAdapter(
        access_token="synthetic", refresh_access_token=_token, mark_expired=_none
    )
    response = httpx.Response(
        429, headers={"Retry-After": "17"}, request=httpx.Request("GET", "https://example.test")
    )
    assert adapter._retry_after(response) == 17


@pytest.mark.asyncio
async def test_google_revocation_is_persisted_as_degraded_connection(database_url: str) -> None:
    """Gmail 与 Calendar 共用凭据仓储时必须写出同一可见撤销状态。"""
    sessions = build_session_factory(database_url)
    user_id, connection_id = uuid4(), uuid4()
    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="revoked-owner@example.test",
                    display_name="Revoked Owner",
                    password_hash=None,
                    timezone="UTC",
                    brief_time=time(8),
                )
            )
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=user_id,
                    provider="google",
                    provider_account_id="synthetic-revoked",
                    account_email="revoked-owner@example.test",
                    scopes=[],
                    status="connected",
                    last_error_code=None,
                )
            )
        async with SqlAlchemyGmailSyncRepositoryFactory(sessions)() as store:
            await store.mark_expired(user_id=user_id, connection_id=connection_id)
        async with sessions() as session:
            connection = await session.scalar(
                select(OAuthConnectionModel).where(OAuthConnectionModel.id == connection_id)
            )
        assert connection is not None
        assert connection.status == "degraded"
        assert connection.last_error_code == "oauth_revoked"
    finally:
        await sessions.dispose()


async def _token() -> str:
    return "synthetic"


async def _none() -> None:
    return None
