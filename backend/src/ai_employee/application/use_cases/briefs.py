"""协调每日简报结果的版本化持久化。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text

from ai_employee.domain.briefs import DailyBriefContent
from ai_employee.infrastructure.db.models.briefs import (
    DailyBriefItemModel,
    DailyBriefModel,
    LLMInvocationModel,
)
from ai_employee.infrastructure.db.models.sources import EmailAnalysisModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class PersistDailyBriefUseCase:
    """在一个事务中持久化简报、条目和可审计完成事件。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """注入受调用方拥有的数据库 session factory。"""
        self._session_factory = session_factory

    async def execute(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        content: DailyBriefContent,
        markdown: str,
        email_analyses: tuple[dict[str, Any], ...] = (),
        model_invocations: tuple[dict[str, Any], ...] = (),
    ) -> UUID:
        """写入下一版本的 complete/partial 简报和所有顺序条目。

        手动刷新与重复 worker 恢复由调用方传入同一个 task_id；同一任务再次执行会复用
        已写入简报，防止至少一次投递创建第二个版本。
        """
        async with self._session_factory.begin() as session:
            # PostgreSQL advisory transaction lock serializes version allocation for one user/date
            # without locking unrelated users or days. The unique constraints remain the final guard.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:brief_key))"),
                {"brief_key": f"daily-brief:{user_id}:{content.local_date.isoformat()}"},
            )
            existing = await session.scalar(
                select(DailyBriefModel.id).where(
                    DailyBriefModel.user_id == user_id, DailyBriefModel.task_id == task_id
                )
            )
            if existing is not None:
                return existing
            current = await session.scalar(
                select(func.max(DailyBriefModel.version)).where(
                    DailyBriefModel.user_id == user_id,
                    DailyBriefModel.local_date == content.local_date,
                )
            )
            brief = DailyBriefModel(
                user_id=user_id,
                local_date=content.local_date,
                version=(current or 0) + 1,
                task_id=task_id,
                completeness=content.completeness,
                source_cutoff=content.source_cutoff,
                headline=content.headline,
                structured_content=content.model_dump(mode="json"),
                markdown=markdown,
                warnings=content.warnings,
                created_at=datetime.now(UTC),
            )
            session.add(brief)
            await session.flush()
            session.add_all(
                DailyBriefItemModel(
                    brief_id=brief.id,
                    position=index,
                    section=item.section.value,
                    priority=item.priority.value,
                    title=item.title,
                    body_markdown=item.body_markdown,
                    source_refs=[source.model_dump(mode="json") for source in item.source_refs],
                    suggested_action_kind=item.suggested_action_kind,
                )
                for index, item in enumerate(content.items)
            )
            for analysis in email_analyses:
                session.add(
                    EmailAnalysisModel(
                        user_id=user_id,
                        thread_id=UUID(str(analysis["thread_id"])),
                        category=str(analysis["category"]),
                        urgency=str(analysis["urgency"]),
                        needs_reply=bool(analysis.get("needs_reply", False)),
                        deadline_at=analysis.get("deadline_at"),
                        confidence=float(analysis["confidence"]),
                        reason_codes=list(analysis.get("reason_codes", [])),
                        model_name=analysis.get("model_name"),
                        prompt_version=analysis.get("prompt_version"),
                        input_hash=str(analysis["input_hash"]),
                        created_at=datetime.now(UTC),
                    )
                )
            for invocation in model_invocations:
                session.add(
                    LLMInvocationModel(
                        user_id=user_id,
                        task_id=task_id,
                        step_id=None,
                        provider=str(invocation["provider"]),
                        model_name=str(invocation["model_name"]),
                        prompt_version=str(invocation["prompt_version"]),
                        input_hash=str(invocation["input_hash"]),
                        output_schema=str(invocation["output_schema"]),
                        input_tokens=int(invocation["input_tokens"]),
                        output_tokens=int(invocation["output_tokens"]),
                        estimated_cost_microusd=invocation.get("estimated_cost_microusd"),
                        latency_ms=int(invocation["latency_ms"]),
                        status=str(invocation["status"]),
                        error_code=invocation.get("error_code"),
                        created_at=datetime.now(UTC),
                    )
                )
            task = await session.get(TaskRunModel, task_id, with_for_update=True)
            if task is not None and task.user_id == user_id:
                task.result_payload = {
                    "brief_id": str(brief.id),
                    "completeness": content.completeness,
                }
            session.add(
                AuditEventModel(
                    user_id=user_id,
                    task_id=task_id,
                    event_type="brief.ready",
                    actor_type="system",
                    actor_id=None,
                    event_metadata={
                        "brief_id": str(brief.id),
                        "completeness": content.completeness,
                    },
                )
            )
            return brief.id
