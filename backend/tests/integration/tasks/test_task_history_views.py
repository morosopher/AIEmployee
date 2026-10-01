"""真实PostgreSQL验证隔离、精确keyset与单请求只读快照。"""

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, event, text, update
from sqlalchemy.engine import Connection, Result
from sqlalchemy.sql import Executable

from ai_employee.application.task_history import (
    HistoryCursorState,
    HistoryDirection,
    HistoryReadPage,
    TaskHistoryFilters,
    TaskHistoryQuery,
    history_filters_hash,
    normalize_history_filters,
)
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.task_history_views import (
    SqlAlchemyTaskHistoryReader,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from tests.integration.tasks.history_fixtures import BASE_TIME, HistoryDataset, seed_history_dataset

pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def history_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """复用受管regular临时数据库，禁止修改共享anchor。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def history_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """迁移由regular生命周期完成；本模块不另行迁移。"""
    del cycle5_regular_database_url
    yield


@pytest.fixture
async def session_factory(
    database_url: ValidatedTestDatabaseUrl, cycle5_tracked_session_factories: None
) -> ManagedAsyncSessionMaker:
    """使用可跟踪工厂保证失败时先释放pool。"""
    del cycle5_tracked_session_factories
    return build_session_factory(database_url)


@pytest.fixture
async def history_dataset(session_factory: ManagedAsyncSessionMaker) -> HistoryDataset:
    """生成固定合成数据，无正文或真实账号。"""
    return await seed_history_dataset(session_factory)


@pytest.fixture
async def history_reader(session_factory: ManagedAsyncSessionMaker) -> SqlAlchemyTaskHistoryReader:
    """构造仅有会话依赖的只读适配器。"""
    return SqlAlchemyTaskHistoryReader(session_factory)


def filters_for(dataset: HistoryDataset) -> TaskHistoryFilters:
    """返回全部任务的默认筛选；其他组合由测试显式规范化。"""
    return normalize_history_filters(
        user_id=dataset.owner_id, timezone="UTC", query=TaskHistoryQuery(scope="all")
    )


def cursor_for(
    page: HistoryReadPage, filters: TaskHistoryFilters, direction: HistoryDirection
) -> HistoryCursorState:
    """从真实方向锚点构造状态，不依赖锚点记录继续存在。"""
    anchor = page.next_anchor if direction == "older" else page.previous_anchor
    assert anchor is not None and page.upper is not None
    return HistoryCursorState(
        filters.user_id, history_filters_hash(filters), direction, page.upper, anchor, BASE_TIME
    )


async def test_pages_are_user_scoped_and_bidirectional(
    history_reader: SqlAlchemyTaskHistoryReader, history_dataset: HistoryDataset
) -> None:
    """20+5全量遍历且回上一页，包含同微秒UUID的严格倒序。"""
    filters = filters_for(history_dataset)
    first = await history_reader.read_page(filters=filters, cursor=None)
    assert tuple(row.id for row in first.items) == tuple(
        reversed(history_dataset.owner_task_ids[5:])
    )
    assert first.previous_anchor is None and first.next_anchor is not None
    second = await history_reader.read_page(
        filters=filters, cursor=cursor_for(first, filters, "older")
    )
    assert tuple(row.id for row in second.items) == tuple(
        reversed(history_dataset.owner_task_ids[:5])
    )
    assert second.next_anchor is None
    back = await history_reader.read_page(
        filters=filters, cursor=cursor_for(second, filters, "newer")
    )
    assert back.items == first.items
    assert first.background_failed_count == second.background_failed_count == 8


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (TaskHistoryQuery(), 10),
        (TaskHistoryQuery(scope="background"), 15),
        (TaskHistoryQuery(scope="all", kind="other"), 5),
        (TaskHistoryQuery(kind="daily_brief", status="failed"), 3),
        (TaskHistoryQuery(scope="all", created_from_date="2029-01-02"), 0),
        (TaskHistoryQuery(scope="all", created_to_date="2028-12-31"), 0),
    ],
)
async def test_filters_and_independent_failed_count(
    history_reader: SqlAlchemyTaskHistoryReader,
    history_dataset: HistoryDataset,
    query: TaskHistoryQuery,
    expected: int,
) -> None:
    """业务/后台/other/精确类型状态及日期，不缩窄后台失败口径。"""
    filters = normalize_history_filters(
        user_id=history_dataset.owner_id, timezone="UTC", query=query
    )
    page = await history_reader.read_page(filters=filters, cursor=None)
    assert len(page.items) == expected
    assert page.background_failed_count == (8 if expected else 0)
    if not expected:
        assert page.upper is page.next_anchor is page.previous_anchor is None


async def test_deleted_anchor_empty_window_and_no_recovery_loop(
    history_reader: SqlAlchemyTaskHistoryReader,
    history_dataset: HistoryDataset,
    session_factory: ManagedAsyncSessionMaker,
) -> None:
    """删除锚点仍能seek；整个older窗口消失时只保留有记录的反向入口。"""
    filters = filters_for(history_dataset)
    first = await history_reader.read_page(filters=filters, cursor=None)
    cursor = cursor_for(first, filters, "older")
    async with session_factory.begin() as session:
        await session.execute(
            delete(TaskRunModel).where(
                TaskRunModel.user_id == filters.user_id, TaskRunModel.id == cursor.anchor.task_id
            )
        )
    second = await history_reader.read_page(filters=filters, cursor=cursor)
    assert len(second.items) == 5
    async with session_factory.begin() as session:
        await session.execute(
            delete(TaskRunModel).where(
                TaskRunModel.user_id == filters.user_id,
                TaskRunModel.id.in_(history_dataset.owner_task_ids[:5]),
            )
        )
    empty = await history_reader.read_page(filters=filters, cursor=cursor)
    assert not empty.items and empty.next_anchor is None and empty.previous_anchor == cursor.anchor
    recovered = await history_reader.read_page(
        filters=filters, cursor=cursor_for(empty, filters, "newer")
    )
    assert len(recovered.items) == 19 and recovered.next_anchor is None
    async with session_factory.begin() as session:
        await session.execute(delete(TaskRunModel).where(TaskRunModel.user_id == filters.user_id))
    gone = await history_reader.read_page(filters=filters, cursor=cursor)
    assert not gone.items and gone.next_anchor is gone.previous_anchor is None


async def test_upper_and_newer_empty_window(
    history_reader: SqlAlchemyTaskHistoryReader,
    history_dataset: HistoryDataset,
    session_factory: ManagedAsyncSessionMaker,
) -> None:
    """上界固定，不让更新的失败任务挤入，但计数仍包含上界外失败。"""
    filters = filters_for(history_dataset)
    first = await history_reader.read_page(filters=filters, cursor=None)
    second = await history_reader.read_page(
        filters=filters, cursor=cursor_for(first, filters, "older")
    )
    newer = cursor_for(second, filters, "newer")
    async with session_factory.begin() as session:
        await session.execute(
            update(TaskRunModel)
            .where(
                TaskRunModel.user_id == filters.user_id,
                TaskRunModel.id.in_(history_dataset.owner_task_ids[5:]),
            )
            .values(created_at=BASE_TIME + timedelta(days=1))
        )
    empty = await history_reader.read_page(filters=filters, cursor=newer)
    assert not empty.items and empty.previous_anchor is None and empty.next_anchor == newer.anchor
    assert empty.background_failed_count == 8
    recovered = await history_reader.read_page(
        filters=filters, cursor=cursor_for(empty, filters, "older")
    )
    assert len(recovered.items) == 4 and recovered.previous_anchor is not None


async def test_snapshot_projection_and_closed_transaction(
    history_reader: SqlAlchemyTaskHistoryReader,
    history_dataset: HistoryDataset,
    session_factory: ManagedAsyncSessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首个SELECT后并发提交状态/删除，摘要、计数、方向仍来自同一只读快照。"""
    from sqlalchemy.ext.asyncio import AsyncSession

    original = AsyncSession.execute
    snapshot = asyncio.Event()
    changed = asyncio.Event()
    statements: list[str] = []
    intercepted = False

    async def execute(
        self: AsyncSession, statement: Executable, *args: Any, **kwargs: Any
    ) -> Result[Any]:
        """包装SQLAlchemy开放执行边界；仅在第一个已建立的快照后等待并发提交。"""
        nonlocal intercepted
        result = await original(self, statement, *args, **kwargs)
        sql = str(statement)
        if sql.startswith("SELECT") and not intercepted:
            intercepted = True
            settings = (
                await original(
                    self,
                    text(
                        "SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')"
                    ),
                )
            ).one()
            assert tuple(settings) == ("repeatable read", "on")
            snapshot.set()
            await asyncio.wait_for(changed.wait(), 10)
        return result

    def record(
        conn: Connection,
        cur: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        """仅收集SQL模板，禁止把绑定参数或游标值写入证据。"""
        statements.append(statement)

    monkeypatch.setattr(AsyncSession, "execute", execute)
    event.listen(session_factory.engine.sync_engine, "before_cursor_execute", record)

    async def mutate() -> None:
        await asyncio.wait_for(snapshot.wait(), 10)
        async with session_factory.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(
                    TaskRunModel.user_id == history_dataset.owner_id,
                    TaskRunModel.kind.not_in(("daily_brief", "conversation.respond")),
                )
                .values(status="succeeded")
            )
            await session.execute(
                delete(TaskRunModel).where(
                    TaskRunModel.user_id == history_dataset.owner_id,
                    TaskRunModel.id.in_(history_dataset.owner_task_ids[:5]),
                )
            )
        changed.set()

    try:
        page, _ = await asyncio.gather(
            history_reader.read_page(filters=filters_for(history_dataset), cursor=None), mutate()
        )
    finally:
        event.remove(session_factory.engine.sync_engine, "before_cursor_execute", record)
    assert len(page.items) == 20 and page.next_anchor is not None
    assert page.background_failed_count == 8
    assert any(row.status.value == "failed" and row.kind == "sync_mail" for row in page.items)
    selects = [sql for sql in statements if sql.startswith("SELECT") and "task_runs" in sql]
    assert len(selects) == 4
    projection = selects[0].split(" FROM ")[0].replace("\n", " ").split(" FROM ")[0]
    assert projection.removeprefix("SELECT ").strip().split(", ") == [
        f"task_runs.{column}"
        for column in (
            "id",
            "kind",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "error_code",
            "retry_of_task_id",
        )
    ]
    assert all("user_id" in sql for sql in selects)
    assert all(
        forbidden not in " ".join(selects)
        for forbidden in ("input_payload", "result_payload", "lease_owner", "task_steps")
    )
    assert session_factory.engine.pool.checkedout() == 0
    fresh = await history_reader.read_page(filters=filters_for(history_dataset), cursor=None)
    assert fresh.background_failed_count == 0 and fresh.next_anchor is None


async def test_projected_seek_explain(
    session_factory: ManagedAsyncSessionMaker, history_dataset: HistoryDataset
) -> None:
    """合成较大集合验证八列seek计划使用既有三列索引，不为计数臆加索引。"""
    async with session_factory.begin() as session:
        await session.execute(
            text("""
            INSERT INTO task_runs (id, user_id, kind, status, idempotency_key, input_payload, created_at)
            SELECT md5('history-explain-' || n::text)::uuid, :owner, 'sync_mail', 'failed',
                   'history-explain-' || n::text, '{}'::jsonb,
                   '2028-01-01T00:00:00Z'::timestamptz + n * interval '1 second'
            FROM generate_series(1, 3000) n
        """),
            {"owner": history_dataset.owner_id},
        )
        await session.execute(text("ANALYZE task_runs"))
        plan = (
            await session.execute(
                text("""
            EXPLAIN (FORMAT JSON, VERBOSE)
            SELECT id, kind, status, created_at, started_at, finished_at, error_code, retry_of_task_id
            FROM task_runs WHERE user_id = :owner
            AND (created_at, id) < (:anchor_time, :anchor_id)
            ORDER BY created_at DESC, id DESC LIMIT 21
        """),
                {
                    "owner": history_dataset.owner_id,
                    "anchor_time": BASE_TIME,
                    "anchor_id": history_dataset.owner_task_ids[0],
                },
            )
        ).scalar_one()[0]["Plan"]
    scan = plan["Plans"][0]
    assert scan["Index Name"] == "ix_task_runs_user_created_id"
    assert scan["Scan Direction"] == "Backward"
    assert scan["Output"] == [
        "id",
        "kind",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "error_code",
        "retry_of_task_id",
    ]


@pytest.mark.parametrize("limit", [1, 100])
async def test_page_limits_and_retry_metadata(
    history_reader: SqlAlchemyTaskHistoryReader,
    history_dataset: HistoryDataset,
    session_factory: ManagedAsyncSessionMaker,
    limit: int,
) -> None:
    """最小/最大页大小保留重试独立记录、可空执行时间和错误码。"""
    async with session_factory.begin() as session:
        await session.execute(
            update(TaskRunModel)
            .where(
                TaskRunModel.user_id == history_dataset.owner_id,
                TaskRunModel.id == history_dataset.owner_task_ids[-1],
            )
            .values(
                retry_of_task_id=history_dataset.owner_task_ids[0],
                error_code="synthetic_failure",
                started_at=BASE_TIME,
                finished_at=BASE_TIME + timedelta(seconds=1),
            )
        )
    filters = normalize_history_filters(
        user_id=history_dataset.owner_id,
        timezone="UTC",
        query=TaskHistoryQuery(scope="all", limit=limit),
    )
    page = await history_reader.read_page(filters=filters, cursor=None)
    assert len(page.items) == min(limit, 25)
    assert page.items[0].retry_of_task_id == history_dataset.owner_task_ids[0]
    assert page.items[0].error_code == "synthetic_failure"
    assert page.items[0].started_at == BASE_TIME
    assert page.items[0].finished_at == BASE_TIME + timedelta(seconds=1)


async def test_new_commit_does_not_cross_upper_but_status_changes_are_visible(
    history_reader: SqlAlchemyTaskHistoryReader,
    history_dataset: HistoryDataset,
    session_factory: ManagedAsyncSessionMaker,
) -> None:
    """分页间新增行受upper排除；旧行状态变化按当前请求快照重新筛选。"""
    from uuid import UUID

    filters = normalize_history_filters(
        user_id=history_dataset.owner_id,
        timezone="UTC",
        query=TaskHistoryQuery(scope="all", status="failed", limit=3),
    )
    first = await history_reader.read_page(filters=filters, cursor=None)
    async with session_factory.begin() as session:
        session.add(
            TaskRunModel(
                id=UUID(int=9999),
                user_id=history_dataset.owner_id,
                kind="sync_mail",
                status="failed",
                idempotency_key="new-history-after-upper",
                input_payload={},
                created_at=BASE_TIME + timedelta(days=1),
            )
        )
        await session.execute(
            update(TaskRunModel)
            .where(
                TaskRunModel.user_id == history_dataset.owner_id,
                TaskRunModel.id == history_dataset.owner_task_ids[18],
            )
            .values(status="succeeded")
        )
    second = await history_reader.read_page(
        filters=filters, cursor=cursor_for(first, filters, "older")
    )
    assert [row.id for row in second.items] == [
        history_dataset.owner_task_ids[i] for i in (16, 14, 12)
    ]
    assert second.upper == first.upper
    assert second.background_failed_count == 8
    fresh = await history_reader.read_page(filters=filters, cursor=None)
    assert fresh.items[0].id == UUID(int=9999)
