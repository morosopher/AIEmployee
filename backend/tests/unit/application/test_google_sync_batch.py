"""验证手动 Google 同步的两个任务创建共享同一事务边界。"""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

from ai_employee.application.use_cases.connections import GoogleConnectionsUseCase
from ai_employee.application.use_cases.tasks import (
    CreateTaskBatchItem,
    CreateTaskResult,
    CreateTaskUseCase,
)
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.workers.sync_calendar import CalendarSyncTaskStep


@dataclass
class RecordingRepository:
    """模拟可回滚事务存储，用于证明第二次持久化失败不会提交第一项。"""

    fail_on_second: bool = False
    pending: list[str] = field(default_factory=list)
    committed: list[str] = field(default_factory=list)

    async def create_with_outbox(
        self, *, user_id: UUID, kind: str, input_payload: dict, idempotency_key: str
    ) -> CreateTaskResult:
        """记录一项待提交任务，并按测试开关在 Calendar 项模拟持久化失败。"""
        del user_id, input_payload
        self.pending.append(kind)
        if self.fail_on_second and kind == "sync_calendar":
            raise RuntimeError("simulated second persistence failure")
        return CreateTaskResult(uuid4())

    async def create_many_with_outbox(
        self, *, user_id: UUID, items: tuple[CreateTaskBatchItem, ...]
    ) -> tuple[CreateTaskResult, ...]:
        """在同一待提交列表中顺序写入，异常由上下文管理器整体回滚。"""
        return tuple(
            [
                await self.create_with_outbox(
                    user_id=user_id,
                    kind=item.kind,
                    input_payload=item.input_payload,
                    idempotency_key=item.idempotency_key,
                )
                for item in items
            ]
        )


class RecordingFactory:
    """以显式 commit/rollback 行为模拟 Repository factory。"""

    def __init__(self, repository: RecordingRepository) -> None:
        """保存单测可观测的事务对象。"""
        self.repository = repository

    @asynccontextmanager
    async def __call__(self):
        """异常时清空 pending，正常返回才复制为 committed。"""
        try:
            yield self.repository
        except Exception:
            self.repository.pending.clear()
            raise
        else:
            self.repository.committed.extend(self.repository.pending)
            self.repository.pending.clear()


class RecordingDispatcher:
    """记录提交后投递，防止回滚路径仍产生外部副作用。"""

    def __init__(self) -> None:
        """初始化空的已投递任务列表。"""
        self.dispatched: list[UUID] = []

    async def dispatch(self, task_id: UUID) -> TaskStatus:
        """记录投递并返回稳定创建状态。"""
        self.dispatched.append(task_id)
        return TaskStatus.CREATED


class ConnectedStore:
    """只向手动同步用例返回一条归属正确且仍连接的合成连接。"""

    def __init__(self, connection: OAuthConnectionModel) -> None:
        """保存测试所需的唯一连接，不实现本用例不会调用的 OAuth 写操作。"""
        self._connection = connection

    async def get_connection(
        self, *, user_id: UUID, connection_id: UUID
    ) -> OAuthConnectionModel | None:
        """仅在用户与连接标识同时匹配时返回连接，保留归属边界。"""
        if self._connection.user_id != user_id or self._connection.id != connection_id:
            return None
        return self._connection


@dataclass
class CapturingTaskCreator:
    """记录手动同步创建的精确任务种类、载荷与幂等键。"""

    items: tuple[CreateTaskBatchItem, ...] = ()

    async def execute_many(
        self,
        *,
        user_id: UUID,
        items: tuple[CreateTaskBatchItem, ...],
    ) -> tuple[CreateTaskResult, ...]:
        """保存批次并返回两个稳定结果，不执行数据库或队列副作用。"""
        del user_id
        self.items = items
        return (CreateTaskResult(uuid4()), CreateTaskResult(uuid4()))


@pytest.mark.asyncio
async def test_batch_creation_rolls_back_both_tasks_when_second_persistence_fails() -> None:
    """Calendar 写入失败时邮件任务、审计和 Outbox 也不能提交或投递。"""
    repository = RecordingRepository(fail_on_second=True)
    dispatcher = RecordingDispatcher()
    use_case = CreateTaskUseCase(RecordingFactory(repository), dispatcher)

    with pytest.raises(RuntimeError, match="second persistence failure"):
        await use_case.execute_many(
            user_id=uuid4(),
            items=(
                CreateTaskBatchItem(
                    "sync_mail",
                    {"connection_id": "one", "scope_key": "mailbox"},
                    "sync:gmail",
                ),
                CreateTaskBatchItem("sync_calendar", {"connection_id": "one"}, "sync:calendar"),
            ),
        )

    assert repository.committed == []
    assert dispatcher.dispatched == []


@pytest.mark.asyncio
async def test_manual_sync_creates_canonical_tasks_with_explicit_owner_scopes() -> None:
    """新手动同步显式创建 mailbox 与 directory owner，旧任务才允许缺 scope。"""
    user_id, connection_id = uuid4(), uuid4()
    connection = OAuthConnectionModel(
        id=connection_id,
        user_id=user_id,
        provider="google",
        provider_account_id="manual-sync",
        account_email="manual-sync@example.test",
        scopes=[],
        status="connected",
        last_error_code=None,
    )

    @asynccontextmanager
    async def stores():
        """为单次调用提供只读连接存储上下文。"""
        yield ConnectedStore(connection)

    tasks = CapturingTaskCreator()
    use_case = GoogleConnectionsUseCase(
        stores,  # type: ignore[arg-type]
        AeadCipher(b"k" * 32),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        "synthetic-client",
        "https://example.test/callback",
    )

    await use_case.start_manual_sync(
        user_id=user_id,
        connection_id=connection_id,
        idempotency_key="manual-sync-key",
        tasks=tasks,
    )

    assert tasks.items == (
        CreateTaskBatchItem(
            "sync_mail",
            {"connection_id": str(connection_id), "scope_key": "mailbox"},
            "manual-sync-key:gmail",
        ),
        CreateTaskBatchItem(
            "sync_calendar",
            {"connection_id": str(connection_id), "scope_key": "directory"},
            "manual-sync-key:calendar",
        ),
    )


def test_calendar_worker_routes_legacy_missing_scope_through_directory() -> None:
    """缺少 scope 的持久旧任务也必须先经过目录发现，不能绕过目录证明 primary。"""
    assert CalendarSyncTaskStep._resolve_scope_key("directory") == "directory"
    assert CalendarSyncTaskStep._resolve_scope_key(None) == "directory"
    with pytest.raises(ValueError, match="requires scope_key"):
        CalendarSyncTaskStep._resolve_scope_key("")
