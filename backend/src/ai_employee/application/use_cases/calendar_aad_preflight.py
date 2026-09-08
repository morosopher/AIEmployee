"""协调 0018 的主动 refresh、精确有界 probe 与无内容 rollout 结果，不修改日历事实。"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_employee.application.calendar_aad_digests import rollout_digest_v1
from ai_employee.application.ports.calendar import CalendarReader
from ai_employee.application.ports.oauth_refresh import (
    OAuthRefreshCoordinator,
    OAuthRefreshLease,
    OAuthRefreshProvider,
    OAuthRefreshRequest,
)
from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadArtifact,
    CalendarAadBinding,
    CalendarAadFacts,
    CalendarAadPair,
    CalendarAadRevision,
    CalendarAadRolloutError,
    create_rollout_artifact,
    verify_rollout,
)
from ai_employee.application.use_cases.sync_calendar import collect_calendar_event_pages
from ai_employee.domain.connections import ConnectionCapability


class CalendarAadPreflightStore(Protocol):
    """只读取真实 affected set 与既有 started ID，不承担第二套 OAuth closure/writer。"""

    async def read_facts(self, *, expected_revision: CalendarAadRevision) -> CalendarAadFacts:
        """从当前数据库重建精确本地可恢复性与 current expiry。"""
        ...

    async def existing_attempt(self, *, pair: CalendarAadPair, rollout_digest: str) -> UUID | None:
        """定位当前 rollout/连接唯一 started；结果合法性由共享 coordinator 判定。"""
        ...


class CalendarAadAdapters(Protocol):
    """组合根提供与普通 Calendar Worker 同源的 provider-neutral 只读与 OAuth 端口。"""

    def oauth(self, provider: str) -> OAuthRefreshProvider:
        """解析当前 owning connection 的 OAuth refresh adapter。"""
        ...

    def calendar_reader(self, pair: CalendarAadPair, access_token: str) -> CalendarReader:
        """使用当前已确认 access 构造不自动 refresh、不更改能力状态的 exact reader。"""
        ...


class CalendarAadPreflightUseCase:
    """在一个 revision lease 下按连接排序主动刷新，随后仅 probe 原 affected pairs。"""

    def __init__(
        self,
        *,
        store: CalendarAadPreflightStore,
        coordinator: OAuthRefreshCoordinator,
        adapters: CalendarAadAdapters,
        lease: OAuthRefreshLease,
        clock: Callable[[], datetime],
        step_timeout_seconds: float,
        task_timeout_seconds: float,
    ) -> None:
        """注入同一 coordinator 与两个时间预算；预算不能改变固定 900 秒安全余量。"""
        self._store, self._coordinator, self._adapters = store, coordinator, adapters
        self._lease, self._clock = lease, clock
        self._step_timeout, self._task_timeout = step_timeout_seconds, task_timeout_seconds

    async def execute(
        self, *, binding: CalendarAadBinding, existing: CalendarAadArtifact | None
    ) -> CalendarAadArtifact:
        """完成一次 no-filter preflight，异常保留真实 OAuth fence，但不写 event/cursor/marker。"""
        async with asyncio.timeout(self._task_timeout):
            return await self._execute(binding=binding, existing=existing)

    async def _execute(
        self, *, binding: CalendarAadBinding, existing: CalendarAadArtifact | None
    ) -> CalendarAadArtifact:
        """串行 refresh/readiness/probe，所有数据库短事务在下一 provider 调用前结束。"""
        await self._lease.assert_owned()
        initial = await self._store.read_facts(expected_revision="20260809_0018")
        if existing is not None:
            verify_rollout(existing, initial, now=self._clock())
        digest = rollout_digest_v1(binding.basename, binding.immutable_image_id)
        connections: dict[UUID, CalendarAadPair] = {}
        for pair in initial.pairs:
            connections.setdefault(pair.connection_id, pair)
        for pair in connections.values():
            async with asyncio.timeout(self._step_timeout):
                await self._lease.assert_owned()
                attempt = await self._store.existing_attempt(pair=pair, rollout_digest=digest)
                await self._coordinator.refresh(
                    OAuthRefreshRequest(
                        user_id=pair.user_id,
                        connection_id=pair.connection_id,
                        capability=ConnectionCapability.CALENDAR_READ,
                        source="calendar_aad_preflight",
                        rollout_digest_v1=digest,
                        attempt_id=attempt,
                    ),
                    self._adapters.oauth(pair.provider),
                    outer_lease=self._lease,
                )
        # ACK-lost closure/readiness 返回后必须读当前 rows，不能消费历史 confirmed candidate。
        current = await self._store.read_facts(expected_revision="20260809_0018")
        if current.pair_digests != initial.pair_digests:
            raise CalendarAadRolloutError("calendar_aad_affected_set_changed")
        artifact = existing or create_rollout_artifact(binding, current)
        verify_rollout(artifact, current, now=self._clock())
        for pair in current.pairs:
            async with asyncio.timeout(self._step_timeout):
                # 复用共享连接 lease 原语；它不创建 automatic started，也不交换 OAuth code。
                async with self._coordinator.explicit_recovery_lease(
                    user_id=pair.user_id, connection_id=pair.connection_id
                ) as connection_lease:
                    ready = await self._coordinator.read_current(
                        OAuthRefreshRequest(
                            user_id=pair.user_id,
                            connection_id=pair.connection_id,
                            capability=ConnectionCapability.CALENDAR_READ,
                        )
                    )

                    async def before_page() -> None:
                        """每次实际 provider page 前重读 current expiry，并证明两层 lease 仍归本调用。"""
                        facts = await self._store.read_facts(expected_revision="20260809_0018")
                        await self._lease.assert_owned()
                        await connection_lease.assert_owned()
                        verify_rollout(artifact, facts, now=self._clock())

                    reader = self._adapters.calendar_reader(pair, ready.access_token)
                    await collect_calendar_event_pages(
                        reader.initial_pages(pair.calendar_id),
                        scope_key=pair.calendar_id,
                        before_page=before_page,
                    )
        await self._lease.assert_owned()
        verify_rollout(
            artifact,
            await self._store.read_facts(expected_revision="20260809_0018"),
            now=self._clock(),
        )
        return artifact
