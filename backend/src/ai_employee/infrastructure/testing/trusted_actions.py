"""双测试开关专用的离线写组合；合成策略只与同一个封闭 Fake registry 配对。

本模块不创建 HTTP 客户端、不解密 OAuth token。正式提交、审批、认领、request-start、
Checkpoint 与核对仍使用生产用例，变化仅限于供应商端口和合成账户命名空间。
"""

import re
from uuid import UUID

from sqlalchemy import select

from ai_employee.application.commands import TrustedCommand
from ai_employee.application.ports.trusted_actions import TrustedActionAdapter
from ai_employee.config import Settings
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel
from ai_employee.infrastructure.db.repositories.email import (
    MailConnectionCredentials,
    SqlAlchemyMailSyncRepository,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.testing.scenarios import M2FakeActionAdapter
from ai_employee.integrations.registry import CredentialBoundProviderRegistry

_SYNTHETIC_IDENTITY = re.compile(
    r"(?:google::|microsoft:synthetic-tenant:)synthetic-m2-"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def synthetic_account_id(provider: str, connection_id: UUID) -> str:
    """从合成连接主键确定身份，禁止通过用户可编辑的邮件字段选择账户。"""
    if provider not in {"google", "microsoft"}:
        raise ValueError("synthetic provider is unsupported")
    prefix = "synthetic-tenant:" if provider == "microsoft" else ""
    return f"{prefix}synthetic-m2-{connection_id}"


class SyntheticTrustedActionRegistry(CredentialBoundProviderRegistry):
    """保留固定四动作 slot，只解析经过用户和 UUID 身份双重绑定的 Fake。

    继承基础 slot 与 fail-closed refresh 协议，重写唯一凭据选择和 adapter 解析边界。
    组合根只在确切类型及同一个 Settings 对象配对时将它作为测试写策略；不会影响真实
    registry 的 Settings 门禁，更不会把三个真实写开关或允许列表改成放行值。
    """

    def __init__(self, *, session_factory: ManagedAsyncSessionMaker, settings: Settings) -> None:
        """验证双开关后固定注册 Fake；初始化没有数据库、Secret 或网络 I/O。"""
        if settings.app_env != "test" or not settings.app_test_mode:
            raise ValueError("synthetic actions require both test switches")
        super().__init__(session_factory=session_factory, settings=settings)

    def bound_to(self, settings: Settings) -> bool:
        """只有构造时同一配置实例可取得此 Fake 的配对策略。"""
        return self._settings is settings and settings.app_env == "test" and settings.app_test_mode

    def provider_writes_enabled(self, provider: str) -> bool:
        """仅授权封闭两家 Fake；真实配置值保持关闭且不授予真实 adapter 权限。"""
        return self.bound_to(self._settings) and provider in {"google", "microsoft"}

    def write_account_allowed(self, provider_identity_key: str) -> bool:
        """只接受专用合成 UUID 命名空间；本地连接解析还会核对精确主键和用户。"""
        return self.bound_to(self._settings) and _SYNTHETIC_IDENTITY.fullmatch(provider_identity_key) is not None

    async def _connection_credentials(
        self, *, user_id: UUID, provider: str, connection_id: UUID, action: str
    ) -> MailConnectionCredentials:
        """读取同用户连接和能力，再核对合成账户；凭据只检查存在，绝不解密。

        任何跨用户、断连、读能力丢失或非合成连接都在 execute 前拒绝。请求前的写能力、
        审批和版本检查仍由生产事务执行，本方法不会替代或跳过它们。
        """
        self.trusted_action_adapter(provider=provider, action=action)
        async with self._sessions() as session:
            credentials = await SqlAlchemyMailSyncRepository(session).get_credentials(
                user_id=user_id, connection_id=connection_id, expected_provider=provider,
                required_capability="mail.read" if action == "mail.send" else "calendar.read",
            )
            connection = await session.scalar(select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == connection_id, OAuthConnectionModel.user_id == user_id,
            ))
            if (
                credentials is None or connection is None or connection.user_id != user_id
                or connection.provider_account_id != synthetic_account_id(provider, connection_id)
                or connection.provider_tenant_id != ("synthetic-tenant" if provider == "microsoft" else "")
                or connection.account_email != f"synthetic-{connection_id}@example.test"
            ):
                raise StateConflictError(
                    error_code="provider_action_unavailable", message="Synthetic connection is unavailable"
                )
        return credentials

    async def resolve_trusted_action_adapter(
        self, *, user_id: UUID, provider: str, command: TrustedCommand
    ) -> TrustedActionAdapter:
        """审批预检与执行均返回无网络 Fake，读取场景只发生在实际 execute 内。"""
        await self._connection_credentials(
            user_id=user_id, provider=provider, connection_id=command.connection_id, action=command.action,
        )
        return M2FakeActionAdapter(redis_url=self._settings.redis_url, user_id=user_id, provider=provider)
