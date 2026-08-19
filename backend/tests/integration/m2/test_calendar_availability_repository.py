"""验证日历可用性 adapter 的最小投影、短事务与版本 CAS。"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, update

from ai_employee.application.use_cases.calendar_proposals import (
    CalendarProposalContent,
    CalendarProposalUseCase,
)
from ai_employee.domain.calendar_actions import NotificationPolicy
from ai_employee.domain.calendar_availability import AvailabilityResult, suggest_meeting_times
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.database_url import (
    TestDatabaseUrl as ValidatedTestDatabaseUrl,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.calendar import (
    SqlAlchemyCalendarSyncRepository,
)
from ai_employee.infrastructure.db.repositories.calendar_availability import (
    SqlAlchemyCalendarAvailabilityRepository,
)
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher

NOW = datetime(2030, 3, 11, 8, tzinfo=UTC)
FUTURE_SEARCH_START = datetime(2030, 4, 1, 9, tzinfo=UTC)
PAST_SEARCH_START = datetime(2030, 2, 1, 9, tzinfo=UTC)
ACTION_CIPHER = ActionPayloadCipher.from_key(b"v" * 32)

# 本模块复用 Cycle 5 的 disposable regular 数据库，并确保异常退出时先释放所有 pool。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """只使用已通过 typed lifecycle 建立的 synthetic PostgreSQL 17 数据库。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """覆盖全局迁移 fixture；Cycle 5 helper 已完成 head/catalog 验证。"""
    del cycle5_regular_database_url
    yield


@dataclass(frozen=True, slots=True)
class _SeededAvailabilityUser:
    """记录一个用户的 proposal 与连接身份，避免测试重新读取 ORM 对象。"""

    user_id: UUID
    proposal_id: UUID
    connection_ids: tuple[UUID, ...]
    calendar_ids: tuple[str, ...]


@dataclass(slots=True)
class _TransactionProbe:
    """统计当前引擎上的活动事务，供纯函数边界作同步断言。"""

    active: int = 0
    maximum: int = 0

    def began(self, *_: object) -> None:
        """记录事务开始；嵌套或重叠会提高 maximum。"""
        self.active += 1
        self.maximum = max(self.maximum, self.active)

    def ended(self, *_: object) -> None:
        """记录 commit/rollback，并拒绝监听器失配造成的负数。"""
        self.active -= 1
        assert self.active >= 0


async def _seed_availability_user(
    session_factory: ManagedAsyncSessionMaker,
    *,
    connection_count: int,
    cursor_success_at: datetime,
    event_count: int,
) -> _SeededAvailabilityUser:
    """以 synthetic 数据建立 proposal、目录、游标和可批量读取事件。

    Args:
        session_factory: 当前 disposable PostgreSQL 会话工厂。
        connection_count: 相关 calendar 连接数量，至少一项。
        cursor_success_at: directory/calendar 两类游标的成功时刻。
        event_count: 第一个连接上写入的符合窗口事件数量。

    Returns:
        只含 UUID/opaque ID 的冻结测试身份。
    """
    assert connection_count >= 1
    user_id = uuid4()
    proposal_id = uuid4()
    snapshot_id = uuid4()
    connection_ids = tuple(uuid4() for _ in range(connection_count))
    calendar_ids = tuple(f"synthetic-calendar-{index}" for index in range(connection_count))
    content = CalendarProposalContent(
        operation_id=uuid4(),
        title="Synthetic availability proposal",
        starts_at="2030-03-11T09:00:00+00:00",
        ends_at="2030-03-11T09:30:00+00:00",
        timezone="UTC",
        all_day=False,
        notification_policy=NotificationPolicy.NONE,
        changed_fields=("ends_at", "starts_at", "title"),
        confirmed_fields=("calendar", "time", "attendees", "notification_policy"),
    )
    async with session_factory.begin() as session:
        session.add(
            UserModel(
                id=user_id,
                email=f"availability-{user_id}@example.test",
                display_name="Synthetic availability user",
                password_hash=None,
                timezone="UTC",
                locale="en-US",
                brief_time=time(8, 0),
                is_active=True,
                meeting_buffer_minutes=10,
                workspace_history_retention_days=365,
            )
        )
        session.add_all(
            OAuthConnectionModel(
                id=connection_id,
                user_id=user_id,
                provider="google",
                provider_account_id=f"availability-{connection_id}",
                account_email=f"availability-{connection_id}@example.test",
                scopes=[],
                status="connected",
            )
            for connection_id in connection_ids
        )
        await session.flush()
        for connection_id, calendar_id in zip(connection_ids, calendar_ids, strict=True):
            session.add(
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability="calendar.read",
                    status="enabled",
                    actual_scopes=[],
                    last_verified_at=cursor_success_at,
                    last_error_code=None,
                )
            )
            session.add(
                ProviderCalendarModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    provider_calendar_id=calendar_id,
                    name="Synthetic calendar",
                    timezone="UTC",
                    is_primary=True,
                    access_role="owner",
                    can_write=True,
                    provider_url="https://calendar.example.test/synthetic",
                )
            )
            session.add_all(
                (
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        scope_key="directory",
                        cursor="synthetic-directory-cursor",
                        last_success_at=cursor_success_at,
                        last_attempt_at=cursor_success_at,
                        last_error_code=None,
                    ),
                    SyncCursorModel(
                        connection_id=connection_id,
                        resource_kind="calendar",
                        scope_key=calendar_id,
                        cursor="synthetic-event-cursor",
                        last_success_at=cursor_success_at,
                        last_attempt_at=cursor_success_at,
                        last_error_code=None,
                    ),
                )
            )
        await SqlAlchemyCalendarProposalRepository(session, ACTION_CIPHER).create(
            proposal_id=proposal_id,
            snapshot_id=snapshot_id,
            user_id=user_id,
            connection_id=connection_ids[0],
            creation_idempotency_key=f"availability-proposal:{proposal_id}",
            creation_payload_hash="a" * 64,
            calendar_id=calendar_ids[0],
            operation_kind="create",
            target_event_id=None,
            base_etag=None,
            retain_until=NOW + timedelta(days=365),
            desired_state=cast(Mapping[str, object], content.model_dump(mode="json")),
        )

        # 使用 Core executemany 避免 10k+ ORM identity-map 对象；每行仍是完整 synthetic
        # CalendarEvent，读取测试只允许五列跨越 adapter 边界。
        if event_count:
            event_values = []
            for index in range(event_count):
                starts_at = NOW + timedelta(hours=1, minutes=index % 30)
                event_values.append(
                    {
                        "id": uuid4(),
                        "user_id": user_id,
                        "connection_id": connection_ids[0],
                        "provider_event_id": f"synthetic-event-{index}",
                        "calendar_id": calendar_ids[0],
                        "title": f"Sensitive title {index}",
                        "description_ciphertext": None,
                        "description_nonce": None,
                        "description_key_version": None,
                        "description_aad_version": None,
                        "location_ciphertext": None,
                        "location_nonce": None,
                        "location_key_version": None,
                        "location_aad_version": None,
                        "starts_at": starts_at,
                        "ends_at": starts_at + timedelta(minutes=15),
                        "all_day": False,
                        "transparency": "opaque",
                        "status": "confirmed",
                        "timezone": "UTC",
                        "recurring_event_id": None,
                        "etag": f'W/"synthetic-{index}"',
                        "organizer": {"email": "organizer@example.test"},
                        "attendees": [{"email": "attendee@example.test"}],
                        "access_role": "owner",
                        "can_edit": True,
                        "provider_url": "https://calendar.example.test/event",
                        "provider_updated_at": NOW,
                    }
                )
            await session.execute(CalendarEventModel.__table__.insert(), event_values)
    return _SeededAvailabilityUser(
        user_id=user_id,
        proposal_id=proposal_id,
        connection_ids=connection_ids,
        calendar_ids=calendar_ids,
    )


def _select_list(statement: str) -> str:
    """返回规范小写 SQL 的 SELECT 列表，供字段级泄漏断言。"""
    normalized = " ".join(statement.lower().split())
    return normalized.split(" from ", maxsplit=1)[0]


@pytest.mark.asyncio
async def test_reader_uses_observed_at_for_future_and_past_search_freshness(
    database_url: ValidatedTestDatabaseUrl,
) -> None:
    """future/past 搜索都只能用 observed_at 计算十五分钟 freshness cutoff。"""
    sessions = build_session_factory(database_url)
    try:
        seeded = await _seed_availability_user(
            sessions,
            connection_count=1,
            cursor_success_at=NOW - timedelta(minutes=10),
            event_count=0,
        )
        repository = SqlAlchemyCalendarAvailabilityRepository(sessions, ACTION_CIPHER)
        future = await repository.load_suggestion(
            user_id=seeded.user_id,
            proposal_id=seeded.proposal_id,
            observed_at=NOW,
            search_start=FUTURE_SEARCH_START,
            horizon_days=14,
        )
        assert future is not None
        assert future.context.missing_connection_ids == ()

        async with sessions.begin() as session:
            await session.execute(
                update(SyncCursorModel)
                .where(SyncCursorModel.connection_id.in_(seeded.connection_ids))
                .values(last_success_at=NOW - timedelta(minutes=16))
            )
        past = await repository.load_suggestion(
            user_id=seeded.user_id,
            proposal_id=seeded.proposal_id,
            observed_at=NOW,
            search_start=PAST_SEARCH_START,
            horizon_days=14,
        )
        assert past is not None
        assert past.context.missing_connection_ids == seeded.connection_ids
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_sync_repository_requires_independent_observed_at(
    database_url: ValidatedTestDatabaseUrl,
) -> None:
    """底层 availability reader 不得再用 search_start 隐式充当 freshness 时钟。"""
    sessions = build_session_factory(database_url)
    try:
        seeded = await _seed_availability_user(
            sessions,
            connection_count=1,
            cursor_success_at=NOW,
            event_count=0,
        )
        async with sessions() as session:
            with pytest.raises(TypeError):
                await SqlAlchemyCalendarSyncRepository(session).get_availability_context(
                    user_id=seeded.user_id,
                    search_start=FUTURE_SEARCH_START,
                    horizon_days=14,
                )
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_reader_has_fixed_query_count_minimal_projection_and_user_isolation(
    database_url: ValidatedTestDatabaseUrl,
) -> None:
    """1/32 连接使用同样 SELECT 数，事件不截断且不读取任何敏感内容列。"""
    sessions = build_session_factory(database_url)
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """只记录 adapter 运行期 SELECT；seed INSERT 不参与查询预算。"""
        if statement.lstrip().lower().startswith("select"):
            statements.append(statement)

    try:
        one = await _seed_availability_user(
            sessions,
            connection_count=1,
            cursor_success_at=NOW,
            event_count=1,
        )
        many = await _seed_availability_user(
            sessions,
            connection_count=32,
            cursor_success_at=NOW,
            event_count=10_001,
        )
        repository = SqlAlchemyCalendarAvailabilityRepository(sessions, ACTION_CIPHER)
        event.listen(sessions.engine.sync_engine, "before_cursor_execute", capture)
        first = await repository.load_suggestion(
            user_id=one.user_id,
            proposal_id=one.proposal_id,
            observed_at=NOW,
            search_start=NOW,
            horizon_days=14,
        )
        one_statements = tuple(statements)
        statements.clear()
        loaded = await repository.load_suggestion(
            user_id=many.user_id,
            proposal_id=many.proposal_id,
            observed_at=NOW,
            search_start=NOW,
            horizon_days=14,
        )
        many_statements = tuple(statements)
        statements.clear()
        cross_user = await repository.load_suggestion(
            user_id=one.user_id,
            proposal_id=many.proposal_id,
            observed_at=NOW,
            search_start=NOW,
            horizon_days=14,
        )

        assert first is not None
        assert loaded is not None
        assert cross_user is None
        assert len(one_statements) == len(many_statements)
        assert len(loaded.context.events) == 10_001

        event_selects = [sql for sql in many_statements if "from calendar_events" in sql.lower()]
        assert len(event_selects) == 1
        event_select = event_selects[0]
        event_projection = _select_list(event_select)
        assert all(
            column in event_projection
            for column in (
                "calendar_events.starts_at",
                "calendar_events.ends_at",
                "calendar_events.all_day",
                "calendar_events.transparency",
                "calendar_events.status",
            )
        )
        assert all(
            forbidden not in event_projection
            for forbidden in (
                "calendar_events.title",
                "description_ciphertext",
                "description_nonce",
                "description_key_version",
                "description_aad_version",
                "location_ciphertext",
                "location_nonce",
                "location_key_version",
                "location_aad_version",
                "calendar_events.organizer",
                "calendar_events.attendees",
                "calendar_events.etag",
                "calendar_events.provider_url",
            )
        )
        assert " limit " not in " ".join(event_select.lower().split())

        directory_select = next(
            sql for sql in many_statements if "from provider_calendars" in sql.lower()
        )
        assert all(
            forbidden not in _select_list(directory_select)
            for forbidden in (".name", ".timezone", ".access_role", ".provider_url")
        )
        cursor_select = next(
            sql for sql in many_statements if "from sync_cursors" in sql.lower()
        )
        assert "sync_cursors.cursor" not in _select_list(cursor_select)
    finally:
        if event.contains(sessions.engine.sync_engine, "before_cursor_execute", capture):
            event.remove(sessions.engine.sync_engine, "before_cursor_execute", capture)
        await sessions.dispose()


class _ForbiddenLegacyPort:
    """若 suggest_times 回退到 caller-owned legacy repository，测试立即失败。"""

    def __getattr__(self, name: str) -> object:
        """任何属性访问都表示窄 availability port 未被使用。"""
        raise AssertionError(f"legacy calendar proposal port was accessed: {name}")


class _RacingAvailabilityRepository(SqlAlchemyCalendarAvailabilityRepository):
    """在真实短读与短写之间提交一个并发版本，用于验证 expected_version CAS。"""

    def __init__(
        self,
        sessions: ManagedAsyncSessionMaker,
        cipher: ActionPayloadCipher,
    ) -> None:
        super().__init__(sessions, cipher)
        self._sessions = sessions
        self._cipher = cipher

    async def save_suggestion(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        proposal_id: UUID,
        expected_version: int,
        desired_state: Mapping[str, object],
        retain_until: datetime,
    ):
        """先由另一短事务赢得版本二，再执行 production CAS 并期待稳定冲突。"""
        async with self._sessions.begin() as session:
            proposals = SqlAlchemyCalendarProposalRepository(session, self._cipher)
            current = await proposals.get_current(user_id=user_id, proposal_id=proposal_id)
            assert current is not None
            concurrent = dict(current.desired_snapshot.content)
            concurrent["title"] = "Concurrent winner"
            concurrent["availability"] = None
            await proposals.save_next_version(
                snapshot_id=uuid4(),
                user_id=user_id,
                proposal_id=proposal_id,
                expected_version=expected_version,
                desired_state=concurrent,
                retain_until=retain_until,
            )
        return await super().save_suggestion(
            snapshot_id=snapshot_id,
            user_id=user_id,
            proposal_id=proposal_id,
            expected_version=expected_version,
            desired_state=desired_state,
            retain_until=retain_until,
        )


@pytest.mark.asyncio
async def test_use_case_computes_without_transaction_and_lost_cas_does_not_overwrite(
    database_url: ValidatedTestDatabaseUrl,
) -> None:
    """真实 adapter 关闭读事务后计算，竞态写只保留先赢得的 immutable version。"""
    sessions = build_session_factory(database_url)
    probe = _TransactionProbe()
    sync_engine = sessions.engine.sync_engine
    event.listen(sync_engine, "begin", probe.began)
    event.listen(sync_engine, "commit", probe.ended)
    event.listen(sync_engine, "rollback", probe.ended)
    try:
        seeded = await _seed_availability_user(
            sessions,
            connection_count=1,
            cursor_success_at=NOW,
            event_count=1,
        )
        persistence = _RacingAvailabilityRepository(sessions, ACTION_CIPHER)

        def transaction_free_suggest(**kwargs: object) -> AvailabilityResult:
            """纯候选计算开始时数据库事务计数必须已经归零。"""
            assert probe.active == 0
            return suggest_meeting_times(**kwargs)  # type: ignore[arg-type]

        forbidden = cast(object, _ForbiddenLegacyPort())
        use_case = CalendarProposalUseCase(
            proposals=forbidden,  # type: ignore[arg-type]
            calendar=forbidden,  # type: ignore[arg-type]
            availability=persistence,
            suggestion_function=cast(Callable[..., AvailabilityResult], transaction_free_suggest),
            clock=lambda: NOW,
        )
        with pytest.raises(StateConflictError) as raised:
            await use_case.suggest_times(
                user_id=seeded.user_id,
                proposal_id=seeded.proposal_id,
                expected_version=1,
                search_start=NOW,
            )

        assert raised.value.error_code == "proposal_version_conflict"
        assert probe.active == 0
        assert probe.maximum == 1
        async with sessions() as session:
            current = await SqlAlchemyCalendarProposalRepository(
                session, ACTION_CIPHER
            ).get_current(user_id=seeded.user_id, proposal_id=seeded.proposal_id)
        assert current is not None
        persisted = CalendarProposalContent.model_validate(current.desired_snapshot.content)
        assert current.current_version == 2
        assert persisted.title == "Concurrent winner"
        assert persisted.availability is None
    finally:
        event.remove(sync_engine, "begin", probe.began)
        event.remove(sync_engine, "commit", probe.ended)
        event.remove(sync_engine, "rollback", probe.ended)
        await sessions.dispose()
