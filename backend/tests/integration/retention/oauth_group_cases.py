"""以真实 app/retention 会话验证 OAuth 完整事件组，不改变 writer、ACL 或测试现场。"""

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.credential_rotation import (
    OAuthRefreshAuditRecord,
    OAuthRefreshResultV1,
    parse_oauth_refresh_result,
)
from ai_employee.application.ports.oauth_refresh import OAuthRefreshError
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.repositories import oauth_lifecycle
from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import find_refresh_result
from ai_employee.infrastructure.db.session import build_session_factory
from tests.integration.m2.test_credential_rotation_repository import (
    FakeRefreshProvider,
    token_response,
)
from tests.integration.retention.oauth_cases import (
    _assert_privileges,
    _events,
    _prepare_writer,
    _seed_state,
)
from tests.unit.application.test_oauth_refresh_coordinator import result_event


def _mixed_result(
    original: OAuthRefreshAuditRecord,
    replacement: OAuthRefreshAuditRecord,
    *,
    kind: str,
    created_at: datetime,
) -> OAuthRefreshAuditRecord:
    """把独立合成 union 模板绑定到真实 writer 的历史字段，返回尚未写入的冲突结果。

    只复制内容无关的审计绑定；调用方必须先经共享严格 parser 验证，不能把错误 fixture
    当作产品 RED。confirmed 使用 automatic 的原代际，不能误用 replacement 的目标代际。
    """
    template = result_event(kind)
    metadata = {
        name: replacement.metadata.get(name, value) for name, value in template.metadata.items()
    }
    metadata["result_code"] = template.metadata["result_code"]
    if kind == "confirmed":
        for name in ("source", "source_revision", "target_revision", "rollout_digest_v1"):
            metadata[name] = original.metadata[name]
        for name in ("source_generation", "pre_generation", "post_generation"):
            metadata[name] = original.metadata["fence_generation"]
        metadata["refresh_token_disposition"] = "different"
        metadata["refresh_identity_changed"] = True
        metadata["rollout_deadline_candidate"] = None
    else:
        metadata["requested_capabilities"] = ["mail.send"]
    return replace(
        template,
        event_id=replacement.event_id + 1,
        user_id=original.user_id,
        created_at=created_at,
        metadata=metadata,
    )


async def assert_mixed_closing_fresh_reread(
    *,
    app_url: str,
    retention_url: str,
    conflict_kind: str,
    conflict_timing: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选预读→app 提交混合 closing→cleanup 取锁→fresh reread，整组必须零删除。

    Args:
        app_url: 官方 fixture 已建立的 app 登录。
        retention_url: 同库冻结的 retention 登录，不增加审计 UPDATE 权限。
        conflict_kind: confirmed 或 unsatisfied，均与真实 replacement 形成适用关闭冲突。
        conflict_timing: 旧、cutoff 当刻或更晚；年龄不能掩盖关闭歧义。
        monkeypatch: 只暂停/记录实际函数边界，不替换真实数据库锁和结果解析。
    """
    app, retention = build_session_factory(app_url), build_session_factory(retention_url)
    try:
        state = await _seed_state(app)
        await (await _prepare_writer(state, "replacement"))()
        initial = await _events(app, state)
        automatic, recovery, replacement = initial
        cutoff = max(row.created_at for row in initial) + timedelta(days=1)
        conflict = _mixed_result(
            automatic,
            replacement,
            kind=conflict_kind,
            created_at={
                "expired": replacement.created_at + timedelta(microseconds=1),
                "at_cutoff": cutoff,
                "after_cutoff": cutoff + timedelta(microseconds=1),
            }[conflict_timing],
        )
        assert (
            parse_oauth_refresh_result(
                conflict,
                automatic=automatic,
                recovery=recovery,
                user_id=state.user_id,
                connection_id=state.connection_id,
                key_version=1,
            )
            is not None
        )
        with pytest.raises(OAuthRefreshError) as raised:
            find_refresh_result(
                (*initial, conflict),
                user_id=state.user_id,
                connection_id=state.connection_id,
                attempt_id=UUID(
                    str(
                        recovery.metadata["oauth_attempt_id"]
                        if conflict_kind == "unsatisfied"
                        else automatic.metadata["refresh_attempt_id"]
                    )
                ),
                key_version=1,
                recovery=conflict_kind == "unsatisfied",
            )
        assert raised.value.error_code == "oauth_credential_state_conflict"
        timeline: list[str] = []
        original_groups = oauth_lifecycle._closed_groups
        original_lock = oauth_lifecycle.lock_oauth_cleanup_identity

        def record_group_read(
            records: tuple[OAuthRefreshAuditRecord, ...],
            *,
            user_id: UUID,
            connection_id: UUID,
            key_version: int,
            cutoff: datetime,
        ) -> tuple[oauth_lifecycle._ClosedGroup, ...]:
            """调用真实 parser，分别记录候选与锁后完整事实重读。"""
            groups = original_groups(
                records,
                user_id=user_id,
                connection_id=connection_id,
                key_version=key_version,
                cutoff=cutoff,
            )
            timeline.append("candidate-read" if not timeline else "fresh-reread")
            return groups

        async def insert_before_lock(
            session: AsyncSession, *, user_id: UUID, connection_id: UUID
        ) -> oauth_lifecycle.OAuthCleanupIdentity | None:
            """保留 app INSERT 和真实表锁；不修改或删除已有审计来制造输入。"""
            assert timeline == ["candidate-read"]
            async with app.begin() as writer:
                writer.add(
                    AuditEventModel(
                        user_id=user_id,
                        task_id=None,
                        event_type=conflict.event_type,
                        actor_type="system",
                        actor_id=None,
                        event_metadata=dict(conflict.metadata),
                        created_at=conflict.created_at,
                    )
                )
            timeline.append("writer-committed")
            identity = await original_lock(session, user_id=user_id, connection_id=connection_id)
            timeline.append("cleanup-locked")
            return identity

        monkeypatch.setattr(oauth_lifecycle, "_closed_groups", record_group_read)
        monkeypatch.setattr(oauth_lifecycle, "lock_oauth_cleanup_identity", insert_before_lock)
        deleted = await oauth_lifecycle.OAuthLifecycleCleanup(retention).clean_user(
            user_id=state.user_id, cutoff=cutoff, batch_size=1
        )
        assert timeline == ["candidate-read", "writer-committed", "cleanup-locked", "fresh-reread"]
        assert deleted == 0
        remaining = await _events(app, state)
        assert len(remaining) == len(initial) + 1
        assert all(before == after for before, after in zip(initial, remaining, strict=False))
        await _assert_privileges(app, retention)
    finally:
        await retention.dispose()
        await app.dispose()


async def assert_single_group_work_is_bounded(
    *, app_url: str, retention_url: str, group_count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只删一组时，真实锁内读取/解析/互斥集合不随同连接其他 attempt 历史增长。

    记录真实 SELECT 的 DBAPI 行数、共享 parser 次数和实际 audit mutex ID；它们是
    工作量证据，不是数据库耗时基准。完整关联组没有 cutoff 或 LIMIT 截断。
    """
    app, retention = build_session_factory(app_url), build_session_factory(retention_url)
    inside_lock = False
    read_rows: list[int] = []
    parser_calls = 0
    mutex_ids: list[int] = []
    parsed_ids: list[tuple[int, ...]] = []

    def observe_rows(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        """仅保留实际行数；不保存 SQL 参数、审计 metadata 或凭据字段。"""
        del connection, parameters, context, executemany
        if (
            inside_lock
            and statement.lstrip().upper().startswith("SELECT")
            and "FROM audit_events" in statement
        ):
            count = getattr(cursor, "rowcount", None)
            assert isinstance(count, int) and count >= 0
            read_rows.append(count)

    def observe_commit(connection: object) -> None:
        """提交后结束锁内计数，不把下一次无锁候选发现计入该短事务。"""
        del connection
        nonlocal inside_lock
        inside_lock = False

    event.listen(retention.engine.sync_engine, "after_cursor_execute", observe_rows)
    event.listen(retention.engine.sync_engine, "commit", observe_commit)
    try:
        state = await _seed_state(app)
        other = await _seed_state(app)
        provider = FakeRefreshProvider(token_response())
        for _ in range(group_count):
            await state.coordinator.refresh(state.request(), provider)
        await other.coordinator.refresh(other.request(), other.provider)
        initial = await _events(app, state)
        other_before = await _events(app, other)
        assert len(initial) == 2 * group_count
        target_ids = tuple(row.event_id for row in initial[:2])
        original_lock = oauth_lifecycle.lock_oauth_cleanup_identity
        original_mutex = oauth_lifecycle.lock_refresh_audit_event
        original_parse = oauth_lifecycle.parse_oauth_refresh_result
        original_groups = oauth_lifecycle._closed_groups

        async def observe_identity(
            session: AsyncSession, *, user_id: UUID, connection_id: UUID
        ) -> oauth_lifecycle.OAuthCleanupIdentity | None:
            """只有真实两表锁和身份读取完成后，才开始记录该组的锁内工作。"""
            nonlocal inside_lock
            identity = await original_lock(session, user_id=user_id, connection_id=connection_id)
            inside_lock = identity is not None
            return identity

        async def observe_mutex(session: AsyncSession, *, event_id: int) -> None:
            """记录实际取得的共同审计 mutex，不能只观察某条 SQL 文本。"""
            await original_mutex(session, event_id=event_id)
            if inside_lock:
                mutex_ids.append(event_id)

        def observe_parse(
            record: OAuthRefreshAuditRecord,
            *,
            automatic: OAuthRefreshAuditRecord,
            recovery: OAuthRefreshAuditRecord | None = None,
            user_id: UUID,
            connection_id: UUID,
            key_version: int,
        ) -> OAuthRefreshResultV1 | None:
            """保留真实 parser，只计当前表锁事务的调用，不以 mock 值绕过校验。"""
            nonlocal parser_calls
            if inside_lock:
                parser_calls += 1
            return original_parse(
                record,
                automatic=automatic,
                recovery=recovery,
                user_id=user_id,
                connection_id=connection_id,
                key_version=key_version,
            )

        def observe_group(
            records: tuple[OAuthRefreshAuditRecord, ...],
            *,
            user_id: UUID,
            connection_id: UUID,
            key_version: int,
            cutoff: datetime,
        ) -> tuple[oauth_lifecycle._ClosedGroup, ...]:
            """比较锁后真正交给 parser 的标识集合，确保没有混入其他 attempt。"""
            if inside_lock:
                parsed_ids.append(tuple(row.event_id for row in records))
            return original_groups(
                records,
                user_id=user_id,
                connection_id=connection_id,
                key_version=key_version,
                cutoff=cutoff,
            )

        monkeypatch.setattr(oauth_lifecycle, "lock_oauth_cleanup_identity", observe_identity)
        monkeypatch.setattr(oauth_lifecycle, "lock_refresh_audit_event", observe_mutex)
        monkeypatch.setattr(oauth_lifecycle, "parse_oauth_refresh_result", observe_parse)
        monkeypatch.setattr(oauth_lifecycle, "_closed_groups", observe_group)
        assert (
            await oauth_lifecycle.OAuthLifecycleCleanup(retention).clean_user(
                user_id=state.user_id,
                cutoff=max(row.created_at for row in initial) + timedelta(days=1),
                batch_size=1,
            )
            == 1
        )
        metrics = (tuple(read_rows), parser_calls, len(mutex_ids))
        assert read_rows and sum(read_rows) <= 4 and parser_calls <= 2 and len(mutex_ids) == 1, (
            metrics
        )
        assert mutex_ids == [target_ids[0]]
        assert parsed_ids == [target_ids]
        remaining = await _events(app, state)
        assert len(remaining) == len(initial) - 2
        assert all(before == after for before, after in zip(initial[2:], remaining, strict=True))
        other_after = await _events(app, other)
        assert len(other_after) == len(other_before)
        assert all(before == after for before, after in zip(other_before, other_after, strict=True))
        assert provider.calls == group_count
        await _assert_privileges(app, retention)
    finally:
        event.remove(retention.engine.sync_engine, "after_cursor_execute", observe_rows)
        event.remove(retention.engine.sync_engine, "commit", observe_commit)
        await retention.dispose()
        await app.dispose()
