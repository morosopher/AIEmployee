"""验证保留清理的配置安全边界。"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from ai_employee.config import get_settings
from ai_employee.infrastructure.db.models.briefs import ConversationModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.workers.retention import RetentionCleanupWorker, build_retention_cleanup_worker


class _NoopSession:
    """提供旧实现仍会进入的最小事务接口，避免测试依赖 SQLAlchemy。"""

    async def execute(self, statement: object) -> None:
        """接受清理 SQL；本测试只验证 Worker 的策略编排顺序。"""
        del statement

    def add(self, item: object) -> None:
        """接受审计对象；内容不属于本编排测试的断言边界。"""
        del item

    async def scalars(self, statement: object) -> "_NoopSession":
        """新增动作与OAuth阶段没有候选；具体保留行为由真实数据库矩阵覆盖。"""
        del statement
        return self

    def all(self) -> list[object]:
        """返回空扫描集合，不伪造行权限或持久状态。"""
        return []


class _NoopSessionFactory:
    """返回可重复进入的空异步事务，隔离策略测试与数据库 I/O。"""

    def begin(self) -> "_NoopSessionFactory":
        """返回自身作为短事务上下文。"""
        return self

    def __call__(self) -> "_NoopSessionFactory":
        """提供新阶段使用的短只读扫描会话。"""
        return self

    async def __aenter__(self) -> _NoopSession:
        """进入合成事务。"""
        return _NoopSession()

    async def __aexit__(self, *args: object) -> None:
        """退出合成事务；测试不模拟提交错误。"""
        del args


class _RecordingRetentionWorker(RetentionCleanupWorker):
    """记录每个用户保留阶段，证明历史清理不会被遗漏或排到审计之后。"""

    def __init__(self) -> None:
        """使用空工厂并初始化阶段记录。"""
        super().__init__(_NoopSessionFactory())  # type: ignore[arg-type]
        self.calls: list[str] = []

    async def _scrub_email_bodies(self, user_id: UUID, cutoff: datetime, batch_size: int) -> None:
        """记录正文擦除阶段。"""
        del user_id, cutoff, batch_size
        self.calls.append("body")

    async def _scrub_calendar_content(
        self, user_id: UUID, cutoff: datetime, batch_size: int
    ) -> None:
        """记录M2描述/地点四列清理，必须早于来源元数据删除。"""
        del user_id, cutoff, batch_size
        self.calls.append("calendar")

    async def _delete_source_metadata(
        self, user_id: UUID, cutoff: datetime, batch_size: int
    ) -> None:
        """记录来源元数据阶段。"""
        del user_id, cutoff, batch_size
        self.calls.append("source")

    async def _delete_workspace_history(
        self, user_id: UUID, cutoff: datetime, batch_size: int
    ) -> None:
        """记录工作区历史与终态任务图清理阶段。"""
        del user_id, cutoff, batch_size
        self.calls.append("workspace")

    async def _clean_disconnected_credentials(self, user_id: UUID, batch_size: int) -> None:
        """记录断开凭据及同步游标处理阶段。"""
        del user_id, batch_size
        self.calls.append("credentials")

    async def _append_cleanup_audit(self, user_id: UUID, now: datetime) -> None:
        """记录必须最后写入的本轮无内容审计事实。"""
        del user_id, now
        self.calls.append("audit")


def test_production_retention_worker_rejects_missing_dedicated_dsn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """生产环境缺少 retention 专用连接串时不得静默使用普通应用角色。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("RETENTION_DATABASE_URL_FILE", str(tmp_path / "missing"))
    get_settings.cache_clear()
    build_retention_cleanup_worker.cache_clear()

    with pytest.raises(RuntimeError, match="RETENTION_DATABASE_URL_FILE"):
        build_retention_cleanup_worker()

    get_settings.cache_clear()
    build_retention_cleanup_worker.cache_clear()


@pytest.mark.asyncio
async def test_workspace_history_and_disconnected_credentials_are_processed_before_run_audit() -> (
    None
):
    """保留清理必须先删除到期工作区图、重置断开同步状态，最后才写当前轮审计。"""
    worker = _RecordingRetentionWorker()
    user = SimpleNamespace(
        id=uuid4(),
        email_body_retention_days=30,
        source_metadata_retention_days=180,
        workspace_history_retention_days=365,
    )

    await worker._clean_user(user, now=datetime(2026, 8, 4, tzinfo=UTC), batch_size=10)

    assert worker.calls == ["body", "calendar", "source", "workspace", "credentials", "audit"]


class _AuditPreservingRetentionWorker(RetentionCleanupWorker):
    """记录工作区清理选择的表，避免测试依赖真实 PostgreSQL。"""

    def __init__(self) -> None:
        """使用空会话工厂并保存被请求删除的 ORM 表。"""
        super().__init__(_NoopSessionFactory())  # type: ignore[arg-type]
        self.deleted_models: list[type[object]] = []

    async def _delete_bounded(
        self,
        model: type[object],
        user_id: UUID,
        timestamp: object,
        cutoff: datetime,
        batch_size: int,
    ) -> None:
        """仅记录调用，隔离本测试与 SQLAlchemy 查询构造。"""
        del user_id, timestamp, cutoff, batch_size
        self.deleted_models.append(model)

    async def _delete_terminal_task_graph(
        self, user_id: UUID, cutoff: datetime, batch_size: int
    ) -> None:
        """阻止进入真实任务图 SQL，仅验证上层表选择边界。"""
        del user_id, cutoff, batch_size

    async def _delete_ordinary_audits(
        self, user_id: UUID, cutoff: datetime, batch_size: int
    ) -> None:
        """记录普通审计的专用路径；不能再期望用通用删除器跳过OAuth/authority保护。"""
        del user_id, cutoff, batch_size
        self.deleted_models.append(AuditEventModel)

    async def _delete_empty_conversations(self, user_id: UUID, batch_size: int) -> None:
        """阻止进入真实会话 SQL，子类可覆盖以观察该安全清理路径。"""
        del user_id, batch_size


@pytest.mark.asyncio
async def test_workspace_retention_deletes_expired_audit_events_in_bounded_batches() -> None:
    """工作区保留必须回收 cutoff 前的审计内容，当前轮审计则在该阶段后写入。"""
    worker = _AuditPreservingRetentionWorker()

    await worker._delete_workspace_history(uuid4(), datetime(2026, 8, 4, tzinfo=UTC), batch_size=10)

    assert AuditEventModel in worker.deleted_models


class _ConversationSafeRetentionWorker(_AuditPreservingRetentionWorker):
    """记录会话清理路径，证明新消息不会被旧会话级联删除。"""

    def __init__(self) -> None:
        """初始化空调用记录。"""
        super().__init__()
        self.empty_conversation_calls = 0

    async def _delete_empty_conversations(self, user_id: UUID, batch_size: int) -> None:
        """记录仅删除已无消息的会话路径。"""
        del user_id, batch_size
        self.empty_conversation_calls += 1


@pytest.mark.asyncio
async def test_workspace_retention_only_removes_empty_conversations() -> None:
    """旧会话含有新消息时必须保留，不能由父行级联删除仍有效的消息。"""
    worker = _ConversationSafeRetentionWorker()

    await worker._delete_workspace_history(uuid4(), datetime(2026, 8, 4, tzinfo=UTC), batch_size=10)

    assert ConversationModel not in worker.deleted_models
    assert worker.empty_conversation_calls == 1
