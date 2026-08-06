"""执行每日简报 Graph，并在来源新鲜度边界持久化可审计结果。"""

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ai_employee.agents.daily_brief.graph import build_daily_brief_graph
from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.application.ports.model import ModelGateway
from ai_employee.application.use_cases.briefs import PersistDailyBriefUseCase
from ai_employee.application.use_cases.sync_calendar import CalendarConnectionNotFoundError
from ai_employee.application.use_cases.sync_gmail import GmailConnectionNotFoundError
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.domain.briefs import DailyBriefContent
from ai_employee.domain.errors import TransientProviderError, UserActionRequiredError
from ai_employee.infrastructure.db.models.briefs import DailyBriefModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    EmailAnalysisModel,
    EmailMessageModel,
    EmailThreadModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.briefs import (
    SqlAlchemyDailyBriefPersistenceStoreFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import Metrics
from ai_employee.infrastructure.testing.scenarios import consume_test_scenario
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
        model_gateway_factory: Callable[[UUID], ModelGateway] | None = None,
        model_name: str = "fake",
        sync_source: SyncSource | None = None,
        now: UtcNow | None = None,
        checkpoint_database_url: str | None = None,
    ) -> None:
        """注入可替换模型、同步步骤和 UTC 时钟，避免 Graph I/O 占用数据库事务。"""
        self._session_factory = session_factory
        self._model_gateway = model_gateway
        self._model_gateway_factory = model_gateway_factory
        self._model_name = model_name
        self._sync_source = sync_source
        self._now = now or (lambda: datetime.now(UTC))
        self._checkpoint_database_url = checkpoint_database_url

    async def execute(self, task: LeasedTask) -> None:
        """生成当日简报；无可用来源由 Graph 写稳定失败，不伪造成功。"""
        if task.user_id is None:
            raise ValueError("daily_brief requires user_id")
        # 重放任务已有简报时必须在任何 Graph/模型调用前返回，避免遗漏调用审计。
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(DailyBriefModel.id).where(
                    DailyBriefModel.user_id == task.user_id,
                    DailyBriefModel.task_id == task.task_id,
                )
            )
        if existing is not None:
            return
        cutoff = self._cutoff(task.input_payload.get("source_cutoff"))
        connection_id = self._connection_scope(task.input_payload.get("connection_id"))
        async with self._session_factory() as session:
            user = await session.get(UserModel, task.user_id)
            if user is None:
                raise ValueError("daily_brief user not found")
            local_date = self._local_date(
                task.input_payload.get("local_date"), user.timezone, cutoff
            )
            stale = await self._stale_resources(session, task.user_id, cutoff, connection_id)
        warnings = await self._refresh_stale_sources(task.user_id, stale)
        async with self._session_factory() as session:
            user = await session.get(UserModel, task.user_id)
            if user is None:
                raise ValueError("daily_brief user not found")
            mail_threads = await self._mail_threads_for_local_day(
                session, task.user_id, local_date, user.timezone, cutoff, connection_id
            )
            calendar_events = await self._events_for_local_day(
                session, task.user_id, local_date, user.timezone, connection_id
            )
        model_gateway = self._model_gateway or (
            self._model_gateway_factory(task.user_id)
            if self._model_gateway_factory is not None
            else build_model_gateway()
        )
        graph_input = {
            "task_run_id": str(task.task_id),
            "local_date": local_date.isoformat(),
            "source_cutoff": cutoff.isoformat(),
            "mail_threads": mail_threads,
            "calendar_events": calendar_events,
            "warnings": warnings,
            "model_name": self._model_name,
            "locale": user.locale,
        }
        # 模型端口由 Graph 节点闭包持有，不能混入 checkpoint 状态；任务 ID 则是跨 Worker
        # 接管不变的 thread_id。纯单元测试仍可不提供 PostgreSQL 保存器。
        if self._checkpoint_database_url is None:
            result = await build_daily_brief_graph(model_gateway=model_gateway).ainvoke(graph_input)
        else:
            async with postgres_checkpointer(self._checkpoint_database_url) as saver:
                result = await build_daily_brief_graph(
                    model_gateway=model_gateway, checkpointer=saver
                ).ainvoke(
                    graph_input,
                    {"configurable": {"thread_id": str(task.task_id)}},
                )
        content = DailyBriefContent.model_validate(result["content"])
        markdown = "\n".join(
            [f"# {content.headline}", *(f"- {item.title}" for item in content.items)]
        )
        await PersistDailyBriefUseCase(
            SqlAlchemyDailyBriefPersistenceStoreFactory(self._session_factory)
        ).execute(
            user_id=task.user_id,
            task_id=task.task_id,
            content=content,
            markdown=markdown,
            email_analyses=tuple(self._email_analyses(result)),
            model_invocations=tuple(result.get("model_invocations", [])),
        )

    async def _stale_resources(
        self, session: Any, user_id: UUID, cutoff: datetime, connection_id: UUID | None
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
                *(
                    (OAuthConnectionModel.id == connection_id,)
                    if connection_id is not None
                    else ()
                ),
            )
        )
        stale: list[tuple[str, UUID]] = []
        found: set[tuple[UUID, str]] = set()
        for row_connection_id, resource_kind, cursor, last_success_at in rows:
            if resource_kind in {"gmail", "calendar"}:
                found.add((row_connection_id, resource_kind))
                if (
                    cursor is None
                    or last_success_at is None
                    or last_success_at < cutoff - SYNC_FRESHNESS
                ):
                    stale.append((resource_kind, row_connection_id))
        for (row_connection_id,) in (
            await session.execute(
                select(OAuthConnectionModel.id).where(
                    OAuthConnectionModel.user_id == user_id,
                    OAuthConnectionModel.provider == "google",
                    OAuthConnectionModel.status == "connected",
                    *(
                        (OAuthConnectionModel.id == connection_id,)
                        if connection_id is not None
                        else ()
                    ),
                )
            )
        ).all():
            for resource_kind in ("gmail", "calendar"):
                if (row_connection_id, resource_kind) not in found:
                    stale.append((resource_kind, row_connection_id))
        return tuple(stale)

    async def _refresh_stale_sources(
        self, user_id: UUID, stale: tuple[tuple[str, UUID], ...]
    ) -> list[str]:
        """逐资源刷新，单一只读同步失败仅降级本次简报而不取消其他来源。

        告警从 PostgreSQL 读取最后成功时刻而非使用 Worker 内存；进程崩溃或另一 Worker
        接管后仍能向用户说明缺什么、数据新鲜度及可执行的修复动作。
        """
        if self._sync_source is None:
            return []
        warnings: list[str] = []
        for resource_kind, connection_id in stale:
            try:
                await self._sync_source(resource_kind, connection_id, user_id)
            except (
                TransientProviderError,
                UserActionRequiredError,
                GmailConnectionNotFoundError,
                CalendarConnectionNotFoundError,
            ) as error:
                last_success = await self._last_source_success(
                    user_id=user_id,
                    connection_id=connection_id,
                    resource_kind=resource_kind,
                )
                repair = "reconnect" if isinstance(error, UserActionRequiredError) else "retry"
                rendered_success = last_success.isoformat() if last_success is not None else "never"
                warnings.append(
                    f"missing:{resource_kind};last_success:{rendered_success};repair:{repair}"
                )
        return warnings

    async def _last_source_success(
        self, *, user_id: UUID, connection_id: UUID, resource_kind: str
    ) -> datetime | None:
        """读取当前用户连接的最后成功同步时刻，不把跨用户游标泄露到简报。"""
        async with self._session_factory() as session:
            return await session.scalar(
                select(SyncCursorModel.last_success_at)
                .join(OAuthConnectionModel, OAuthConnectionModel.id == SyncCursorModel.connection_id)
                .where(
                    OAuthConnectionModel.user_id == user_id,
                    SyncCursorModel.connection_id == connection_id,
                    SyncCursorModel.resource_kind == resource_kind,
                )
            )

    async def _mail_threads_for_local_day(
        self,
        session: Any,
        user_id: UUID,
        local_date: date,
        timezone: str,
        cutoff: datetime,
        connection_id: UUID | None,
    ) -> list[dict[str, object]]:
        """按邮件 received_at 的用户本地日去重线程，绝不以线程更新时间替代接收日期。"""
        start, end = self._day_bounds(local_date, timezone)
        upper = min(end, cutoff)
        messages = (
            await session.scalars(
                select(EmailMessageModel).join(
                    EmailThreadModel, EmailThreadModel.id == EmailMessageModel.thread_id
                )
                .where(
                    EmailMessageModel.user_id == user_id,
                    EmailMessageModel.received_at >= start,
                    EmailMessageModel.received_at < upper,
                    *(
                        (EmailThreadModel.connection_id == connection_id,)
                        if connection_id is not None
                        else ()
                    ),
                )
                .order_by(EmailMessageModel.received_at.desc())
            )
        ).all()
        deterministic_facts: dict[UUID, EmailAnalysisModel] = {}
        for analysis in (
            await session.scalars(
                select(EmailAnalysisModel)
                .where(
                    EmailAnalysisModel.user_id == user_id,
                    EmailAnalysisModel.model_name == "deterministic",
                )
                .order_by(EmailAnalysisModel.created_at.desc())
            )
        ).all():
            # 只采用同一线程最新的规则结果。模型输出不能覆盖 Graph 的可信确定性事实。
            deterministic_facts.setdefault(analysis.thread_id, analysis)
        result: list[dict[str, object]] = []
        seen: set[UUID] = set()
        for message in messages:
            if message.thread_id in seen:
                continue
            thread = await session.scalar(
                select(EmailThreadModel).where(
                    EmailThreadModel.id == message.thread_id,
                    EmailThreadModel.user_id == user_id,
                    *((EmailThreadModel.connection_id == connection_id,)
                      if connection_id is not None else ()),
                )
            )
            if thread is None:
                continue
            seen.add(message.thread_id)
            facts = deterministic_facts.get(thread.id)
            result.append(
                {
                    "thread_id": str(thread.id),
                    "subject": message.subject,
                    "sender": message.sender.get("email", ""),
                    "labels": message.labels,
                    "headers": message.headers,
                    "summary": message.snippet,
                    "provider_url": message.provider_url,
                    "needs_reply": facts.needs_reply if facts is not None else False,
                    "deadline_at": facts.deadline_at if facts is not None else None,
                }
            )
        return result

    async def _events_for_local_day(
        self,
        session: Any,
        user_id: UUID,
        local_date: date,
        timezone: str,
        connection_id: UUID | None,
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
                    *(
                        (CalendarEventModel.connection_id == connection_id,)
                        if connection_id is not None
                        else ()
                    ),
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
                    "needs_reply": bool(item.get("needs_reply", False)),
                    "deadline_at": item.get("deadline_at"),
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
                    "model_name": result.get("model_name", ""),
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
    def _local_date(raw: object, timezone: str, cutoff: datetime) -> date:
        """优先使用任务中的明确本地日期，否则显式转换用户时区。"""
        return (
            date.fromisoformat(raw)
            if isinstance(raw, str)
            else cutoff.astimezone(ZoneInfo(timezone)).date()
        )

    def _cutoff(self, raw: object) -> datetime:
        """使用任务冻结的 source cutoff；缺失时只在入口读取一次当前 UTC。"""
        if isinstance(raw, str):
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("source_cutoff must be timezone-aware")
            return parsed.astimezone(UTC)
        return self._utc_now()

    @staticmethod
    def _connection_scope(raw: object) -> UUID | None:
        """解析可选的单连接测试范围，拒绝不稳定值以免悄悄回退到全量来源。"""
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise TypeError("daily_brief connection_id must be a UUID string")
        return UUID(raw)


def build_generate_brief_task_step(
    *,
    session_factory: ManagedAsyncSessionMaker,
    settings: Settings | None = None,
    metrics: Metrics | None = None,
) -> GenerateBriefTaskStep:
    """构造供 DurableTaskRunner 使用的实际 daily_brief 节点。"""
    if settings is None:
        return GenerateBriefTaskStep(session_factory)

    async def sync_source(resource_kind: str, connection_id: UUID, user_id: UUID) -> None:
        """复用既有只读同步 Worker，连接 ID 仅在本进程内传递且不进入模型输入。"""
        from ai_employee.workers.sync_calendar import build_calendar_sync_task_step
        from ai_employee.workers.sync_gmail import build_gmail_sync_task_step

        step = (
            build_gmail_sync_task_step(
                session_factory=session_factory, settings=settings, metrics=metrics
            )
            if resource_kind == "gmail"
            else build_calendar_sync_task_step(
                session_factory=session_factory, settings=settings, metrics=metrics
            )
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

    model_gateway_factory: Callable[[UUID], ModelGateway] | None = None
    if settings.app_test_mode:
        model_gateway_factory = lambda user_id: build_model_gateway(
            settings,
            metrics=metrics,
            scenario_consumer=lambda current_user_id: consume_test_scenario(
                redis_url=settings.redis_url, user_id=current_user_id
            ),
            user_id=user_id,
        )
    return GenerateBriefTaskStep(
        session_factory,
        model_gateway=None
        if settings.app_test_mode
        else build_model_gateway(settings, metrics=metrics),
        model_gateway_factory=model_gateway_factory,
        model_name=settings.model_name,
        sync_source=sync_source,
        checkpoint_database_url=settings.checkpoint_database_url,
    )
