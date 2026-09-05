"""验证连接撤销在本地事务与供应商网络之间保持 fail-closed 顺序。"""

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.oauth import (
    OAuthProvider,
    OAuthRevocationResult,
    OAuthRevocationStatus,
)
from ai_employee.application.use_cases.connections import ConnectionsUseCase, StoredConnection
from ai_employee.infrastructure.security.encryption import AeadCipher

NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _FixedClock:
    """返回测试冻结的 UTC 时刻，避免撤权事实依赖真实时间。"""

    def now(self) -> datetime:
        """返回固定时刻。"""
        return NOW


@dataclass(slots=True)
class _DisconnectStore:
    """模拟提交边界并返回记录绑定的合成 refresh-token 密文。"""

    user_id: UUID
    connection_id: UUID
    refresh: EncryptedValue
    transaction_open: bool = False
    committed: bool = False
    unresolved: list[dict[str, object]] = field(default_factory=list)

    async def get_connection(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> StoredConnection | None:
        """返回不含凭据的连接投影。"""
        assert user_id == self.user_id and connection_id == self.connection_id
        return StoredConnection(
            id=connection_id,
            user_id=user_id,
            provider="google",
            provider_account_id="synthetic-account",
            provider_tenant_id="",
            account_type="google",
            account_email="owner@example.test",
            scopes=(),
            status="connected",
            last_error_code=None,
        )

    async def disconnect(self, **values: Any) -> tuple[bool, EncryptedValue | None]:
        """返回即将在当前事务删除的密文投影。"""
        assert self.transaction_open
        assert values["user_id"] == self.user_id
        assert values["connection_id"] == self.connection_id
        return True, self.refresh

    async def record_oauth_revoke_unresolved(self, **values: Any) -> None:
        """只收集无凭据维护事实，供缺失适配器的断开路径断言。"""
        assert self.committed
        self.unresolved.append(values)


class _TransactionAwareCipher:
    """记录解密是否发生在本地删除事务仍未提交时。"""

    def __init__(self, store: _DisconnectStore, delegate: AeadCipher) -> None:
        """保存事务探针与真实 AEAD 实现。"""
        self._store = store
        self._delegate = delegate
        self.decrypted_while_open = False

    def encrypt(self, plaintext: bytes, aad: bytes) -> EncryptedValue:
        """委托真实 AEAD，保持测试密文与生产边界一致。"""
        return self._delegate.encrypt(plaintext, aad)

    def decrypt(self, value: EncryptedValue, aad: bytes) -> bytes:
        """在解密时捕获事务状态，不记录 token 内容。"""
        self.decrypted_while_open = self._store.transaction_open
        return self._delegate.decrypt(value, aad)


@dataclass(slots=True)
class _RevocationAdapter:
    """确认远端调用只发生在本地断开事务提交之后。"""

    store: _DisconnectStore
    provider: OAuthProvider = OAuthProvider.GOOGLE
    calls: int = 0

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """消费受控内存 token，且不把它保存到 Fake 状态。"""
        assert self.store.committed and not self.store.transaction_open
        assert token
        self.calls += 1
        return OAuthRevocationResult(OAuthRevocationStatus.REVOKED)


@pytest.mark.asyncio
async def test_disconnect_decrypts_before_local_commit_and_revokes_only_after_commit() -> None:
    """解密移到事务外会破坏“先受控取出、再永久删密文”的断开协议。"""
    user_id, connection_id = uuid4(), uuid4()
    delegate = AeadCipher(b"r" * 32)
    aad = f"{user_id}:{connection_id}:refresh_token".encode("ascii")
    store = _DisconnectStore(
        user_id=user_id,
        connection_id=connection_id,
        refresh=delegate.encrypt(b"synthetic-revocation-value", aad),
    )
    cipher = _TransactionAwareCipher(store, delegate)
    adapter = _RevocationAdapter(store)

    @asynccontextmanager
    async def stores() -> Any:
        """在退出时模拟生产事务提交。"""
        store.transaction_open = True
        try:
            yield store
        finally:
            store.transaction_open = False
            store.committed = True

    use_case = ConnectionsUseCase(stores, cipher, {"google": adapter}, _FixedClock())

    result = await use_case.disconnect(user_id=user_id, connection_id=connection_id)

    assert result == OAuthRevocationResult(OAuthRevocationStatus.REVOKED)
    assert cipher.decrypted_while_open is True
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_disconnect_without_adapter_records_unresolved_after_local_commit() -> None:
    """当前进程未组装安全撤销端点时也必须留下维护事实，不能静默丢弃撤销责任。"""
    user_id = UUID("00000000-0000-0000-0000-000000000101")
    connection_id = UUID("00000000-0000-0000-0000-000000000201")
    cipher = AeadCipher(b"r" * 32)
    store = _DisconnectStore(
        user_id=user_id,
        connection_id=connection_id,
        refresh=cipher.encrypt(
            b"synthetic-revoke-value",
            f"{user_id}:{connection_id}:refresh_token".encode("ascii"),
        ),
    )

    @asynccontextmanager
    async def stores() -> Any:
        """模拟两个独立事务；维护事实只能在删除事务提交后写入。"""
        store.transaction_open = True
        try:
            yield store
        finally:
            store.transaction_open = False
            store.committed = True

    result = await ConnectionsUseCase(stores, cipher, {}, _FixedClock()).disconnect(
        user_id=user_id, connection_id=connection_id
    )

    assert result is not None and result.status is OAuthRevocationStatus.UNSUPPORTED
    assert store.unresolved == [
        {
            "user_id": user_id,
            "connection_id": connection_id,
            "provider": "google",
            "error_code": result.error_code,
            "occurred_at": NOW,
        }
    ]


@pytest.mark.parametrize("count", [3, 0, -1, "synthetic-sensitive-count", True])
def test_revoke_backlog_json_log_exposes_only_valid_integer_count(count: object) -> None:
    """生产 JSON 告警必须保留积压计数，同时拒绝字符串和布尔等非计数内容。"""
    from ai_employee.infrastructure.observability.logging import _JsonFormatter

    record = logging.getLogger("ai_employee.oauth.revoke_backlog").makeRecord(
        "ai_employee.oauth.revoke_backlog",
        logging.WARNING,
        __file__,
        1,
        "unused",
        (),
        None,
        extra={
            "provider": "google",
            "error_code": "oauth_revoke_unresolved",
            "unresolved_count": count,
            "account_email": "synthetic@example.test",
        },
    )
    payload = json.loads(_JsonFormatter(()).format(record))
    if type(count) is int and count >= 0:
        assert payload["unresolved_count"] == count
    else:
        assert "unresolved_count" not in payload
    assert "account_email" not in payload
