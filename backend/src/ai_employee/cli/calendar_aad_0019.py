"""无参数的 0019 exact-pair 恢复 one-off；只分派一次 planner 返回的任务集合。

使用 PostgreSQL Outbox 与原 DurableTaskRunner，队列适配器在本进程仅接收允许的 ID。
不启动 Taskiq、不访问 Redis、不消费未被本次 planner 选中的任务，也不自动分配新 ordinal。
"""

import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from httpx import HTTPError
from sqlalchemy.exc import SQLAlchemyError

from ai_employee.application.ports.encryption import Encryption
from ai_employee.application.ports.oauth_refresh import OAuthRefreshCoordinator
from ai_employee.application.use_cases.calendar_aad_preflight import CalendarAadAdapters
from ai_employee.application.use_cases.calendar_aad_recovery import (
    CalendarAadPlannedTask,
    CalendarAadRecoveryUseCase,
)
from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadBinding,
    CalendarAadRolloutError,
)
from ai_employee.application.use_cases.outbox import OutboxRelay
from ai_employee.cli.calendar_aad_preflight_0019 import (
    require_no_rollout_arguments,
    require_sealed_settings,
    rollout_environment,
)
from ai_employee.config import Settings
from ai_employee.domain.errors import DomainError
from ai_employee.infrastructure.calendar_aad_resources import await_calendar_aad_resource
from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
    CalendarAadArtifactFile,
    CalendarAadCurrentGuard,
    SqlAlchemyCalendarAadPreflightRepository,
    async_calendar_aad_rollout_lease,
)
from ai_employee.infrastructure.db.repositories.calendar_aad_recovery import (
    SqlAlchemyCalendarAadRecoveryStoreFactory,
)
from ai_employee.infrastructure.db.repositories.outbox import SqlAlchemyOutboxStore
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.integrations.registry import build_oauth_security_services
from ai_employee.workers.execute_task import RETRY_DELAY_SECONDS, build_task_runner_for_session
from ai_employee.workers.sync_calendar import CalendarAadReadAdapters, CalendarAadRecoveryTaskStep


@dataclass(frozen=True, slots=True)
class CalendarAadRecoveryResult:
    """仅向 CLI 暴露计数、digest、ordinal 与有效截止线，不包含 cursor/原始 scope。"""

    planned: tuple[CalendarAadPlannedTask, ...]
    remaining_markers: int
    effective_deadline: datetime | None


class _RecoveryOnlyEnqueuer:
    """精确任务 Outbox 的进程内端口；不执行节点、不与通用队列通信。"""

    def __init__(self, planned: tuple[CalendarAadPlannedTask, ...]) -> None:
        """冻结唯一允许接收的 planner 结果集。"""
        self._allowed = frozenset(item.task_id for item in planned)

    async def enqueue(
        self, task_id: UUID, *, resume: str | None = None, recover_approval_checkpoint: bool = False
    ) -> None:
        """拒绝其它 ID 与审批控制字段；实际执行在 Outbox 确认提交之后串行进行。"""
        if task_id not in self._allowed or resume is not None or recover_approval_checkpoint:
            raise CalendarAadRolloutError("calendar_aad_recovery_input_invalid")


async def run_recovery(
    *,
    sessions: ManagedAsyncSessionMaker,
    cipher: Encryption,
    coordinator: OAuthRefreshCoordinator,
    adapters: CalendarAadAdapters,
    backup_directory: Path,
    basename: str,
    immutable_image_id: str,
    clock: Callable[[], datetime],
    settings: Settings,
) -> CalendarAadRecoveryResult:
    """持 revision lease 验证 artifact，规划一次、逐项精确分派并运行原 durable runner。

    每个 ID 最多运行一次投递；尚未到期的 retry Outbox 不被提前执行。失败后本调用不再
    规划；后续显式调用复用活动 ordinal，只有 failed/cancelled 允许 planner 分配下一个。
    """
    require_sealed_settings(settings)
    binding = CalendarAadBinding(basename, immutable_image_id)
    async with (
        asyncio.timeout(settings.task_timeout_seconds),
        async_calendar_aad_rollout_lease(sessions.engine.url) as lease,
    ):
        artifact_file = CalendarAadArtifactFile(backup_directory, binding)
        # 与 preflight 共用同一受保护读取，取消后先收回文件线程再释放 revision lease。
        artifact = await await_calendar_aad_resource(
            asyncio.create_task(asyncio.to_thread(artifact_file.read))
        )
        guard = CalendarAadCurrentGuard(
            repository=SqlAlchemyCalendarAadPreflightRepository(sessions),
            artifact_file=artifact_file,
            artifact=artifact,
            lease=lease,
            clock=clock,
            expected_revision="20260809_0019",
        )
        await guard.verify()
        stores = SqlAlchemyCalendarAadRecoveryStoreFactory(sessions)
        planned = await CalendarAadRecoveryUseCase(
            stores=stores, guard=guard, artifact=artifact
        ).plan()
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(sessions),
            enqueuer=_RecoveryOnlyEnqueuer(planned),
            clock=clock,
            claim_ttl=timedelta(seconds=settings.task_lease_seconds),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(minutes=5),
        )
        step = CalendarAadRecoveryTaskStep(
            sessions=sessions,
            cipher=cipher,
            coordinator=coordinator,
            adapters=adapters,
            guard=guard,
            clock=clock,
        )
        runner = build_task_runner_for_session(
            sessions, settings=settings, calendar_aad_step=step, clock=clock
        )
        for item in planned:
            await guard.verify()
            await relay.dispatch(item.task_id)
            await runner.run(item.task_id, retry_delay=timedelta(seconds=RETRY_DELAY_SECONDS))
        deadline = await guard.verify()
        async with stores() as store:
            remaining = len(await store.marked_pairs())
        return CalendarAadRecoveryResult(planned, remaining, deadline)


async def _run(
    settings: Settings, directory: Path, binding: CalendarAadBinding
) -> CalendarAadRecoveryResult:
    """复用 same-Secret OAuth 组合根，调用方拥有 session factory 并在退出时释放。"""
    sessions = build_session_factory(settings.database_url)
    try:
        security = await asyncio.to_thread(
            build_oauth_security_services,
            session_factory=sessions,
            master_key_file=settings.app_master_key_file,
        )
        clock = lambda: datetime.now(UTC)
        return await run_recovery(
            sessions=sessions,
            cipher=security.cipher,
            coordinator=security.coordinator,
            adapters=CalendarAadReadAdapters(settings, clock=clock),
            backup_directory=directory,
            basename=binding.basename,
            immutable_image_id=binding.immutable_image_id,
            clock=clock,
            settings=settings,
        )
    finally:
        await sessions.dispose()


def main(arguments: Sequence[str] | None = None) -> int:
    """只输出内容无关结果；任何剩余 marker 均返回非零，不把部分恢复标记成成功。"""
    try:
        require_no_rollout_arguments(arguments)
        directory, binding = rollout_environment()
        settings = Settings()
        require_sealed_settings(settings)
        result = asyncio.run(_run(settings, directory, binding))
    except (DomainError, SQLAlchemyError, HTTPError, OSError, ValueError, RuntimeError) as error:
        print(
            error.error_code if isinstance(error, DomainError) else "calendar_aad_rollout_failed",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "planned_count": len(result.planned),
                "remaining_markers": result.remaining_markers,
                "pairs": [
                    {
                        "pair_digest": item.pair_digest,
                        "recovery_attempt_ordinal": item.recovery_attempt_ordinal,
                    }
                    for item in result.planned
                ],
                "effective_deadline": result.effective_deadline.isoformat()
                if result.effective_deadline
                else None,
                "result_code": "calendar_aad_recovery_passed"
                if not result.remaining_markers
                else "calendar_aad_recovery_incomplete",
            },
            sort_keys=True,
        )
    )
    return 1 if result.remaining_markers else 0


if __name__ == "__main__":
    raise SystemExit(main())
