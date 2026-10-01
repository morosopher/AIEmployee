"""真实Cookie、PostgreSQL、签名游标组合验证列表隔离及只读契约。"""

import base64
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, update

from ai_employee.config import get_settings
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.passwords import PasswordHasher
from ai_employee.main import create_app
from tests.integration.tasks.history_fixtures import HistoryDataset, seed_history_dataset

HistoryClient = tuple[
    httpx.AsyncClient, httpx.AsyncClient, ManagedAsyncSessionMaker, HistoryDataset
]

pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def history_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """绑定既有受管regular数据库，避免重复迁移或修改共享anchor。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def history_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """迁移完全由既有lifecycle负责。"""
    del cycle5_regular_database_url
    yield


@pytest.fixture
async def history_client(
    database_url: ValidatedTestDatabaseUrl,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[
    tuple[httpx.AsyncClient, httpx.AsyncClient, ManagedAsyncSessionMaker, HistoryDataset]
]:
    """两位合成用户通过真实登录Cookie访问同一应用，Secret随临时目录清理。"""
    secret = tmp_path / "history-key"
    secret.write_text(base64.b64encode(bytes([251]) * 32).decode("ascii"))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_MASTER_KEY_FILE", str(secret))
    monkeypatch.setenv("SESSION_COOKIE_NAME", "history_test_session")
    get_settings.cache_clear()
    factory = build_session_factory(database_url)
    dataset = await seed_history_dataset(factory)
    async with factory.begin() as session:
        await session.execute(
            update(UserModel).values(
                password_hash=PasswordHasher().hash("synthetic-password"),
                is_active=True,
                timezone="Asia/Shanghai",
            )
        )
        await session.execute(
            update(TaskRunModel).values(
                input_payload={"private": "synthetic-private-input"},
                result_payload={"private": "synthetic-private-result"},
            )
        )
    app = create_app()
    try:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://testserver"
            ) as owner,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://testserver"
            ) as other,
        ):
            for client, user_id in ((owner, dataset.owner_id), (other, dataset.other_id)):
                login = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "email": f"history-{user_id.int}@example.test",
                        "password": "synthetic-password",
                    },
                )
                assert login.status_code == 200
            yield owner, other, factory, dataset
    finally:
        await factory.dispose()
        await app.state.auth_session_factory.dispose()
        get_settings.cache_clear()


async def test_summary_pagination_is_isolated_and_read_only(history_client: HistoryClient) -> None:
    """完整20+5遍历、反向恢复、跨用户游标拒绝且不新增任务/Outbox/审计。"""
    owner, other, factory, dataset = history_client

    async def counts() -> tuple[int, ...]:
        """登录完成后记录业务表计数，避免把认证自己的审计算成列表副作用。"""
        async with factory() as session:
            return tuple(
                [
                    int(await session.scalar(select(func.count()).select_from(model)) or 0)
                    for model in (TaskRunModel, OutboxEventModel, AuditEventModel)
                ]
            )

    before = await counts()
    first = await owner.get("/api/v1/tasks", params={"scope": "all"})
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    body = first.json()
    assert set(body) == {
        "items",
        "next_cursor",
        "previous_cursor",
        "server_time",
        "filter_timezone",
        "background_failed_count",
    }
    assert set(body["items"][0]) == {
        "id",
        "kind",
        "category",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "error_code",
        "retry_of_task_id",
    }
    assert body["filter_timezone"] == "Asia/Shanghai"
    assert body["background_failed_count"] == 8
    assert [item["id"] for item in body["items"]] == [
        str(key) for key in reversed(dataset.owner_task_ids[5:])
    ]
    assert "synthetic-private" not in first.text
    second = await owner.get(
        "/api/v1/tasks", params={"scope": "all", "cursor": body["next_cursor"]}
    )
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["items"]] == [
        str(key) for key in reversed(dataset.owner_task_ids[:5])
    ]
    back = await owner.get(
        "/api/v1/tasks", params={"scope": "all", "cursor": second.json()["previous_cursor"]}
    )
    assert back.json()["items"] == body["items"]
    forbidden = await other.get(
        "/api/v1/tasks", params={"scope": "all", "cursor": body["next_cursor"]}
    )
    assert forbidden.status_code == 422
    assert forbidden.headers["cache-control"] == "no-store"
    assert forbidden.json()["error_code"] == "task_history_cursor_invalid"
    assert "background_failed_count" not in forbidden.json()
    assert body["next_cursor"] not in forbidden.text
    own = await other.get("/api/v1/tasks", params={"scope": "all"})
    assert len(own.json()["items"]) == 1
    assert own.json()["background_failed_count"] == 1
    assert await counts() == before


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"scope": "invalid-synthetic"}, "filter"),
        ({"kind": "invalid-synthetic"}, "filter"),
        ({"kind": "sync_mail"}, "filter"),
        ({"status": "invalid-synthetic"}, "filter"),
        ({"created_from_date": "20290101"}, "filter"),
        ({"created_to_date": "2029-02-30"}, "filter"),
        ({"created_from_date": "2029-01-02", "created_to_date": "2029-01-01"}, "filter"),
        ({"limit": "01"}, "filter"),
        ({"cursor": "invalid-synthetic"}, "cursor"),
        ({"cursor": "x" * 2049}, "cursor"),
        ({"cursor": ""}, "cursor"),
    ],
)
async def test_invalid_queries_have_safe_stable_problems(
    history_client: HistoryClient, params: dict[str, str], code: str
) -> None:
    """所有输入错误保持固定422与no-store，不回显原始过滤或游标值。"""
    owner, _, _, _ = history_client
    response = await owner.get("/api/v1/tasks", params=params)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error_code"] == f"task_history_{code}_invalid"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "invalid-synthetic" not in response.text
