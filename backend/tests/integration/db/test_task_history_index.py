"""在受管临时head数据库验证任务历史索引目录与只读查询计划。"""

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text

from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory

pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def history_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """只使用经过原生命周期验证的regular数据库。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def history_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """复用受管迁移，禁止修改共享anchor的schema。"""
    del cycle5_regular_database_url
    yield


@pytest.fixture
async def session_factory(
    database_url: ValidatedTestDatabaseUrl,
    cycle5_tracked_session_factories: None,
) -> ManagedAsyncSessionMaker:
    """由tracked registry创建会话工厂，测试失败也能先释放连接再清理。"""
    del cycle5_tracked_session_factories
    return build_session_factory(database_url)


async def test_history_index_catalog_and_seek_plan(
    session_factory: ManagedAsyncSessionMaker,
) -> None:
    """验证事务迁移产生有效三列索引，倒序seek不加载任务载荷或步骤。"""
    async with session_factory() as session:
        row = (
            await session.execute(
                text("""
            SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) AS definition
            FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = 'ix_task_runs_user_created_id'
        """)
            )
        ).one()
        assert row.indisvalid is True
        assert "(user_id, created_at, id)" in row.definition
        owner_id = UUID(int=1)
        session.add(
            UserModel(
                id=owner_id,
                email="history-index@example.test",
                display_name="合成历史用户",
                timezone="UTC",
                brief_time=time(9),
            )
        )
        await session.flush()
        session.add_all(
            TaskRunModel(
                id=UUID(int=number + 10),
                user_id=owner_id,
                kind="chat",
                status="succeeded",
                idempotency_key=f"history-index-{number}",
                input_payload={},
                created_at=datetime(2029, 1, 1, tzinfo=UTC) + timedelta(seconds=number),
            )
            for number in range(2000)
        )
        await session.flush()
        # 使用合成负载与默认planner选择验证倒序seek；不作耗时断言或添加第二索引。
        await session.execute(text("ANALYZE task_runs"))
        plan = (
            await session.execute(
                text("""
            EXPLAIN (FORMAT JSON, VERBOSE)
            SELECT id, kind, status, created_at FROM task_runs
            WHERE user_id = '00000000-0000-0000-0000-000000000001'::uuid
              AND (created_at, id) < (
                '2030-01-01T00:00:00Z'::timestamptz,
                '00000000-0000-0000-0000-000000000002'::uuid)
            ORDER BY created_at DESC, id DESC LIMIT 20
        """)
            )
        ).scalar_one()[0]["Plan"]
        scan = plan["Plans"][0]
        assert scan["Index Name"] == "ix_task_runs_user_created_id"
        assert scan["Scan Direction"] == "Backward"
        assert scan["Relation Name"] == "task_runs"
        assert scan["Output"] == ["id", "kind", "status", "created_at"]
