"""执行已注册的每日简报 Graph，并将结果持久化。"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ai_employee.agents.daily_brief.graph import build_daily_brief_graph
from ai_employee.application.use_cases.briefs import PersistDailyBriefUseCase
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.briefs import DailyBriefContent
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import CalendarEventModel, EmailThreadModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.integrations.llm.fake import build_model_gateway


class GenerateBriefTaskStep:
    """从 PostgreSQL 读取本人来源，在事务外运行 Graph 后原子保存结果。"""
    name = "daily_brief"

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存共享 session factory，避免 Graph 网络调用持有数据库事务。"""
        self._session_factory = session_factory

    async def execute(self, task: LeasedTask) -> None:
        """生成当日简报；无可用来源由 Graph 写稳定失败，不伪造成功。"""
        if task.user_id is None:
            raise ValueError("daily_brief requires user_id")
        async with self._session_factory() as session:
            user = await session.get(UserModel, task.user_id)
            if user is None:
                raise ValueError("daily_brief user not found")
            local_date = self._local_date(task.input_payload.get("local_date"), user.timezone)
            cutoff = datetime.now(UTC)
            threads = tuple((await session.scalars(select(EmailThreadModel).where(EmailThreadModel.user_id == task.user_id, EmailThreadModel.latest_message_at <= cutoff))).all())
            events = tuple((await session.scalars(select(CalendarEventModel).where(CalendarEventModel.user_id == task.user_id, CalendarEventModel.starts_at.is_not(None), CalendarEventModel.ends_at.is_not(None), CalendarEventModel.starts_at < cutoff))).all())
        result = await build_daily_brief_graph().ainvoke({"task_run_id": str(task.task_id), "local_date": local_date.isoformat(), "source_cutoff": cutoff.isoformat(), "mail_threads": [{"thread_id": str(thread.id), "subject": thread.subject, "latest_message_at": thread.latest_message_at.isoformat()} for thread in threads], "calendar_events": [{"event_id": str(event.id), "start_at": event.starts_at.isoformat(), "end_at": event.ends_at.isoformat(), "status": event.status, "transparency": event.transparency, "all_day": event.all_day} for event in events if event.starts_at and event.ends_at], "model_gateway": build_model_gateway(), "model_name": "fake", "locale": user.locale})
        content = DailyBriefContent.model_validate(result["content"])
        markdown = "\n".join([f"# {content.headline}", *(f"- {item.title}" for item in content.items)])
        await PersistDailyBriefUseCase(self._session_factory).execute(user_id=task.user_id, task_id=task.task_id, content=content, markdown=markdown)

    @staticmethod
    def _local_date(raw: object, timezone: str) -> date:
        """优先使用任务中的明确本地日期，否则显式转换用户时区。"""
        return date.fromisoformat(raw) if isinstance(raw, str) else datetime.now(UTC).astimezone(ZoneInfo(timezone)).date()


def build_generate_brief_task_step(*, session_factory: ManagedAsyncSessionMaker) -> GenerateBriefTaskStep:
    """构造供 DurableTaskRunner 使用的实际 daily_brief 节点。"""
    return GenerateBriefTaskStep(session_factory)
