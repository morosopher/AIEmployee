"""任务历史HTTP边界的认证、规范整数与精度契约；不访问数据库。"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest

from ai_employee.api.routers import tasks
from ai_employee.application.task_history import TaskHistoryItem, TaskHistoryPage
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.security.task_history_cursor import TaskHistoryCursorCodec
from ai_employee.main import create_app


async def test_anonymous_history_is_no_store_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺失Cookie时先拒绝认证，不读取分页Secret或打开历史查询。"""

    def forbidden_secret(*args: object) -> None:
        """认证前访问密钥属于越界，即使部署配置存在也必须显式失败。"""
        raise AssertionError("unexpected_secret_access")

    monkeypatch.setattr(TaskHistoryCursorCodec, "from_file", forbidden_secret)
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        response = await client.get("/api/v1/tasks")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "limit",
    ["0", "101", "01", "+1", " 1", "1.0", "１", "1\n", pytest.param("9" * 5000, id="oversized")],
)
def test_noncanonical_limit_is_safe_problem(limit: str) -> None:
    """不能让宽松整数转换或Python超长整数异常绕过统一422。"""
    with pytest.raises(tasks.ApiProblem) as caught:
        tasks.parse_task_history_query(limit=limit)
    assert caught.value.status_code == 422
    assert caught.value.error_code == "task_history_filter_invalid"


def test_summary_serialization_preserves_microseconds_and_nulls() -> None:
    """显式白名单只暴露九字段，并将非UTC时间精确序列化为六位微秒Z。"""
    instant = datetime(2029, 1, 1, 20, 0, 0, 123456, timezone(timedelta(hours=8)))
    page = TaskHistoryPage(
        (
            TaskHistoryItem(
                UUID(int=1),
                "daily_brief",
                "business",
                TaskStatus.FAILED,
                instant,
                instant,
                None,
                "synthetic_failure",
                None,
            ),
        ),
        None,
        None,
        datetime(2029, 1, 1, tzinfo=UTC),
        "Asia/Shanghai",
        0,
    )
    result = tasks.task_history_response(page).model_dump(mode="json")
    assert result["server_time"] == "2029-01-01T00:00:00.000000Z"
    assert result["items"] == [
        {
            "id": str(UUID(int=1)),
            "kind": "daily_brief",
            "category": "business",
            "status": "failed",
            "created_at": "2029-01-01T12:00:00.123456Z",
            "started_at": "2029-01-01T12:00:00.123456Z",
            "finished_at": None,
            "error_code": "synthetic_failure",
            "retry_of_task_id": None,
        }
    ]
