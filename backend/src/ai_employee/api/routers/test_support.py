"""仅供受控测试环境注入合成故障场景的 API 路由。"""

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from ai_employee.api.deps import CsrfProtectedSession
from ai_employee.config import Settings
from ai_employee.infrastructure.db.models.sources import (
    EmailMessageModel,
    EmailThreadModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.security.encryption import AeadCipher

TEST_SCENARIO_TTL_SECONDS = 600


class RedisScenarioClient(Protocol):
    """收窄到测试场景所需的 Redis 原子命令，避免泄露通用客户端。"""

    async def set(self, key: str, value: str, *, ex: int) -> object:
        """以明确 TTL 写入用户隔离场景。"""

    async def getdel(self, key: str) -> bytes | str | None:
        """原子读取并删除一次性场景。"""


class TestScenarioRequest(BaseModel):
    """限制 test-only 注入值，禁止把任意供应商载荷塞进 Redis。"""

    scenario: str = Field(pattern=r"^(oauth_revoked|gmail_429|calendar_5xx|model_invalid_twice|partial_source)$")


class TestScenarioStore:
    """以用户 ID 隔离、十分钟自动过期的一次性 fake adapter 场景存储。"""

    def __init__(self, redis: RedisScenarioClient) -> None:
        """注入仅能执行 SET/GETDEL 的 Redis 协议。"""
        self._redis = redis

    @staticmethod
    def key(*, user_id: UUID | str) -> str:
        """构造不含邮件、凭据或任务内容的每用户短期键。"""
        return f"ai_employee:test-scenario:{user_id}"

    async def set(self, *, user_id: UUID | str, scenario: str) -> None:
        """设置一次性合成场景并强制六百秒 TTL。"""
        await self._redis.set(self.key(user_id=user_id), scenario, ex=TEST_SCENARIO_TTL_SECONDS)

    async def consume(self, *, user_id: UUID | str) -> str | None:
        """原子消费场景，重复假请求不能重复施加同一故障。"""
        value = await self._redis.getdel(self.key(user_id=user_id))
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value if isinstance(value, str) else None

    async def aclose(self) -> None:
        """在测试 ASGI 生命周期结束时释放 Redis 客户端连接池。"""
        close = getattr(self._redis, "aclose", None)
        if close is not None:
            await close()


def should_register_test_support(settings: Settings) -> bool:
    """仅在 APP_ENV=test 且 APP_TEST_MODE=true 时允许路由进入 ASGI 表。"""
    return settings.app_env == "test" and settings.app_test_mode


def build_test_support_router() -> APIRouter:
    """构建 CSRF 保护的场景注入端点。

    Redis 连接由组合根在符合双开关时才创建；路由本身不读取 Secret、不连接真实
    Google，也不返回当前或历史测试场景。
    """
    router = APIRouter(prefix="/api/v1/test-support", tags=["test-support"], include_in_schema=False)

    @router.post("/scenario", status_code=status.HTTP_204_NO_CONTENT)
    async def set_scenario(
        payload: TestScenarioRequest,
        authenticated: CsrfProtectedSession,
        request: Request,
    ) -> None:
        """为当前登录用户设置一次性 fake adapter 故障，拒绝跨用户写入。"""
        store = request.app.state.test_scenario_store
        await store.set(user_id=authenticated.user.id, scenario=payload.scenario)

    @router.post("/seed-google-source", status_code=status.HTTP_204_NO_CONTENT)
    async def seed_google_source(authenticated: CsrfProtectedSession, request: Request) -> None:
        """仅为 E2E 创建当前用户可消费的合成 Google 来源，不接受任意外部数据。"""
        connection_id = uuid4()
        user_id = authenticated.user.id
        cipher = AeadCipher.from_file(request.app.state.auth_settings.app_master_key_file)
        access = cipher.encrypt(
            b"fake-access", f"{user_id}:{connection_id}:access_token".encode("ascii")
        )
        refresh = cipher.encrypt(
            b"fake-refresh", f"{user_id}:{connection_id}:refresh_token".encode("ascii")
        )
        async with request.app.state.auth_session_factory.begin() as session:
            session.add(
                OAuthConnectionModel(
                    id=connection_id,
                    user_id=authenticated.user.id,
                    provider="google",
                    provider_account_id=f"e2e-{connection_id}",
                    account_email="e2e-source@example.test",
                    scopes=[],
                    status="connected",
                    last_error_code=None,
                )
            )
            session.add_all(
                (
                    EncryptedCredentialModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        credential_kind="access_token",
                        ciphertext=access.ciphertext,
                        nonce=access.nonce,
                        key_version=access.key_version,
                    ),
                    EncryptedCredentialModel(
                        user_id=user_id,
                        connection_id=connection_id,
                        credential_kind="refresh_token",
                        ciphertext=refresh.ciphertext,
                        nonce=refresh.nonce,
                        key_version=refresh.key_version,
                    ),
                )
            )
            session.add_all(
                (
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="gmail",
                        cursor="synthetic-cursor",
                        last_success_at=datetime.now(UTC) - timedelta(hours=1),
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        cursor="synthetic-cursor",
                        last_success_at=datetime.now(UTC),
                    ),
                )
            )
            thread = EmailThreadModel(
                user_id=authenticated.user.id,
                connection_id=connection_id,
                provider_thread_id=f"e2e-thread-{connection_id}",
                subject="Synthetic follow-up",
                participants=[],
                latest_message_at=datetime.now(UTC),
                provider_url="https://example.test/e2e-thread",
            )
            session.add(thread)
            await session.flush()
            session.add(
                EmailMessageModel(
                    user_id=authenticated.user.id,
                    thread_id=thread.id,
                    provider_message_id=f"e2e-message-{connection_id}",
                    received_at=datetime.now(UTC),
                    sender={"email": "sender@example.test"},
                    recipients=[],
                    subject="Synthetic follow-up",
                    snippet="Synthetic approved source fact",
                    body_ciphertext=b"test",
                    body_nonce=b"0" * 12,
                    body_key_version=1,
                    labels=[],
                    headers={},
                    provider_url="https://example.test/e2e-message",
                )
            )

    return router
