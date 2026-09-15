"""用实际PG等待图和NOWAIT验证可信claim/request-start不会先锁本地动作再等待User。"""

import asyncio
from typing import Literal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from ai_employee.application.ports.trusted_actions import RequestStartDisposition
from ai_employee.infrastructure.db.models.actions import CalendarChangeProposalModel, MailDraftModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepository,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.integrations.registry import ProviderAdapterRegistry
from tests.integration.m2.test_tool_execution_claim import (
    ACTION_CIPHER,
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
    _RecordingAdapter,
    _Seed,
    _seed_action,
    _seed_calendar_action,
    _workflow,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]


@pytest.mark.parametrize("kind", ("mail", "calendar"))
@pytest.mark.parametrize("phase", ("claim", "request-start"))
async def test_trusted_user_lock_precedes_local_action_lock(
    database_url: str,
    kind: Literal["mail", "calendar"],
    phase: str,
) -> None:
    """活动用户下只证明锁序反转，不把既有active守卫误报为缺失。

    独立短事务先持User；真实claim读取或request-start等待它时，用PG阻塞图确认等待。
    User持有者此时应能取得本地动作锁，代表另一个user→local事务不会形成锁环；旧顺序
    会因提前持有local产生55P03。savepoint只隔离NOWAIT探针，原执行随后正常继续。
    """
    seed, sessions = _Seed(), build_session_factory(database_url)
    payload_hash = await (
        _seed_action(database_url, seed)
        if kind == "mail"
        else _seed_calendar_action(database_url, seed)
    )
    adapter = _RecordingAdapter()
    registry = ProviderAdapterRegistry(
        google_mail_action=adapter if kind == "mail" else None,
        google_calendar_action=adapter if kind == "calendar" else None,
    )
    workflow = _workflow(database_url, adapter, registry=registry)
    pending: asyncio.Task[bool] | None = None
    writer_started = asyncio.Event()
    writer_pid: int | None = None
    local_lock_failure: tuple[str, str | None] | None = None
    try:
        if phase == "request-start":
            await workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        async with sessions.begin() as session:
            dispatch = (
                await SqlAlchemyTrustedActionRepository(session, ACTION_CIPHER).load_dispatch(
                    task_id=seed.task_id,
                    approval_id=seed.approval_id,
                    operation_id=seed.operation_id,
                )
                if phase == "request-start"
                else None
            )

        async def run_writer() -> bool:
            """执行完整生产锁序；只有应用级最终授权使用内容无关的合成许可。"""
            nonlocal writer_pid
            async with sessions.begin() as session:
                writer_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                writer_started.set()
                repository = SqlAlchemyTrustedActionRepository(session, ACTION_CIPHER)
                if phase == "claim":
                    snapshot = await repository.lock_execution(
                        task_id=seed.task_id,
                        approval_id=seed.approval_id,
                        operation_id=seed.operation_id,
                    )
                    return snapshot is not None and snapshot.user_is_active
                assert dispatch is not None
                result = await repository.mark_request_started(
                    snapshot=dispatch,
                    lease_owner=seed.owner,
                    authorize=lambda _facts: None,
                )
                return result.disposition is RequestStartDisposition.STARTED

        async with sessions.begin() as holder:
            assert (
                await holder.scalar(
                    select(UserModel.id)
                    .where(
                        UserModel.id == seed.user_id,
                    )
                    .with_for_update()
                )
                == seed.user_id
            )
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(run_writer())
            await asyncio.wait_for(writer_started.wait(), timeout=5)

            async def wait_for_user_block() -> None:
                """只查询两个已知合成会话的真实等待边，避免sleep猜测并发阶段。"""
                while True:
                    async with sessions() as observer:
                        if await observer.scalar(
                            text("SELECT :holder_pid = ANY(pg_blocking_pids(:writer_pid))"),
                            {"holder_pid": holder_pid, "writer_pid": writer_pid},
                        ):
                            return

            await asyncio.wait_for(wait_for_user_block(), timeout=5)
            local_model = MailDraftModel if kind == "mail" else CalendarChangeProposalModel
            try:
                async with holder.begin_nested():
                    assert (
                        await holder.scalar(
                            select(local_model.id)
                            .where(
                                local_model.id == seed.draft_id,
                            )
                            .with_for_update(nowait=True)
                        )
                        == seed.draft_id
                    )
            except DBAPIError as error:
                # SQL和参数不进入pytest输出；只记录锁冲突类型与标准SQLSTATE。
                local_lock_failure = (type(error).__name__, getattr(error.orig, "sqlstate", None))

        assert await asyncio.wait_for(pending, timeout=5)
        assert local_lock_failure is None, f"local action locked before User: {local_lock_failure}"
        assert adapter.write_calls == adapter.reconcile_calls == 0
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await workflow.dispose()
        await sessions.dispose()
