"""验证简报 REST 资源的真实授权、版本和来源投影。"""

from datetime import UTC, date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from ai_employee.infrastructure.db.models.briefs import DailyBriefItemModel, DailyBriefModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

from .conftest import AuthenticatedApiClients


async def _insert_brief(
    session_factory: ManagedAsyncSessionMaker, *, user_id: UUID, local_date: date, version: int
) -> UUID:
    """直接准备已完成版本，确保 REST 测试只验证真实读取与授权边界。"""
    async with session_factory.begin() as session:
        task = TaskRunModel(
            user_id=user_id,
            kind="daily_brief",
            status="succeeded",
            idempotency_key=f"api-brief:{user_id}:{local_date}:{version}",
            input_payload={},
        )
        session.add(task)
        await session.flush()
        brief = DailyBriefModel(
            user_id=user_id,
            local_date=local_date,
            version=version,
            task_id=task.id,
            completeness="complete",
            source_cutoff=datetime.now(UTC),
            headline=f"Synthetic brief {version}",
            structured_content={},
            markdown=f"# Synthetic brief {version}",
            warnings=[],
            created_at=datetime.now(UTC),
        )
        session.add(brief)
        await session.flush()
        session.add(
            DailyBriefItemModel(
                brief_id=brief.id,
                position=0,
                section="attention",
                priority="high",
                title="Synthetic source-backed item",
                body_markdown="No personal content.",
                source_refs=[{"source_type": "email_thread", "source_id": "synthetic-thread"}],
                suggested_action_kind="create_task",
            )
        )
        return brief.id


@pytest.mark.asyncio
async def test_generate_today_history_and_owned_source_refs(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """手动生成返回 202；今日和历史始终给出最新版本及最小来源引用。"""
    clients = authenticated_api_clients
    local_today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()
    first_id = await _insert_brief(clients.session_factory, user_id=clients.owner_id, local_date=local_today, version=1)
    second_id = await _insert_brief(clients.session_factory, user_id=clients.owner_id, local_date=local_today, version=2)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    created = await clients.owner.post("/api/v1/briefs/generate", headers={"X-CSRF-Token": csrf})
    today = await clients.owner.get("/api/v1/briefs/today")
    history = await clients.owner.get("/api/v1/briefs", params={"local_date": local_today.isoformat()})
    detail = await clients.owner.get(f"/api/v1/briefs/{second_id}")
    assert created.status_code == 202 and UUID(created.json()["task_id"])
    assert today.status_code == 200 and today.json()["id"] == str(second_id)
    assert history.status_code == 200 and [item["id"] for item in history.json()] == [str(second_id), str(first_id)]
    assert detail.json()["items"] == [
        {
            "position": 0,
            "section": "attention",
            "priority": "high",
            "title": "Synthetic source-backed item",
            "body_markdown": "No personal content.",
            "source_refs": [{"source_type": "email_thread", "source_id": "synthetic-thread"}],
            "suggested_action_kind": "create_task",
        }
    ]


@pytest.mark.asyncio
async def test_foreign_brief_is_not_readable(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """跨用户简报详情必须统一隐藏为 404，阻止资源枚举。"""
    clients = authenticated_api_clients
    brief_id = await _insert_brief(
        clients.session_factory,
        user_id=clients.other_id,
        local_date=date(2026, 8, 4),
        version=1,
    )
    response = await clients.owner.get(f"/api/v1/briefs/{brief_id}")
    assert response.status_code == 404
    assert response.json()["error_code"] == "brief_not_found"
