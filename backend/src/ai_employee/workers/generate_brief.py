"""执行每日简报 Graph，并在来源新鲜度边界持久化可审计结果。"""

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ai_employee.agents.daily_brief.graph import build_daily_brief_graph
from ai_employee.application.ports.model import ModelGateway
from ai_employee.application.use_cases.briefs import PersistDailyBriefUseCase
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.briefs import DailyBriefContent
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    EmailMessageModel,
    EmailThreadModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.integrations.llm.fake import build_model_gateway

SyncSource = Callable[[str, UUID, UUID], Awaitable[None]]
UtcNow = Callable[[], datetime]
SYNC_FRESHNESS = timedelta(minutes=15)


class GenerateBriefTaskStep:
    """按用户本地日和已验证游标读取来源，在事务外运行 Graph 后原子保存结果。"""

    name = "daily_brief"

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        *,
        model_gateway: ModelGateway | None = None,
        sync_source: SyncSource | None = None,
        now: UtcNow | None = None,
    ) -> None:
        """注入可替换模型、同步步骤和 UTC 时钟，避免 Graph I/O 占用数据库事务。"""
        self._session_factory = session_factory
        self._model_gateway = model_gateway
        self._sync_source = sync_source
        self._now = now or (lambda: datetime.now(UTC))

    async def execute(self, task: LeasedTask) -> None:
        """生成当日简报；无可用来源由 Graph 写稳定失败，不伪造成功。"""
        if task.user_id is None:
            raise ValueError("daily_brief requires user_id")
        cutoff = self._utc_now()
        async with self._session_factory() as session:
            user = await session.get(UserModel, task.user_id)
            if user is None:
                raise ValueError("daily_brief user not found")
            local_date = self._local_date(task.input_payload.get("local_date"), user.timezone)
            stale = await self._stale_resources(session, task.user_id, cutoff)
        warnings = await self._refresh_stale_sources(task.user_id, stale)
        async with self._session_factory() as session:
            user = await session.get(UserModel, task.user_id)
            if user is None:
                raise ValueError("daily_brief user not found")
            mail_threads = await self._mail_threads_for_local_day(
                session, task.user_id, local_date, user.timezone
            )
            calendar_events = await self._events_for_local_day(
                session, task.user_id, local_date, user.timezone
            )
        result = await build_daily_brief_graph().ainvoke(
            {
                "task_run_id": str(task.task_id),
                "local_date": local_date.isoformat(),
                "source_cutoff": cutoff.isoformat(),
                "mail_threads": mail_threads,
                "calendar_events": calendar_events,
                "warnings": warnings,
                "model_gateway": self._model_gateway or build_model_gateway(),
                "model_name": "fake",
                "locale": user.locale,
            }
        )
        content = DailyBriefContent.model_validate(result["content"])
        markdown = "\n".join(
            [f"# {content.headline}", *(f"- {item.title}" for item in content.items)]
        )
        await PersistDailyBriefUseCase(self._session_factory).execute(
            user_id=task.user_id,
            task_id=task.task_id,
            content=content,
            markdown=markdown,
            email_analyses=tuple(self._email_analyses(result)),
            model_invocations=tuple(result.get("model_invocations", [])),
        )

    async def _stale_resources(
        self, session: Any, user_id: UUID, cutoff: datetime
    ) -> tuple[tuple[str, UUID], ...]:
        """返回缺失或超过十五分钟未成功同步的已连接资源，始终带用户归属过滤。"""
        rows = await session.execute(
            select(
                OAuthConnectionModel.id,
                SyncCursorModel.resource_kind,
                SyncCursorModel.cursor,
                SyncCursorModel.last_success_at,
            )
            .outerjoin(SyncCursorModel, OAuthConnectionModel.id == SyncCursorModel.connection_id)
            .where(
                OAuthConnectionModel.user_id == user_id,
                OAuthConnectionModel.provider == "google",
                OAuthConnectionModel.status == "connected",
            )
        )
        stale: list[tuple[str, UUID]] = []
        found: set[tuple[UUID, str]] = set()
        for connection_id, resource_kind, cursor, last_success_at in rows:
            if resource_kind in {"gmail", "calendar"}:
                found.add((connection_id, resource_kind))
                if (
                    cursor is None
                    or last_success_at is None
                    or last_success_at < cutoff - SYNC_FRESHNESS
                ):
                    stale.append((resource_kind, connection_id))
        for (connection_id,) in (
            await session.execute(
                select(OAuthConnectionModel.id).where(
                    OAuthConnectionModel.user_id == user_id,
                    OAuthConnectionModel.provider == "google",
                    OAuthConnectionModel.status == "connected",
                )
            )
        ).all():
            for resource_kind in ("gmail", "calendar"):
                if (connection_id, resource_kind) not in found:
                    stale.append((resource_kind, connection_id))
        return tuple(stale)

    async def _refresh_stale_sources(
        self, user_id: UUID, stale: tuple[tuple[str, UUID], ...]
    ) -> list[str]:
        """逐资源刷新，单一只读同步失败仅降级本次简报而不取消其他来源。"""
        if self._sync_source is None:
            return []
        warnings: list[str] = []
        for resource_kind, connection_id in stale:
            try:
                await self._sync_source(resource_kind, connection_id, user_id)
            except Exception:  # noqa: BLE001 - Worker 将单一来源错误降级为可审计 partial。
                warnings.append(f"source_sync_failed:{resource_kind}")
        return warnings

    async def _mail_threads_for_local_day(
        self, session: Any, user_id: UUID, local_date: date, timezone: str
    ) -> list[dict[str, object]]:
        """按邮件 received_at 的用户本地日去重线程，绝不以线程更新时间替代接收日期。"""
        start, end = self._day_bounds(local_date, timezone)
        messages = (
            await session.scalars(
                select(EmailMessageModel)
                .where(
                    EmailMessageModel.user_id == user_id,
                    EmailMessageModel.received_at >= start,
                    EmailMessageModel.received_at < end,
                )
                .order_by(EmailMessageModel.received_at.desc())
            )
        ).all()
        result: list[dict[str, object]] = []
        seen: set[UUID] = set()
        for message in messages:
            if message.thread_id in seen:
                continue
            thread = await session.scalar(
                select(EmailThreadModel).where(
                    EmailThreadModel.id == message.thread_id, EmailThreadModel.user_id == user_id
                )
            )
            if thread is None:
                continue
            seen.add(message.thread_id)
            result.append(
                {
                    "thread_id": str(thread.id),
                    "subject": message.subject,
                    "sender": message.sender.get("email", ""),
                    "labels": message.labels,
                    "headers": message.headers,
                    "summary": message.snippet,
                    "provider_url": message.provider_url,
                }
            )
        return result

    async def _events_for_local_day(
        self, session: Any, user_id: UUID, local_date: date, timezone: str
    ) -> list[dict[str, object]]:
        """选择与用户本地日发生任何重叠的日程，包括当天稍后才开始的事件。"""
        start, end = self._day_bounds(local_date, timezone)
        events = (
            await session.scalars(
                select(CalendarEventModel).where(
                    CalendarEventModel.user_id == user_id,
                    CalendarEventModel.starts_at.is_not(None),
                    CalendarEventModel.ends_at.is_not(None),
                    CalendarEventModel.starts_at < end,
                    CalendarEventModel.ends_at > start,
                )
            )
        ).all()
        return [
            {
                "event_id": str(event.id),
                "start_at": event.starts_at.isoformat(),
                "end_at": event.ends_at.isoformat(),
                "status": event.status,
                "transparency": event.transparency,
                "all_day": event.all_day,
                "provider_url": event.provider_url,
            }
            for event in events
            if event.starts_at is not None and event.ends_at is not None
        ]

    @staticmethod
    def _day_bounds(local_date: date, timezone: str) -> tuple[datetime, datetime]:
        """从 IANA 时区构造本地日的两个 UTC 边界，覆盖跨日和夏令时偏移。"""
        zone = ZoneInfo(timezone)
        return (
            datetime.combine(local_date, time.min, tzinfo=zone).astimezone(UTC),
            datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC),
        )

    @staticmethod
    def _email_analyses(result: dict[str, Any]) -> list[dict[str, object]]:
        """规范化规则和模型判断为可持久化字段，规则结果显式记录版本化 ruleset。"""
        analyses: list[dict[str, object]] = []
        for item in result.get("classifications", []):
            analyses.append(
                {
                    **item,
                    "needs_reply": False,
                    "confidence": 1.0,
                    "model_name": "deterministic",
                    "prompt_version": "email_rules_v1",
                    "input_hash": sha256(str(item["thread_id"]).encode()).hexdigest(),
                }
            )
        for item in result.get("model_items", []):
            analyses.append(
                {
                    **item,
                    "model_name": "fake",
                    "prompt_version": "daily_brief_v1",
                    "input_hash": sha256(str(item["thread_id"]).encode()).hexdigest(),
                }
            )
        return analyses

    def _utc_now(self) -> datetime:
        """验证注入时钟始终返回带时区 UTC，防止宿主机本地时间参与过期判断。"""
        current = self._now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("daily brief clock must be timezone-aware")
        return current.astimezone(UTC)

    @staticmethod
    def _local_date(raw: object, timezone: str) -> date:
        """优先使用任务中的明确本地日期，否则显式转换用户时区。"""
        return (
            date.fromisoformat(raw)
            if isinstance(raw, str)
            else datetime.now(UTC).astimezone(ZoneInfo(timezone)).date()
        )


def build_generate_brief_task_step(
    *, session_factory: ManagedAsyncSessionMaker, settings: Settings | None = None
) -> GenerateBriefTaskStep:
    """构造供 DurableTaskRunner 使用的实际 daily_brief 节点。"""
    if settings is None:
        return GenerateBriefTaskStep(session_factory)

    async def sync_source(resource_kind: str, connection_id: UUID, user_id: UUID) -> None:
        """复用既有只读同步 Worker，连接 ID 仅在本进程内传递且不进入模型输入。"""
        from ai_employee.workers.sync_calendar import build_calendar_sync_task_step
        from ai_employee.workers.sync_gmail import build_gmail_sync_task_step

        step = (
            build_gmail_sync_task_step(session_factory=session_factory, settings=settings)
            if resource_kind == "gmail"
            else build_calendar_sync_task_step(session_factory=session_factory, settings=settings)
        )
        await step.execute(
            LeasedTask(
                task_id=UUID(int=0),
                user_id=user_id,
                kind=f"sync_{resource_kind}",
                input_payload={"connection_id": str(connection_id)},
                started_at=datetime.now(UTC),
            )
        )

    return GenerateBriefTaskStep(
        session_factory,
        model_gateway=build_model_gateway(settings),
        sync_source=sync_source,
    )
