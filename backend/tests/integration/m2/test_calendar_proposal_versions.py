"""在真实 PostgreSQL 上验证恢复提案的事务外读取与原子版本事实。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select

from ai_employee.application.ports.calendar import CalendarEvent, CalendarReader
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
)
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.workers.prepare_calendar_restore import (
    CalendarRestoreReaderResolver,
    PrepareCalendarRestoreTaskStep,
)

USER_ID = UUID("00000000-0000-0000-0000-000000000931")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000932")
SOURCE_PROPOSAL_ID = UUID("00000000-0000-0000-0000-000000000933")
SOURCE_DESIRED_ID = UUID("00000000-0000-0000-0000-000000000934")
SOURCE_BEFORE_ID = UUID("00000000-0000-0000-0000-000000000935")
CALENDAR_ID = "synthetic-calendar"
PROVIDER_EVENT_ID = "synthetic-provider-event"
NOW = datetime(2030, 3, 11, 8, tzinfo=UTC)
RETAIN_UNTIL = NOW + timedelta(days=365)
ACTION_CIPHER = ActionPayloadCipher.from_key(b"r" * 32)


@dataclass(slots=True)
class _TransactionProbe:
    """跟踪当前测试 Engine 上尚未结束的真实数据库事务数。"""

    active: int = 0

    def began(self, *_: object) -> None:
        """记录 SQLAlchemy connection BEGIN。"""
        self.active += 1

    def ended(self, *_: object) -> None:
        """记录 COMMIT/ROLLBACK，并拒绝计数下溢。"""
        self.active -= 1
        assert self.active >= 0


class _AssertingReader:
    """只实现恢复允许的精确 GET，并断言调用时数据库事务已释放。"""

    def __init__(self, probe: _TransactionProbe, current: CalendarEvent) -> None:
        self._probe = probe
        self._current = current
        self.calls: list[tuple[str, str]] = []

    async def get_current_event(
        self, calendar_id: str, provider_event_id: str
    ) -> CalendarEvent | None:
        """返回合成当前事件；任何仍打开的事务都会令测试立即失败。"""
        assert self._probe.active == 0
        self.calls.append((calendar_id, provider_event_id))
        return self._current


class _ReaderResolver(CalendarRestoreReaderResolver):
    """返回单一 provider-neutral reader，不读取凭据或访问网络。"""

    def __init__(self, reader: CalendarReader) -> None:
        self._reader = reader
        self.calls: list[tuple[UUID, UUID, str, str]] = []

    async def resolve(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        provider: str,
        timezone: str,
    ) -> CalendarReader:
        """记录不含 token 的解析参数并返回合成 reader。"""
        self.calls.append((user_id, connection_id, provider, timezone))
        return self._reader


def _current_provider_event() -> CalendarEvent:
    """构造与历史 before 不同、可编辑且带当前 ETag 的供应商事实。"""
    return CalendarEvent(
        event_id=PROVIDER_EVENT_ID,
        calendar_id=CALENDAR_ID,
        title="Current title",
        description="Current description",
        location="Current room",
        starts_at=datetime(2030, 3, 11, 12, tzinfo=UTC),
        ends_at=datetime(2030, 3, 11, 13, tzinfo=UTC),
        all_day=False,
        transparency="opaque",
        status="confirmed",
        timezone="UTC",
        recurring_event_id=None,
        etag='W/"etag-current"',
        provider_url="https://calendar.example.test/event",
        attendees=({"email": "person@example.test"},),
        can_edit=True,
    )


async def _seed_restore_source(session_factory) -> tuple[UUID, UUID]:  # type: ignore[no-untyped-def]
    """保存用户、读写能力、可写目录、历史提案/before 与 RUNNING 恢复任务。"""
    task_id = uuid4()
    async with session_factory.begin() as session:
        session.add(
            UserModel(
                id=USER_ID,
                email="calendar-restore@example.test",
                display_name="Calendar restore",
                password_hash=None,
                timezone="UTC",
                locale="en-US",
                brief_time=time(8, 0),
                is_active=True,
                default_calendar_connection_id=CONNECTION_ID,
                default_calendar_id=CALENDAR_ID,
            )
        )
        session.add(
            OAuthConnectionModel(
                id=CONNECTION_ID,
                user_id=USER_ID,
                provider="google",
                provider_account_id="calendar-restore-account",
                account_email="calendar-restore@example.test",
                scopes=[],
                status="connected",
            )
        )
        session.add_all(
            ConnectionCapabilityModel(
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
                capability=capability,
                status="enabled",
                actual_scopes=[],
            )
            for capability in ("calendar.read", "calendar.write")
        )
        session.add(
            ProviderCalendarModel(
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
                provider_calendar_id=CALENDAR_ID,
                name="Synthetic calendar",
                timezone="UTC",
                is_primary=True,
                access_role="owner",
                can_write=True,
                provider_url="https://calendar.example.test/calendar",
            )
        )
        repository = SqlAlchemyCalendarProposalRepository(session, ACTION_CIPHER)
        await repository.create(
            proposal_id=SOURCE_PROPOSAL_ID,
            snapshot_id=SOURCE_DESIRED_ID,
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            creation_idempotency_key="restore-source",
            creation_payload_hash="a" * 64,
            calendar_id=CALENDAR_ID,
            operation_kind="update",
            target_event_id=PROVIDER_EVENT_ID,
            base_etag='W/"etag-old"',
            retain_until=RETAIN_UNTIL,
            desired_state={
                "operation_id": str(uuid4()),
                "title": "Changed title",
                "description": "Changed description",
                "location": "Changed room",
                "starts_at": "2030-03-11T10:00:00+00:00",
                "ends_at": "2030-03-11T11:00:00+00:00",
                "timezone": "UTC",
                "all_day": False,
                "attendees": ["person@example.test"],
                "notification_policy": "all",
                "changed_fields": ["location"],
                "confirmed_fields": [],
                "source_event_ids": [],
                "notification_policy_user_set": False,
                "availability": None,
            },
        )
        await repository.save_snapshot(
            snapshot_id=SOURCE_BEFORE_ID,
            user_id=USER_ID,
            proposal_id=SOURCE_PROPOSAL_ID,
            version=1,
            snapshot_kind="before",
            content={
                "operation_id": str(uuid4()),
                "title": "Historical title",
                "description": "Historical description",
                "location": "Historical room",
                "starts_at": "2030-03-11T09:00:00+00:00",
                "ends_at": "2030-03-11T10:00:00+00:00",
                "timezone": "UTC",
                "all_day": False,
                "attendees": ["person@example.test"],
                "notification_policy": "none",
                "changed_fields": [],
                "confirmed_fields": [],
                "source_event_ids": [],
                "notification_policy_user_set": False,
                "availability": None,
            },
            retain_until=RETAIN_UNTIL,
        )
        session.add(
            TaskRunModel(
                id=task_id,
                user_id=USER_ID,
                kind="calendar.restore.prepare",
                status=TaskStatus.RUNNING.value,
                lease_owner="calendar-restore-worker",
                idempotency_key=f"calendar-restore-task:{task_id}",
                input_payload={
                    "source_snapshot_id": str(SOURCE_BEFORE_ID),
                    "creation_idempotency_key": f"calendar-restore:{task_id}",
                },
                started_at=NOW,
            )
        )
    return task_id, SOURCE_BEFORE_ID


def _install_probe(engine, probe: _TransactionProbe) -> Callable[[], None]:  # type: ignore[no-untyped-def]
    """安装事务事件监听并返回精确移除回调。"""
    sync_engine = engine.sync_engine
    event.listen(sync_engine, "begin", probe.began)
    event.listen(sync_engine, "commit", probe.ended)
    event.listen(sync_engine, "rollback", probe.ended)

    def remove() -> None:
        """移除本测试注册的三个监听器，避免污染后续用例。"""
        event.remove(sync_engine, "begin", probe.began)
        event.remove(sync_engine, "commit", probe.ended)
        event.remove(sync_engine, "rollback", probe.ended)

    return remove


@pytest.mark.asyncio
async def test_restore_provider_read_is_outside_transaction_and_result_is_atomic(
    database_url: str,
) -> None:
    """供应商 GET 位于两个短事务之间，新提案、ETag、before 与任务 marker 同时提交。"""
    session_factory = build_session_factory(database_url)
    probe = _TransactionProbe()
    remove_probe = _install_probe(session_factory.engine, probe)
    try:
        task_id, source_snapshot_id = await _seed_restore_source(session_factory)
        assert probe.active == 0
        reader = _AssertingReader(probe, _current_provider_event())
        resolver = _ReaderResolver(reader)
        step = PrepareCalendarRestoreTaskStep(
            session_factory,
            action_cipher=ACTION_CIPHER,
            reader_resolver=resolver,
            clock=lambda: NOW,
        )

        await step.execute(
            LeasedTask(
                task_id=task_id,
                user_id=USER_ID,
                kind="calendar.restore.prepare",
                input_payload={
                    "source_snapshot_id": str(source_snapshot_id),
                    "creation_idempotency_key": f"calendar-restore:{task_id}",
                },
                started_at=NOW,
                lease_owner="calendar-restore-worker",
            )
        )

        assert reader.calls == [(CALENDAR_ID, PROVIDER_EVENT_ID)]
        assert resolver.calls == [(USER_ID, CONNECTION_ID, "google", "UTC")]
        assert probe.active == 0
        async with session_factory() as session:
            proposals = tuple(
                (
                    await session.scalars(
                        select(CalendarChangeProposalModel).order_by(
                            CalendarChangeProposalModel.created_at
                        )
                    )
                ).all()
            )
            restored = proposals[-1]
            task = await session.get(TaskRunModel, task_id)
            snapshots = tuple(
                (
                    await session.scalars(
                        select(CalendarChangeSnapshotModel).where(
                            CalendarChangeSnapshotModel.proposal_id == restored.id
                        )
                    )
                ).all()
            )
            repository = SqlAlchemyCalendarProposalRepository(session, ACTION_CIPHER)
            restored_view = await repository.get_current(
                user_id=USER_ID,
                proposal_id=restored.id,
            )
        assert len(proposals) == 2
        assert restored.operation_kind == "restore"
        assert restored.base_etag == 'W/"etag-current"'
        assert {snapshot.snapshot_kind for snapshot in snapshots} == {"desired", "before"}
        assert restored_view is not None
        assert restored_view.before_snapshot_id is not None
        assert restored_view.desired_snapshot.content["location"] == "Historical room"
        assert task is not None
        assert task.result_payload == {"calendar_proposal_id": str(restored.id)}
    finally:
        remove_probe()
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_restore_rolls_back_proposal_when_before_snapshot_persistence_fails(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二事务任一写入失败时，新提案与任务 marker 必须一起回滚。"""
    session_factory = build_session_factory(database_url)
    try:
        task_id, source_snapshot_id = await _seed_restore_source(session_factory)
        reader = _AssertingReader(_TransactionProbe(), _current_provider_event())
        original_save = SqlAlchemyCalendarProposalRepository.save_snapshot

        async def fail_new_restore_before(
            repository: SqlAlchemyCalendarProposalRepository,
            **kwargs: object,
        ) -> object:
            """只在新恢复提案的 before 写入点注入合成崩溃。"""
            if kwargs.get("proposal_id") != SOURCE_PROPOSAL_ID:
                raise RuntimeError("synthetic restore snapshot failure")
            return await original_save(repository, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            SqlAlchemyCalendarProposalRepository,
            "save_snapshot",
            fail_new_restore_before,
        )
        step = PrepareCalendarRestoreTaskStep(
            session_factory,
            action_cipher=ACTION_CIPHER,
            reader_resolver=_ReaderResolver(reader),
            clock=lambda: NOW,
        )

        with pytest.raises(RuntimeError, match="synthetic restore snapshot failure"):
            await step.execute(
                LeasedTask(
                    task_id=task_id,
                    user_id=USER_ID,
                    kind="calendar.restore.prepare",
                    input_payload={
                        "source_snapshot_id": str(source_snapshot_id),
                        "creation_idempotency_key": f"calendar-restore:{task_id}",
                    },
                    started_at=NOW,
                    lease_owner="calendar-restore-worker",
                )
            )

        async with session_factory() as session:
            proposal_count = await session.scalar(
                select(func.count()).select_from(CalendarChangeProposalModel)
            )
            task = await session.get(TaskRunModel, task_id)
        assert proposal_count == 1
        assert task is not None and task.result_payload is None
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_restore_uses_persisted_task_input_instead_of_message_copy(
    database_url: str,
) -> None:
    """队列消息中的伪造创建键不能覆盖 PostgreSQL 任务已绑定的恢复身份。"""
    session_factory = build_session_factory(database_url)
    try:
        task_id, source_snapshot_id = await _seed_restore_source(session_factory)
        reader = _AssertingReader(_TransactionProbe(), _current_provider_event())
        step = PrepareCalendarRestoreTaskStep(
            session_factory,
            action_cipher=ACTION_CIPHER,
            reader_resolver=_ReaderResolver(reader),
            clock=lambda: NOW,
        )

        await step.execute(
            LeasedTask(
                task_id=task_id,
                user_id=USER_ID,
                kind="calendar.restore.prepare",
                input_payload={
                    "source_snapshot_id": str(source_snapshot_id),
                    "creation_idempotency_key": "forged-message-creation-key",
                },
                started_at=NOW,
                lease_owner="calendar-restore-worker",
            )
        )

        async with session_factory() as session:
            restored = await session.scalar(
                select(CalendarChangeProposalModel)
                .where(CalendarChangeProposalModel.operation_kind == "restore")
                .order_by(CalendarChangeProposalModel.created_at.desc())
            )
        assert restored is not None
        assert restored.creation_idempotency_key == f"calendar-restore:{task_id}"
    finally:
        await session_factory.dispose()
