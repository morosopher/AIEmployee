"""验证 REST 任务快照公开 PostgreSQL 审计游标。"""

from uuid import uuid4

from ai_employee.api.routers.tasks import _task_response
from ai_employee.application.use_cases.task_views import TaskSnapshot
from ai_employee.domain.tasks import TaskStatus


def test_task_response_exposes_only_snapshot_event_cursor() -> None:
    """REST 快照必须公开最大审计游标，以建立 SSE 重放基线。"""
    response = _task_response(
        TaskSnapshot(
            id=uuid4(),
            kind="daily_brief",
            status=TaskStatus.SUCCEEDED,
            retry_of_task_id=None,
            input_payload={"internal": "must_not_leak"},
            error_code=None,
            event_cursor=8,
            steps=(),
        )
    )

    assert response.event_cursor == 8
    assert "input_payload" not in response.model_dump()
