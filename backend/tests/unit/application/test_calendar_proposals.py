"""验证版本化日历提案的应用层授权、默认值与恢复规则。"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from ai_employee.application.ports.calendar import CalendarEvent
from ai_employee.application.use_cases.calendar_proposals import (
    CalendarAvailabilityContext,
    CalendarProposalContent,
    CalendarProposalEventBinding,
    CalendarProposalEventSnapshot,
    CalendarProposalSnapshot,
    CalendarProposalTargetSnapshot,
    CalendarProposalUseCase,
    CalendarRestoreSourceSnapshot,
    CalendarSnapshot,
    parse_calendar_proposal_conversation_request,
)
from ai_employee.domain.actions import CalendarProposalStatus
from ai_employee.domain.calendar_actions import NotificationPolicy
from ai_employee.domain.calendar_availability import AvailabilityEvent
from ai_employee.domain.connections import CapabilityStatus, ConnectionStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.settings import WeeklyWorkingHours
from ai_employee.domain.tasks import JsonValue

USER_ID = UUID("00000000-0000-0000-0000-000000000911")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000912")
ALTERNATE_CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000927")
EVENT_ID = UUID("00000000-0000-0000-0000-000000000913")
PROPOSAL_ID = UUID("00000000-0000-0000-0000-000000000914")
DESIRED_ID = UUID("00000000-0000-0000-0000-000000000915")
BEFORE_ID = UUID("00000000-0000-0000-0000-000000000916")
NEXT_DESIRED_ID = UUID("00000000-0000-0000-0000-000000000917")
OPERATION_ID = UUID("00000000-0000-0000-0000-000000000918")
NEXT_OPERATION_ID = UUID("00000000-0000-0000-0000-000000000919")
MISSING_CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000920")
NOW = datetime(2030, 3, 11, 8, tzinfo=UTC)
RETAIN_UNTIL = NOW + timedelta(days=365)
CALENDAR_ID = "synthetic-calendar"
PROVIDER_EVENT_ID = "synthetic-event"


def _working_hours() -> WeeklyWorkingHours:
    """返回 UTC 周一至周五的默认工作时间。"""
    return WeeklyWorkingHours.from_mapping(
        {
            "monday": [["09:00", "18:00"]],
            "tuesday": [["09:00", "18:00"]],
            "wednesday": [["09:00", "18:00"]],
            "thursday": [["09:00", "18:00"]],
            "friday": [["09:00", "18:00"]],
            "saturday": [],
            "sunday": [],
        }
    )


def _target(
    *,
    can_write: bool = True,
    write_status: CapabilityStatus = CapabilityStatus.ENABLED,
    supported: frozenset[NotificationPolicy] = frozenset(NotificationPolicy),
) -> CalendarProposalTargetSnapshot:
    """构造不含 OAuth scope/token 的最小可写目标投影。"""
    return CalendarProposalTargetSnapshot(
        connection_id=CONNECTION_ID,
        calendar_id=CALENDAR_ID,
        provider="google",
        timezone="UTC",
        connection_status=ConnectionStatus.CONNECTED,
        read_capability_status=CapabilityStatus.ENABLED,
        write_capability_status=write_status,
        write_capability_error_code=None,
        can_write=can_write,
        retention_days=365,
        supported_notification_policies=supported,
    )


def _event(
    *,
    recurring_event_id: str | None = None,
    can_edit: bool = True,
) -> CalendarProposalEventSnapshot:
    """构造精确绑定本地 UUID 与供应商三元身份的同步事件。"""
    return CalendarProposalEventSnapshot(
        event_id=EVENT_ID,
        connection_id=CONNECTION_ID,
        calendar_id=CALENDAR_ID,
        provider="google",
        provider_event_id=PROVIDER_EVENT_ID,
        title="Synthetic event",
        description="Synthetic description",
        location="Old room",
        starts_at=datetime(2030, 3, 11, 10, tzinfo=UTC),
        ends_at=datetime(2030, 3, 11, 11, tzinfo=UTC),
        all_day=False,
        timezone="UTC",
        attendees=("attendee@example.test",),
        recurring_event_id=recurring_event_id,
        etag='W/"etag-1"',
        status="confirmed",
        can_edit=can_edit,
    )


def _provider_event(
    *,
    status: str = "confirmed",
    can_edit: bool = True,
    recurring_event_id: str | None = None,
    etag: str | None = 'W/"etag-current"',
    location: str = "Current room",
) -> CalendarEvent:
    """构造供应商中立精确 GET 结果，不使用 Google/Microsoft SDK 类型。"""
    return CalendarEvent(
        event_id=PROVIDER_EVENT_ID,
        calendar_id=CALENDAR_ID,
        title="Current title",
        description="Current description",
        location=location,
        starts_at=datetime(2030, 3, 11, 12, tzinfo=UTC),
        ends_at=datetime(2030, 3, 11, 13, tzinfo=UTC),
        all_day=False,
        transparency="opaque",
        status=status,
        timezone="UTC",
        recurring_event_id=recurring_event_id,
        etag=etag,
        provider_url="https://calendar.example.test/event",
        attendees=({"email": "attendee@example.test"},),
        can_edit=can_edit,
    )


class _ProposalRepository:
    """用真实不可变快照语义模拟提案仓储，不模拟供应商行为。"""

    def __init__(self) -> None:
        self.proposals: dict[UUID, CalendarProposalSnapshot] = {}
        self.snapshots: dict[UUID, CalendarSnapshot] = {}
        self.by_key: dict[tuple[UUID, str], UUID] = {}

    async def get_by_creation_key(
        self, *, user_id: UUID, creation_idempotency_key: str
    ) -> CalendarProposalSnapshot | None:
        """按用户创建键返回既有本地事实。"""
        proposal_id = self.by_key.get((user_id, creation_idempotency_key))
        return self.proposals.get(proposal_id) if proposal_id is not None else None

    async def create(
        self,
        *,
        proposal_id: UUID,
        snapshot_id: UUID,
        user_id: UUID,
        connection_id: UUID,
        creation_idempotency_key: str,
        creation_payload_hash: str,
        calendar_id: str,
        operation_kind: str,
        target_event_id: str | None,
        base_etag: str | None,
        retain_until: datetime,
        desired_state: Mapping[str, object],
    ) -> CalendarProposalSnapshot:
        """首次创建版本一；同用户同键重放复用既有提案。"""
        existing = await self.get_by_creation_key(
            user_id=user_id,
            creation_idempotency_key=creation_idempotency_key,
        )
        if existing is not None:
            return existing
        snapshot = CalendarSnapshot(
            snapshot_id=snapshot_id,
            proposal_id=proposal_id,
            version=1,
            snapshot_kind="desired",
            content=cast(dict[str, JsonValue], dict(desired_state)),
            canonical_hash="d" * 64,
            retain_until=retain_until,
            created_at=NOW,
        )
        proposal = CalendarProposalSnapshot(
            proposal_id=proposal_id,
            connection_id=connection_id,
            calendar_id=calendar_id,
            operation_kind=cast(object, operation_kind),
            target_event_id=target_event_id,
            base_etag=base_etag,
            creation_payload_hash=creation_payload_hash,
            current_version=1,
            status=CalendarProposalStatus.EDITING,
            retain_until=retain_until,
            desired_snapshot=snapshot,
            before_snapshot_id=None,
        )
        self.snapshots[snapshot_id] = snapshot
        self.proposals[proposal_id] = proposal
        self.by_key[(user_id, creation_idempotency_key)] = proposal_id
        return proposal

    async def get_current(
        self, *, user_id: UUID, proposal_id: UUID
    ) -> CalendarProposalSnapshot | None:
        """测试数据均属于单一用户，未知 ID 返回 None。"""
        del user_id
        return self.proposals.get(proposal_id)

    async def save_next_version(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        proposal_id: UUID,
        expected_version: int,
        desired_state: Mapping[str, object],
        retain_until: datetime,
        connection_id: UUID | None = None,
        calendar_id: str | None = None,
    ) -> CalendarProposalSnapshot | None:
        """执行与 PostgreSQL 父行相同的版本 CAS 与可选精确 retarget。"""
        del user_id
        current = self.proposals.get(proposal_id)
        if current is None:
            return None
        if current.current_version != expected_version:
            raise StateConflictError(
                error_code="proposal_version_conflict",
                message="calendar proposal version changed",
            )
        version = expected_version + 1
        snapshot = CalendarSnapshot(
            snapshot_id=snapshot_id,
            proposal_id=proposal_id,
            version=version,
            snapshot_kind="desired",
            content=cast(dict[str, JsonValue], dict(desired_state)),
            canonical_hash="e" * 64,
            retain_until=retain_until,
            created_at=NOW,
        )
        saved = replace(
            current,
            connection_id=connection_id or current.connection_id,
            calendar_id=calendar_id or current.calendar_id,
            current_version=version,
            retain_until=retain_until,
            desired_snapshot=snapshot,
        )
        self.snapshots[snapshot_id] = snapshot
        self.proposals[proposal_id] = saved
        return saved

    async def save_snapshot(
        self,
        *,
        snapshot_id: UUID,
        user_id: UUID,
        proposal_id: UUID,
        version: int,
        snapshot_kind: str,
        content: Mapping[str, object],
        retain_until: datetime,
    ) -> CalendarSnapshot | None:
        """保存 before snapshot，并同步更新提案公开引用。"""
        del user_id
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            return None
        existing = next(
            (
                item
                for item in self.snapshots.values()
                if item.proposal_id == proposal_id
                and item.version == version
                and item.snapshot_kind == snapshot_kind
            ),
            None,
        )
        if existing is not None:
            return existing
        snapshot = CalendarSnapshot(
            snapshot_id=snapshot_id,
            proposal_id=proposal_id,
            version=version,
            snapshot_kind=cast(object, snapshot_kind),
            content=cast(dict[str, JsonValue], dict(content)),
            canonical_hash="b" * 64,
            retain_until=retain_until,
            created_at=NOW,
        )
        self.snapshots[snapshot_id] = snapshot
        if snapshot_kind == "before":
            self.proposals[proposal_id] = replace(
                proposal,
                before_snapshot_id=snapshot_id,
            )
        return snapshot

    async def load_snapshot(self, *, user_id: UUID, snapshot_id: UUID) -> CalendarSnapshot | None:
        """按 ID 读取合成快照。"""
        del user_id
        return self.snapshots.get(snapshot_id)

    async def get_restore_source(
        self, *, user_id: UUID, source_snapshot_id: UUID
    ) -> CalendarRestoreSourceSnapshot | None:
        """从 before snapshot 反查不可变提案目标身份。"""
        del user_id
        snapshot = self.snapshots.get(source_snapshot_id)
        if snapshot is None or snapshot.snapshot_kind != "before":
            return None
        proposal = self.proposals[snapshot.proposal_id]
        if proposal.target_event_id is None:
            return None
        return CalendarRestoreSourceSnapshot(
            source_snapshot_id=source_snapshot_id,
            source_proposal_id=proposal.proposal_id,
            connection_id=proposal.connection_id,
            calendar_id=proposal.calendar_id,
            provider_event_id=proposal.target_event_id,
        )


class _CalendarSource:
    """提供用户范围内目标、事件与本人日历可用性事实。"""

    def __init__(
        self,
        *,
        target: CalendarProposalTargetSnapshot | None = None,
        event: CalendarProposalEventSnapshot | None = None,
    ) -> None:
        self.target = target or _target()
        self.event = event or _event()
        self.availability_calls = 0

    async def get_default_proposal_target(
        self, *, user_id: UUID
    ) -> CalendarProposalTargetSnapshot | None:
        """返回显式默认日历，不猜测其他连接。"""
        del user_id
        return self.target

    async def get_proposal_target(
        self, *, user_id: UUID, connection_id: UUID, calendar_id: str
    ) -> CalendarProposalTargetSnapshot | None:
        """只允许精确连接与日历 ID 命中。"""
        del user_id
        if connection_id != self.target.connection_id or calendar_id != self.target.calendar_id:
            return None
        return self.target

    async def get_proposal_event_binding(
        self, *, user_id: UUID, event_id: UUID
    ) -> CalendarProposalEventBinding | None:
        """只返回当前 Fake 事件的非敏感精确身份。"""
        del user_id
        if event_id != self.event.event_id:
            return None
        return CalendarProposalEventBinding(
            event_id=self.event.event_id,
            connection_id=self.event.connection_id,
            calendar_id=self.event.calendar_id,
            provider_event_id=self.event.provider_event_id,
        )

    async def get_proposal_event(
        self, *, user_id: UUID, binding: CalendarProposalEventBinding
    ) -> CalendarProposalEventSnapshot | None:
        """只有 local/connection/calendar/provider 四项都匹配才返回完整事件。"""
        del user_id
        expected = CalendarProposalEventBinding(
            event_id=self.event.event_id,
            connection_id=self.event.connection_id,
            calendar_id=self.event.calendar_id,
            provider_event_id=self.event.provider_event_id,
        )
        return self.event if binding == expected else None

    async def get_availability_context(
        self,
        *,
        user_id: UUID,
        search_start: datetime,
        horizon_days: int,
    ) -> CalendarAvailabilityContext | None:
        """返回本人日历事件；接口没有参会人或 Free/Busy 参数。"""
        del user_id, search_start, horizon_days
        self.availability_calls += 1
        return CalendarAvailabilityContext(
            timezone="UTC",
            working_hours=_working_hours(),
            meeting_buffer=timedelta(minutes=10),
            events=(
                AvailabilityEvent(
                    starts_at=datetime(2030, 3, 11, 9, tzinfo=UTC),
                    ends_at=datetime(2030, 3, 11, 10, tzinfo=UTC),
                    all_day=False,
                    transparency="opaque",
                    status="confirmed",
                ),
            ),
            missing_connection_ids=(MISSING_CONNECTION_ID,),
        )


def _ids(*values: UUID):
    """返回严格有界 UUID 工厂，意外多取会立即暴露测试契约错误。"""
    iterator = iter(values)
    return lambda: next(iterator)


def _use_case(
    *,
    proposals: _ProposalRepository | None = None,
    calendar: _CalendarSource | None = None,
    ids: tuple[UUID, ...] = (
        PROPOSAL_ID,
        DESIRED_ID,
        OPERATION_ID,
        BEFORE_ID,
        NEXT_DESIRED_ID,
        NEXT_OPERATION_ID,
    ),
) -> tuple[CalendarProposalUseCase, _ProposalRepository, _CalendarSource]:
    """构造可观测的提案用例与两个窄 Fake 端口。"""
    repository = proposals or _ProposalRepository()
    source = calendar or _CalendarSource()
    return (
        CalendarProposalUseCase(
            proposals=repository,
            calendar=source,
            clock=lambda: NOW,
            id_factory=_ids(*ids),
        ),
        repository,
        source,
    )


@pytest.mark.asyncio
async def test_update_proposal_captures_etag_and_encrypted_before_snapshot() -> None:
    """修改提案必须绑定本地最新 ETag，并要求仓储保存 before snapshot。"""
    use_case, repository, _ = _use_case()

    proposal = await use_case.create_update(
        user_id=USER_ID,
        event_id=EVENT_ID,
        changes={"location": "Synthetic room"},
    )

    assert proposal.base_etag == 'W/"etag-1"'
    assert proposal.before_snapshot_id is not None
    before = repository.snapshots[proposal.before_snapshot_id]
    assert before.snapshot_kind == "before"
    assert before.content["location"] == "Old room"
    assert proposal.content.location == "Synthetic room"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attendees", "expected"),
    (
        ((), NotificationPolicy.NONE),
        (("person@example.test",), NotificationPolicy.ALL),
    ),
)
async def test_create_notification_default_depends_only_on_attendees(
    attendees: tuple[str, ...], expected: NotificationPolicy
) -> None:
    """创建提案有参会人默认 all，无参会人默认 none。"""
    use_case, _, _ = _use_case()

    proposal = await use_case.create_event(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id=CALENDAR_ID,
        idempotency_key=f"create-default-{expected.value}",
        values={
            "title": "Synthetic meeting",
            "starts_at": datetime(2030, 3, 12, 9, tzinfo=UTC),
            "ends_at": datetime(2030, 3, 12, 10, tzinfo=UTC),
            "timezone": "UTC",
            "all_day": False,
            "attendees": attendees,
        },
    )

    assert proposal.notification_policy is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"location": "New room"}, NotificationPolicy.ALL),
        ({"starts_at": datetime(2030, 3, 11, 10, 30, tzinfo=UTC)}, NotificationPolicy.ALL),
        ({"attendees": ()}, NotificationPolicy.ALL),
        ({"title": "Personal title"}, NotificationPolicy.NONE),
        ({"description": "Personal note"}, NotificationPolicy.NONE),
    ),
)
async def test_update_notification_default_follows_changed_fields(
    changes: dict[str, object], expected: NotificationPolicy
) -> None:
    """更新默认通知只由完整 diff 的时间、地点或参会人字段决定。"""
    use_case, _, _ = _use_case()

    proposal = await use_case.create_update(
        user_id=USER_ID,
        event_id=EVENT_ID,
        changes=changes,
    )

    assert proposal.notification_policy is expected


@pytest.mark.asyncio
async def test_recurring_event_is_rejected_with_stable_error() -> None:
    """master 与 instance 都由非空 recurring_event_id 统一拒绝。"""
    source = _CalendarSource(event=_event(recurring_event_id=PROVIDER_EVENT_ID))
    use_case, repository, _ = _use_case(calendar=source)

    with pytest.raises(StateConflictError) as error:
        await use_case.create_update(
            user_id=USER_ID,
            event_id=EVENT_ID,
            changes={"location": "Synthetic room"},
        )

    assert error.value.error_code == "calendar_recurring_event_unsupported"
    assert repository.proposals == {}


@pytest.mark.asyncio
async def test_read_only_calendar_is_rejected_before_snapshot_creation() -> None:
    """目录 can_write=false 时即使事件自身可编辑也必须 fail closed。"""
    source = _CalendarSource(target=_target(can_write=False))
    use_case, repository, _ = _use_case(calendar=source)

    with pytest.raises(StateConflictError) as error:
        await use_case.create_update(
            user_id=USER_ID,
            event_id=EVENT_ID,
            changes={"location": "Synthetic room"},
        )

    assert error.value.error_code == "calendar_read_only"
    assert repository.proposals == {}


@pytest.mark.asyncio
async def test_disabled_calendar_write_capability_is_rejected() -> None:
    """连接未启用 calendar.write 时不得只凭目录 ACL 创建本地写提案。"""
    source = _CalendarSource(target=_target(write_status=CapabilityStatus.DISABLED))
    use_case, repository, _ = _use_case(calendar=source)

    with pytest.raises(StateConflictError) as error:
        await use_case.create_event(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            calendar_id=CALENDAR_ID,
            idempotency_key="disabled-write",
            values={},
        )

    assert error.value.error_code == "connection_capability_disabled"
    assert repository.proposals == {}


@pytest.mark.asyncio
async def test_edit_increments_version_and_invalidates_cached_availability() -> None:
    """时间编辑必须推进版本，并删除基于旧时间计算的冲突缓存。"""
    use_case, _, _ = _use_case()
    created = await use_case.create_update(
        user_id=USER_ID,
        event_id=EVENT_ID,
        changes={"location": "Synthetic room"},
    )
    cached_content = created.content.model_copy(
        update={
            "availability": {
                "candidates": [],
                "completeness": "complete",
                "missing_connection_ids": [],
                "attendee_availability_checked": False,
            }
        }
    )
    # 先通过普通编辑把缓存写入版本二，再修改时间观察失效。
    cached = await use_case.edit(
        user_id=USER_ID,
        proposal_id=created.proposal_id,
        expected_version=created.current_version,
        changes={"availability": cached_content.availability},
        internal_cache_update=True,
    )

    updated = await use_case.edit(
        user_id=USER_ID,
        proposal_id=created.proposal_id,
        expected_version=cached.current_version,
        changes={"starts_at": "2030-03-11T10:30:00+00:00"},
    )

    assert updated.current_version == 3
    assert updated.content.availability is None


@pytest.mark.asyncio
async def test_suggest_uses_only_personal_calendar_context_and_marks_partial() -> None:
    """建议入口只读取本人同步事件，结果不能声称查询过参会人 Free/Busy。"""
    use_case, _, source = _use_case()
    created = await use_case.create_event(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id=CALENDAR_ID,
        idempotency_key="suggest-personal-only",
        values={
            "title": "Synthetic meeting",
            "starts_at": datetime(2030, 3, 11, 9, tzinfo=UTC),
            "ends_at": datetime(2030, 3, 11, 9, 30, tzinfo=UTC),
            "timezone": "UTC",
            "all_day": False,
            "attendees": ("attendee@example.test",),
        },
    )

    suggested = await use_case.suggest_times(
        user_id=USER_ID,
        proposal_id=created.proposal_id,
        expected_version=created.current_version,
        search_start=NOW,
    )

    assert source.availability_calls == 1
    assert suggested.content.availability is not None
    assert suggested.content.availability.completeness == "partial"
    assert suggested.content.availability.missing_connection_ids == (MISSING_CONNECTION_ID,)
    assert suggested.content.availability.attendee_availability_checked is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "expected_error"),
    (
        (None, "calendar_event_deleted"),
        (_provider_event(status="cancelled"), "calendar_event_deleted"),
        (_provider_event(can_edit=False), "calendar_event_not_editable"),
        (
            _provider_event(recurring_event_id=PROVIDER_EVENT_ID),
            "calendar_recurring_event_unsupported",
        ),
    ),
)
async def test_restore_rejects_deleted_uneditable_or_recurring_provider_facts(
    current: CalendarEvent | None, expected_error: str
) -> None:
    """恢复以供应商当前事实为准，失败时不得留下新提案。"""
    use_case, repository, _ = _use_case()
    source = await use_case.create_update(
        user_id=USER_ID,
        event_id=EVENT_ID,
        changes={"location": "Historical room"},
    )
    assert source.before_snapshot_id is not None
    proposal_count = len(repository.proposals)

    with pytest.raises(StateConflictError) as error:
        await use_case.create_restore(
            user_id=USER_ID,
            source_snapshot_id=source.before_snapshot_id,
            current_event=current,
            idempotency_key=f"restore-{expected_error}",
        )

    assert error.value.error_code == expected_error
    assert len(repository.proposals) == proposal_count


@pytest.mark.asyncio
async def test_restore_uses_current_etag_complete_diff_and_new_operation_identity() -> None:
    """恢复创建新版本一提案，保存当前 before，并对历史状态生成完整 diff。"""
    ids = (
        PROPOSAL_ID,
        DESIRED_ID,
        OPERATION_ID,
        BEFORE_ID,
        UUID("00000000-0000-0000-0000-000000000921"),
        UUID("00000000-0000-0000-0000-000000000922"),
        NEXT_OPERATION_ID,
        UUID("00000000-0000-0000-0000-000000000923"),
    )
    use_case, repository, _ = _use_case(ids=ids)
    source = await use_case.create_update(
        user_id=USER_ID,
        event_id=EVENT_ID,
        changes={"location": "Historical room"},
    )
    assert source.before_snapshot_id is not None

    restored = await use_case.create_restore(
        user_id=USER_ID,
        source_snapshot_id=source.before_snapshot_id,
        current_event=_provider_event(),
        idempotency_key="restore-new-operation",
    )

    assert restored.proposal_id != source.proposal_id
    assert restored.current_version == 1
    assert restored.base_etag == 'W/"etag-current"'
    assert restored.before_snapshot_id is not None
    assert restored.content.operation_id != source.content.operation_id
    assert set(restored.changed_fields) == {
        "description",
        "ends_at",
        "location",
        "starts_at",
        "title",
    }
    assert restored.notification_policy is NotificationPolicy.ALL
    before = repository.snapshots[restored.before_snapshot_id]
    assert before.content["location"] == "Current room"


@pytest.mark.asyncio
@pytest.mark.parametrize("timezone", ["Asia/Shanghai", "America/Los_Angeles"])
async def test_all_day_update_and_restore_snapshots_preserve_local_dates(
    timezone: str,
) -> None:
    """全天 update/restore 的日期必须按事件 IANA 时区往返，不能按 UTC 偏移。"""

    def local_midnight(day: date) -> datetime:
        """把合成本地日期边界转换为同步层保存的 UTC 瞬间。"""
        return datetime.combine(day, datetime.min.time(), ZoneInfo(timezone)).astimezone(UTC)

    source_start = date(2030, 3, 11)
    source_end = date(2030, 3, 12)
    current_start = date(2030, 3, 13)
    current_end = date(2030, 3, 14)
    source = _CalendarSource(
        target=replace(_target(), timezone=timezone),
        event=replace(
            _event(),
            starts_at=local_midnight(source_start),
            ends_at=local_midnight(source_end),
            all_day=True,
            timezone=timezone,
        ),
    )
    ids = (
        PROPOSAL_ID,
        DESIRED_ID,
        OPERATION_ID,
        BEFORE_ID,
        UUID("00000000-0000-0000-0000-000000000924"),
        UUID("00000000-0000-0000-0000-000000000925"),
        NEXT_OPERATION_ID,
        UUID("00000000-0000-0000-0000-000000000926"),
    )
    use_case, repository, _ = _use_case(calendar=source, ids=ids)

    updated = await use_case.create_update(
        user_id=USER_ID,
        event_id=EVENT_ID,
        changes={"title": "Historical all-day title"},
    )
    assert updated.before_snapshot_id is not None
    update_before = repository.snapshots[updated.before_snapshot_id]
    restored = await use_case.create_restore(
        user_id=USER_ID,
        source_snapshot_id=updated.before_snapshot_id,
        current_event=replace(
            _provider_event(),
            starts_at=local_midnight(current_start),
            ends_at=local_midnight(current_end),
            all_day=True,
            timezone=timezone,
        ),
        idempotency_key=f"restore-all-day-{timezone}",
    )
    assert restored.before_snapshot_id is not None
    restore_before = repository.snapshots[restored.before_snapshot_id]

    assert update_before.content["starts_at"] == source_start.isoformat()
    assert update_before.content["ends_at"] == source_end.isoformat()
    assert restored.content.starts_at == source_start.isoformat()
    assert restored.content.ends_at == source_end.isoformat()
    assert restore_before.content["starts_at"] == current_start.isoformat()
    assert restore_before.content["ends_at"] == current_end.isoformat()


@pytest.mark.asyncio
async def test_unsupported_notification_mapping_is_rejected_before_submission() -> None:
    """所选供应商不能无损表达通知策略时，编辑态也必须稳定拒绝。"""
    source = _CalendarSource(target=_target(supported=frozenset({NotificationPolicy.ALL})))
    use_case, _, _ = _use_case(calendar=source)
    created = await use_case.create_update(
        user_id=USER_ID,
        event_id=EVENT_ID,
        changes={"location": "Synthetic room"},
    )

    with pytest.raises(StateConflictError) as error:
        await use_case.edit(
            user_id=USER_ID,
            proposal_id=created.proposal_id,
            expected_version=created.current_version,
            changes={"notification_policy": "none"},
        )

    assert error.value.error_code == "calendar_notification_mapping_unsupported"


@pytest.mark.asyncio
async def test_creation_key_replay_reuses_existing_update_proposal() -> None:
    """相同用户创建键重放复用本地对象，不产生第二组快照。"""
    use_case, repository, _ = _use_case()

    first = await use_case.create_update(
        user_id=USER_ID,
        event_id=EVENT_ID,
        changes={"location": "Synthetic room"},
        idempotency_key="stable-update-key",
    )
    replay = await use_case.create_update(
        user_id=USER_ID,
        event_id=EVENT_ID,
        changes={"location": "Synthetic room"},
        idempotency_key="stable-update-key",
    )

    assert replay.proposal_id == first.proposal_id
    assert len(repository.proposals) == 1
    assert len(repository.snapshots) == 2


@pytest.mark.asyncio
async def test_update_creation_key_rejects_same_content_with_new_base_etag() -> None:
    """同键必须绑定生成时 ETag，不能把供应商新版本误判为原请求重放。"""
    use_case, repository, source = _use_case()
    await use_case.create_update(
        user_id=USER_ID,
        event_id=EVENT_ID,
        changes={"location": "Synthetic room"},
        idempotency_key="etag-bound-update-key",
    )
    source.event = replace(source.event, etag='W/"etag-2"')

    with pytest.raises(StateConflictError) as error:
        await use_case.create_update(
            user_id=USER_ID,
            event_id=EVENT_ID,
            changes={"location": "Synthetic room"},
            idempotency_key="etag-bound-update-key",
        )

    assert error.value.error_code == "idempotency_key_payload_mismatch"
    assert len(repository.proposals) == 1
    assert len(repository.snapshots) == 2


@pytest.mark.asyncio
async def test_conversation_shell_requires_explicit_confirmation_before_submission() -> None:
    """明确对话命令只创建 editing shell，不替用户确认写命令字段。"""
    use_case, _, _ = _use_case()

    proposal = await use_case.create_shell(
        user_id=USER_ID,
        idempotency_key="conversation-calendar-shell",
    )

    assert proposal.status is CalendarProposalStatus.EDITING
    assert proposal.submission_ready is False
    assert proposal.required_confirmations == (
        "calendar",
        "time",
        "attendees",
        "notification_policy",
    )
    assert proposal.content.notification_policy is None
    assert proposal.content.confirmed_fields == ()


@pytest.mark.asyncio
async def test_shell_partial_edit_does_not_confirm_unrelated_required_fields() -> None:
    """填写标题和完整时间只更新值，不得替用户确认日历、时间、参会人或通知。"""
    use_case, _, _ = _use_case()
    shell = await use_case.create_shell(
        user_id=USER_ID,
        idempotency_key="partial-calendar-shell",
    )

    edited = await use_case.edit(
        user_id=USER_ID,
        proposal_id=shell.proposal_id,
        expected_version=shell.current_version,
        changes={
            "title": "Synthetic meeting",
            "starts_at": datetime(2030, 3, 12, 9, tzinfo=UTC),
            "ends_at": datetime(2030, 3, 12, 10, tzinfo=UTC),
            "timezone": "UTC",
            "all_day": False,
        },
    )

    assert edited.submission_ready is False
    assert edited.content.confirmed_fields == ()
    assert edited.required_confirmations == (
        "calendar",
        "time",
        "attendees",
        "notification_policy",
    )


@pytest.mark.asyncio
async def test_shell_becomes_ready_only_after_each_typed_confirmation() -> None:
    """四项确认必须分别推进版本，缺少任一项时都不能 submission ready。"""
    ids = tuple(UUID(f"00000000-0000-0000-0000-{value:012d}") for value in range(951, 959))
    use_case, _, _ = _use_case(ids=ids)
    shell = await use_case.create_shell(
        user_id=USER_ID,
        idempotency_key="confirmed-calendar-shell",
    )
    current = await use_case.edit(
        user_id=USER_ID,
        proposal_id=shell.proposal_id,
        expected_version=shell.current_version,
        changes={
            "title": "Synthetic meeting",
            "starts_at": datetime(2030, 3, 12, 9, tzinfo=UTC),
            "ends_at": datetime(2030, 3, 12, 10, tzinfo=UTC),
            "timezone": "UTC",
            "all_day": False,
            "attendees": (),
            "notification_policy": "none",
        },
    )

    expected_remaining = (
        ("time", "attendees", "notification_policy"),
        ("attendees", "notification_policy"),
        ("notification_policy",),
        (),
    )
    for confirmation, remaining in zip(
        ("calendar", "time", "attendees", "notification_policy"),
        expected_remaining,
        strict=True,
    ):
        current = await use_case.confirm(
            user_id=USER_ID,
            proposal_id=current.proposal_id,
            expected_version=current.current_version,
            confirmation=confirmation,
            connection_id=CONNECTION_ID if confirmation == "calendar" else None,
            calendar_id=CALENDAR_ID if confirmation == "calendar" else None,
        )
        assert current.required_confirmations == remaining
        assert current.submission_ready is (remaining == ())


@pytest.mark.asyncio
async def test_shell_calendar_confirmation_can_select_an_exact_writable_target() -> None:
    """默认日历只建立 shell 归属；显式确认可把编辑提案原子切换到另一可写日历。"""
    source = _CalendarSource()
    use_case, repository, _ = _use_case(calendar=source)
    shell = await use_case.create_shell(
        user_id=USER_ID,
        idempotency_key="retarget-calendar-shell",
    )
    source.target = replace(
        _target(),
        connection_id=ALTERNATE_CONNECTION_ID,
        calendar_id="alternate-calendar",
    )

    selected = await use_case.confirm(
        user_id=USER_ID,
        proposal_id=shell.proposal_id,
        expected_version=shell.current_version,
        confirmation="calendar",
        connection_id=ALTERNATE_CONNECTION_ID,
        calendar_id="alternate-calendar",
    )

    assert selected.connection_id == ALTERNATE_CONNECTION_ID
    assert selected.calendar_id == "alternate-calendar"
    assert selected.required_confirmations == (
        "time",
        "attendees",
        "notification_policy",
    )
    assert repository.proposals[selected.proposal_id].connection_id == ALTERNATE_CONNECTION_ID


@pytest.mark.asyncio
async def test_shell_calendar_confirmation_rejects_a_read_only_target() -> None:
    """显式日历选择仍必须通过连接能力与目录 can_write 双重校验。"""
    source = _CalendarSource()
    use_case, repository, _ = _use_case(calendar=source)
    shell = await use_case.create_shell(
        user_id=USER_ID,
        idempotency_key="read-only-calendar-shell",
    )
    source.target = replace(
        _target(can_write=False),
        connection_id=ALTERNATE_CONNECTION_ID,
        calendar_id="read-only-calendar",
    )

    with pytest.raises(StateConflictError) as error:
        await use_case.confirm(
            user_id=USER_ID,
            proposal_id=shell.proposal_id,
            expected_version=shell.current_version,
            confirmation="calendar",
            connection_id=ALTERNATE_CONNECTION_ID,
            calendar_id="read-only-calendar",
        )

    assert error.value.error_code == "calendar_read_only"
    assert repository.proposals[shell.proposal_id].current_version == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("Please prepare a calendar proposal", True),
        ("Prepare a calendar proposal.", True),
        ("Draft a meeting proposal", True),
        ("请准备一个日程提案", True),
        ("请起草会议提案。", True),
        ("Can you prepare a calendar proposal?", False),
        ("Could you maybe draft a meeting proposal?", False),
        ("Would you prepare an event proposal?", False),
        ("Maybe prepare a calendar proposal", False),
        ("I do not want you to prepare a calendar proposal", False),
        ("I don't want you to draft a meeting proposal", False),
        ("Please do not prepare a calendar proposal", False),
        ("Prepare a calendar proposal for Tuesday", False),
        ("你能准备一个日程提案吗？", False),
        ("可以帮我准备一个日历提案吗？", False),
        ("我不想让你准备一个日程提案", False),
        ("请不要起草会议提案", False),
        ("也许准备一个日历提案", False),
        ("Can you explain calendar proposals?", False),
        ("Do not prepare a calendar proposal", False),
        ("create calendar event", False),
    ),
)
def test_calendar_conversation_parser_requires_explicit_safe_shell_command(
    text: str, expected: bool
) -> None:
    """确定性解析只识别明确草案请求，不把问题、否定或直接写动词当作动作。"""
    assert parse_calendar_proposal_conversation_request(text) is expected


def test_proposal_content_rejects_recurrence_fields() -> None:
    """编辑内容 Schema 本身不允许 recurrence 或供应商扩展字段。"""
    with pytest.raises(ValueError):
        CalendarProposalContent.model_validate(
            {
                "operation_id": str(OPERATION_ID),
                "title": "Synthetic",
                "recurrence": ["RRULE:FREQ=DAILY"],
            }
        )


def test_proposal_content_normalizes_legacy_confirmation_order() -> None:
    """修复前已持久化的字母序确认字段仍应可读，并规范为产品顺序。"""
    content = CalendarProposalContent(
        operation_id=OPERATION_ID,
        confirmed_fields=("attendees", "calendar", "notification_policy", "time"),
    )

    assert content.confirmed_fields == (
        "calendar",
        "time",
        "attendees",
        "notification_policy",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "recurrence_field"),
    (
        ("create", "recurrence"),
        ("update", "recurrence_rule"),
        ("edit", "recurring_event_id"),
    ),
)
async def test_recurrence_input_uses_stable_domain_error(
    operation: str,
    recurrence_field: str,
) -> None:
    """create/update/edit 的 recurrence 输入必须统一返回稳定领域错误码。"""
    use_case, _, _ = _use_case()

    with pytest.raises(StateConflictError) as error:
        if operation == "create":
            await use_case.create_event(
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
                calendar_id=CALENDAR_ID,
                idempotency_key="recurring-create",
                values={recurrence_field: ["RRULE:FREQ=DAILY"]},
            )
        elif operation == "update":
            await use_case.create_update(
                user_id=USER_ID,
                event_id=EVENT_ID,
                changes={recurrence_field: "FREQ=DAILY"},
            )
        else:
            created = await use_case.create_update(
                user_id=USER_ID,
                event_id=EVENT_ID,
                changes={"location": "Synthetic room"},
            )
            await use_case.edit(
                user_id=USER_ID,
                proposal_id=created.proposal_id,
                expected_version=created.current_version,
                changes={recurrence_field: PROVIDER_EVENT_ID},
            )

    assert error.value.error_code == "calendar_recurring_event_unsupported"


@pytest.mark.asyncio
async def test_unknown_calendar_extension_is_not_misreported_as_recurrence() -> None:
    """普通未知扩展仍是输入错误，不能伪装成重复日程领域事实。"""
    use_case, _, _ = _use_case()

    with pytest.raises(ValueError) as error:
        await use_case.create_event(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            calendar_id=CALENDAR_ID,
            idempotency_key="unknown-calendar-extension",
            values={"conference_data": {"provider": "synthetic"}},
        )

    assert not isinstance(error.value, StateConflictError)
