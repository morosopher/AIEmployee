"""验证当前 credential expiry 只收紧 rollout 与冻结双阶段迁移 guard。"""

import asyncio
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError
from tests.integration.alembic_commands import run_alembic_upgrade
from tests.integration.operations.test_calendar_aad_0019_preflight import (
    BASENAME,
    IMAGE,
    NOW,
    FakeAdapters,
    run_fixture,
    seed_pair,
)
from tests.integration.operations.test_calendar_aad_0019_preflight import (
    aad_oauth as aad_oauth,  # noqa: PLC0414 - 显式re-export让pytest发现本模块fixture。
)
from tests.integration.operations.test_calendar_aad_0019_preflight import (
    aad_source as aad_source,  # noqa: PLC0414 - 显式re-export让pytest发现本模块fixture。
)
from tests.integration.operations.test_calendar_aad_0019_preflight import (
    isolated_database as isolated_database,  # noqa: PLC0414 - 保持本模块生命周期fixture覆盖。
)
from tests.integration.operations.test_calendar_aad_0019_preflight import (
    migrated_database as migrated_database,  # noqa: PLC0414 - 保持本模块生命周期fixture覆盖。
)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [None, "deadline", "artifact"])
async def test_calendar_aad_backup_guard_holds_lease_and_freezes_artifact(
    aad_oauth, tmp_path, drift
):
    """真实 guard 跨备份 producer 持 lease；到期或原 artifact 变化均不得发布任何备份。"""
    from ai_employee.cli.calendar_aad_backup_0019 import run_guarded_backup
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        calendar_aad_rollout_lease,
    )

    sessions, _, _, _ = aad_oauth
    await seed_pair(sessions)
    await run_fixture(aad_oauth, tmp_path)
    clock = [NOW]
    produced = []
    artifact_path = tmp_path / f"{BASENAME}.calendar-aad-preflight.json"

    async def producer(path):
        """仅替换 dump/encrypt 外部进程，数据库 guard、文件发布和锁竞争都走产品实现。"""

        def contend():
            with (
                pytest.raises(CalendarAadRolloutError) as failure,
                calendar_aad_rollout_lease(sessions.engine.url),
            ):
                pytest.fail("backup producer lost the revision lease")
            assert failure.value.error_code == "calendar_aad_rollout_locked"

        await asyncio.to_thread(contend)
        assert path.parent != tmp_path
        path.write_bytes(b"synthetic-encrypted-dump")
        produced.append(path)
        if drift == "deadline":
            clock[0] += timedelta(minutes=45)
        elif drift == "artifact":
            payload = json.loads(artifact_path.read_text())
            payload["earliest_token_expires_at"] = "2030-01-01T02:00:00Z"
            payload["rollout_deadline"] = "2030-01-01T01:45:00Z"
            artifact_path.write_text(json.dumps(payload))

    kwargs = {
        "sessions": sessions,
        "backup_directory": tmp_path,
        "basename": BASENAME,
        "immutable_image_id": IMAGE,
        "clock": lambda: clock[0],
        "producer": producer,
    }
    if drift is None:
        await run_guarded_backup(**kwargs)
        target = tmp_path / f"{BASENAME}.dump.enc"
        assert target.read_bytes() == b"synthetic-encrypted-dump"
        assert target.stat().st_mode & 0o777 == 0o600
        assert (tmp_path / f"{BASENAME}.dump.enc.sha256").read_text() == (
            f"{hashlib.sha256(target.read_bytes()).hexdigest()}  {BASENAME}.dump.enc\n"
        )
    else:
        with pytest.raises(CalendarAadRolloutError) as failure:
            await run_guarded_backup(**kwargs)
        assert failure.value.error_code == (
            "calendar_aad_rollout_deadline_exceeded"
            if drift == "deadline"
            else "calendar_aad_artifact_invalid"
        )
        assert not list(tmp_path.glob("*.dump.enc*"))
    assert len(produced) == 1 and not produced[0].parent.exists()


@pytest.mark.asyncio
async def test_calendar_aad_backup_cancellation_waits_for_producer_cleanup(aad_oauth, tmp_path):
    """取消不会遗留 producer、明文临时目录或占住 revision session。"""
    from ai_employee.cli.calendar_aad_backup_0019 import run_guarded_backup
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        async_calendar_aad_rollout_lease,
    )

    sessions, _, _, _ = aad_oauth
    await run_fixture(aad_oauth, tmp_path)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def producer(path):
        """模拟仍活跃的 dump；取消必须等待其 finally 后才清理整个临时目录。"""
        try:
            path.write_bytes(b"synthetic-partial-dump")
            entered.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    task = asyncio.create_task(
        run_guarded_backup(
            sessions=sessions,
            backup_directory=tmp_path,
            basename=BASENAME,
            immutable_image_id=IMAGE,
            clock=lambda: NOW,
            producer=producer,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert [path.name for path in tmp_path.iterdir()] == [f"{BASENAME}.calendar-aad-preflight.json"]
    async with async_calendar_aad_rollout_lease(sessions.engine.url) as lease:
        await lease.assert_owned()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage,foreign_replacement",
    (("body", False), ("body", True), ("final_exit", False)),
)
async def test_calendar_aad_group_compensation_keeps_real_revision_lease(
    aad_oauth, tmp_path, monkeypatch, failure_stage, foreign_replacement
):
    """真实revision锁竞争和三件套文件补偿区分body失效与final-exit特许窗口。

    publisher只提供本例合成dump/manifest；发布、精确receipt删除、原guard和PG lease均
    执行产品实现。body截止线失效后必须持锁收齐补偿；外来替换保留且其他成员仍撤回。
    """
    import os
    from contextlib import asynccontextmanager

    from ai_employee.cli import calendar_aad_backup_0019 as backup
    from ai_employee.infrastructure.db.postgres_backup_manifest import (
        OPERATIONS_EXECUTABLES,
        BackupManifest,
        discard_backup_publication,
        publish_backup_group,
        write_backup_group,
    )
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        calendar_aad_rollout_lease,
    )

    sessions, _, _, _ = aad_oauth
    await seed_pair(sessions)
    await run_fixture(aad_oauth, tmp_path)
    staging = tmp_path / "owned-staging"
    staging.mkdir(mode=0o700)
    target = tmp_path / f"{BASENAME}.dump.enc"
    clock = [NOW]
    observations = []
    receipts = []
    first_failures = []
    exit_failure = OSError("synthetic final revision exit failure")
    original_lease = backup.async_calendar_aad_rollout_lease
    original_verify = backup.CalendarAadCurrentGuard.verify

    @asynccontextmanager
    async def observed_lease(url):
        """只在真实物理lease退出后记录边界；final-exit首错不假称仍持有该锁。"""
        try:
            async with original_lease(url) as lease:
                yield lease
        finally:
            observations.append("lease_exited")
        if failure_stage == "final_exit":
            raise exit_failure

    async def observe_guard(guard):
        try:
            return await original_verify(guard)
        except CalendarAadRolloutError as error:
            first_failures.append(error)
            raise

    monkeypatch.setattr(backup, "async_calendar_aad_rollout_lease", observed_lease)
    monkeypatch.setattr(backup.CalendarAadCurrentGuard, "verify", observe_guard)

    class Publisher:
        """采用真实三件套原语；receipt返回后才越过截止线，精确命中最后body guard。"""

        staging_root = staging

        async def publish(self, staged, target, guard):
            await guard.verify()

            def publish_files():
                data = staged.read_bytes()
                write_backup_group(
                    staged,
                    BackupManifest(
                        staged.name,
                        "2030-01-01T00:00:00.000000Z",
                        "17.6",
                        "20260809_0018",
                        hashlib.sha256(data).hexdigest(),
                        len(data),
                        staged.name + ".sha256",
                        IMAGE,
                        {"release_version": "0.1.0-synthetic"},
                        "a" * 64,
                        {name: "b" * 64 for name in OPERATIONS_EXECUTABLES},
                    ),
                )
                return publish_backup_group(staged, target.parent)

            receipt = await asyncio.to_thread(publish_files)
            assert len(receipt.members) == 3
            receipts.append(receipt)
            observations.append("published")
            if foreign_replacement:
                foreign = tmp_path / "foreign-member"
                foreign.write_bytes(b"synthetic-foreign-file")
                foreign.chmod(0o600)
                os.replace(foreign, target)
            if failure_stage == "body":
                clock[0] = NOW + timedelta(minutes=45)
            return receipt

        async def discard(self, receipt):
            def discard_files():
                """物理PG竞争结果与文件最终状态共同证明补偿时机，不从调用计数猜测锁。"""
                if failure_stage == "body":
                    with (
                        pytest.raises(CalendarAadRolloutError) as caught,
                        calendar_aad_rollout_lease(sessions.engine.url),
                    ):
                        pytest.fail("body compensation released the revision lease")
                    assert caught.value.error_code == "calendar_aad_rollout_locked"
                else:
                    with calendar_aad_rollout_lease(sessions.engine.url) as lease:
                        lease.verify_owned()
                observations.append("discard")
                try:
                    discard_backup_publication(receipt)
                finally:
                    # marker与checksum必须收齐；外来dump不能借本次receipt删除。
                    assert not target.with_name(target.name + ".manifest.json").exists()
                    assert not target.with_name(target.name + ".sha256").exists()
                    assert target.exists() is foreign_replacement

            await asyncio.to_thread(discard_files)

    async def producer(path):
        await asyncio.to_thread(path.write_bytes, b"synthetic-encrypted-dump")
        await asyncio.to_thread(path.chmod, 0o600)

    with pytest.raises(CalendarAadRolloutError if failure_stage == "body" else OSError) as caught:
        await backup.run_guarded_backup(
            sessions=sessions,
            backup_directory=tmp_path,
            basename=BASENAME,
            immutable_image_id=IMAGE,
            clock=lambda: clock[0],
            producer=producer,
            publisher=Publisher(),
        )
    assert len(receipts) == 1
    if failure_stage == "body":
        assert caught.value is first_failures[0]
        assert caught.value.error_code == "calendar_aad_rollout_deadline_exceeded"
        assert observations == ["published", "discard", "lease_exited"]
    else:
        assert caught.value is exit_failure
        assert observations == ["published", "lease_exited", "discard"]
    if foreign_replacement:
        # precheck已知为外来成员时按既有协议跳过；只有捕获后身份漂移才产生cleanup错误。
        assert caught.value.__cause__ is None
        assert target.read_bytes() == b"synthetic-foreign-file"
    else:
        assert not list(tmp_path.glob("*.dump.enc*"))
    async with original_lease(sessions.engine.url) as lease:
        await lease.assert_owned()


@pytest.mark.asyncio
async def test_calendar_aad_migration_guard_rejects_changed_original_artifact(aad_oauth, tmp_path):
    """两个 frozen phase 必须绑定同一原始 artifact；同 image 的新合法 JSON 也不是延期许可。"""
    from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadBinding
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        CalendarAadArtifactFile,
        CalendarAadMigrationArtifactGuard,
        calendar_aad_rollout_lease,
    )

    sessions, _, _, config = aad_oauth
    await seed_pair(sessions)
    await run_fixture(aad_oauth, tmp_path)
    path = tmp_path / f"{BASENAME}.calendar-aad-preflight.json"

    def migrate():
        with calendar_aad_rollout_lease(sessions.engine.url) as lease:

            class TamperingGuard(CalendarAadMigrationArtifactGuard):
                """只在两个真实 phase 之间替换文件，始终委托产品 guard 验证数据库。"""

                def verify(self, *, connection, phase):
                    if phase == "before_commit":
                        payload = json.loads(path.read_text())
                        payload["earliest_token_expires_at"] = "2030-01-01T02:00:00Z"
                        payload["rollout_deadline"] = "2030-01-01T01:45:00Z"
                        path.write_text(json.dumps(payload))
                    return super().verify(connection=connection, phase=phase)

            config.attributes["calendar_aad_0019_guard"] = TamperingGuard(
                artifact_file=CalendarAadArtifactFile(
                    tmp_path, CalendarAadBinding(BASENAME, IMAGE)
                ),
                lease=lease,
                clock=lambda: NOW,
            )
            run_alembic_upgrade(config, "20260809_0019")

    with pytest.raises(CalendarAadRolloutError) as failure:
        await asyncio.to_thread(migrate)
    assert failure.value.error_code == "calendar_aad_artifact_invalid"
    async with sessions() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version")) == "20260809_0018"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("later_minutes, expected_minutes", [(30, 15), (120, 45)])
async def test_calendar_aad_guard_reloads_current_expiry_without_extending_original(
    aad_oauth, tmp_path, later_minutes, expected_minutes
):
    """新合法刷新更短立即收紧、更长不能延长 artifact，历史 deadline candidate 不作输入。"""
    from ai_employee.application.use_cases.calendar_aad_rollout import verify_rollout
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        SqlAlchemyCalendarAadPreflightRepository,
    )

    sessions, _, coordinator, _ = aad_oauth
    await seed_pair(sessions)
    artifact = await run_fixture(aad_oauth, tmp_path)
    from ai_employee.application.ports.oauth import OAuthTokenSet
    from ai_employee.application.ports.oauth_refresh import OAuthRefreshRequest
    from ai_employee.domain.connections import ConnectionCapability
    from tests.integration.m2.test_credential_rotation_repository import (
        CONNECTION_ID,
        SCOPES,
        USER_ID,
        FakeRefreshProvider,
    )

    await coordinator.refresh(
        OAuthRefreshRequest(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            capability=ConnectionCapability.CALENDAR_READ,
        ),
        FakeRefreshProvider(
            OAuthTokenSet("synthetic-later-access", None, later_minutes * 60, SCOPES)
        ),
    )
    repository = SqlAlchemyCalendarAadPreflightRepository(sessions)
    facts = await repository.read_facts(expected_revision="20260809_0018")
    assert verify_rollout(artifact, facts, now=NOW) == NOW + timedelta(minutes=expected_minutes)
    with pytest.raises(CalendarAadRolloutError) as failure:
        verify_rollout(artifact, facts, now=NOW + timedelta(minutes=expected_minutes))
    assert failure.value.error_code == "calendar_aad_rollout_deadline_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_at_commit", [False, True])
async def test_calendar_aad_real_artifact_guard_runs_both_frozen_migration_phases(
    aad_oauth, tmp_path, cross_at_commit
):
    """真实 guard 通过 Config attribute 注入；final 阶段失败必须回滚 DDL、版本与 marker。"""
    from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadBinding
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        CalendarAadArtifactFile,
        CalendarAadMigrationArtifactGuard,
        calendar_aad_rollout_lease,
    )

    sessions, _, _, config = aad_oauth
    await seed_pair(sessions)
    await run_fixture(aad_oauth, tmp_path)
    calls = []

    def migrate():
        """沿官方三锁 helper 迁移，真实 guard 同步读取同一外层 migration Connection。"""
        with calendar_aad_rollout_lease(sessions.engine.url) as lease:

            class RecordingGuard(CalendarAadMigrationArtifactGuard):
                """只观察真实 guard 的冻结调用，不替代其任何验证。"""

                def verify(self, *, connection, phase):
                    """通过注入 UTC 时钟复现 mutation 后、最终 commit 前越过截止线。"""
                    calls.append((connection, phase))
                    self._clock = lambda: (
                        NOW + timedelta(minutes=45)
                        if cross_at_commit and phase == "before_commit"
                        else NOW
                    )
                    return super().verify(connection=connection, phase=phase)

            guard = RecordingGuard(
                artifact_file=CalendarAadArtifactFile(
                    tmp_path, CalendarAadBinding(BASENAME, IMAGE)
                ),
                lease=lease,
                clock=lambda: NOW,
            )
            config.attributes["calendar_aad_0019_guard"] = guard
            run_alembic_upgrade(config, "20260809_0019")

    if cross_at_commit:
        with pytest.raises(CalendarAadRolloutError) as failure:
            await asyncio.to_thread(migrate)
        assert failure.value.error_code == "calendar_aad_rollout_deadline_exceeded"
    else:
        await asyncio.to_thread(migrate)
    assert [phase for _, phase in calls] == ["before_mutation", "before_commit"]
    assert calls[0][0] is calls[1][0]
    async with sessions() as session:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == ("20260809_0018" if cross_at_commit else "20260809_0019")
        marker = await session.scalar(text("SELECT last_error_code FROM sync_cursors"))
        assert marker == (None if cross_at_commit else "calendar_event_resync_required")
        version_column = await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='calendar_events' AND column_name='description_aad_version')"
            )
        )
        assert version_column is (not cross_at_commit)


@pytest.mark.asyncio
async def test_calendar_aad_probe_deadline_crossing_and_zero_state_drift_fail_closed(
    aad_oauth, tmp_path
):
    """零分支也要重证 exact set；非空 probe 后越界不能发布成功文件。"""
    from ai_employee.application.use_cases.calendar_aad_rollout import verify_rollout
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        SqlAlchemyCalendarAadPreflightRepository,
    )

    sessions, _, _, _ = aad_oauth
    zero = await run_fixture(aad_oauth, tmp_path)
    (tmp_path / f"{BASENAME}.calendar-aad-preflight.json").unlink()
    await seed_pair(sessions)
    facts = await SqlAlchemyCalendarAadPreflightRepository(sessions).read_facts(
        expected_revision="20260809_0018"
    )
    with pytest.raises(CalendarAadRolloutError) as failure:
        verify_rollout(zero, facts, now=NOW)
    assert failure.value.error_code == "calendar_aad_affected_set_changed"
    clock = [NOW]
    adapters = FakeAdapters()

    async def advance():
        """只替换 injected clock，不依赖真实等待或供应商时间。"""
        clock[0] += timedelta(minutes=45)

    adapters.reader.before_page = advance
    with pytest.raises(CalendarAadRolloutError) as failure:
        await run_fixture(aad_oauth, tmp_path, adapters=adapters, clock=lambda: clock[0])
    assert failure.value.error_code == "calendar_aad_rollout_deadline_exceeded"
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["image", "zero_timestamp", "extra", "digest", "pair_count"])
async def test_calendar_aad_artifact_tampering_is_rejected_before_provider(
    aad_oauth, tmp_path, mutation
):
    """artifact 闭合 schema、绑定与零状态均 fail closed，不接受手填 deadline/image 豁免。"""
    await run_fixture(aad_oauth, tmp_path)
    path = tmp_path / f"{BASENAME}.calendar-aad-preflight.json"
    payload = json.loads(path.read_text())
    updates = {
        "image": {"immutable_image_id": "sha256:" + "f" * 64},
        "zero_timestamp": {"rollout_deadline": "2030-01-01T00:45:00Z"},
        "extra": {"operator_deadline": "2030-01-01T02:00:00Z"},
        "digest": {"rollout_digest_v1": "0" * 64},
        "pair_count": {"affected_pair_count": 1},
    }
    payload.update(updates[mutation])
    path.write_text(json.dumps(payload))
    adapters = FakeAdapters()
    with pytest.raises(CalendarAadRolloutError) as failure:
        await run_fixture(aad_oauth, tmp_path, adapters=adapters)
    assert failure.value.error_code == "calendar_aad_artifact_invalid"
    assert adapters.provider.calls == 0


@pytest.mark.asyncio
async def test_calendar_aad_dedicated_migration_composes_real_three_lock_authority(
    aad_oauth, tmp_path, monkeypatch
):
    """产品无参迁移入口注入真实 guard；typed 三锁、授权 token 与 frozen env 保持真实。"""
    from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadBinding
    from ai_employee.cli.calendar_aad_migrate_0019 import run_migration
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        CalendarAadArtifactFile,
        CalendarAadMigrationArtifactGuard,
    )

    sessions, _, _, _ = aad_oauth
    await seed_pair(sessions)
    await run_fixture(aad_oauth, tmp_path)
    phases = []
    original = CalendarAadMigrationArtifactGuard.verify

    def recording(self, *, connection, phase):
        """只记录实际调用，委托真实同步 guard 在同一 Connection 上完成验证。"""
        phases.append((id(self), id(connection), phase))
        return original(self, connection=connection, phase=phase)

    monkeypatch.setattr(CalendarAadMigrationArtifactGuard, "verify", recording)

    def migrate():
        """为本例 synthetic target 建 owner engines，不访问保留 anchor 或生产数据库。"""
        url = sessions.engine.url.set(drivername="postgresql+psycopg")
        management = create_engine(
            url.set(database="postgres"), poolclass=NullPool, hide_parameters=True
        )
        target = create_engine(url, poolclass=NullPool, hide_parameters=True)
        try:
            run_migration(
                management_engine=management,
                target_engine=target,
                artifact_file=CalendarAadArtifactFile(
                    tmp_path, CalendarAadBinding(BASENAME, IMAGE)
                ),
                clock=lambda: NOW,
            )
        finally:
            target.dispose()
            management.dispose()

    await asyncio.to_thread(migrate)
    assert len(phases) == 2
    assert phases[0][:2] == phases[1][:2]
    assert [entry[2] for entry in phases] == ["before_mutation", "before_commit"]
    async with sessions() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version")) == "20260809_0019"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("later_minutes", [10, 30])
async def test_calendar_aad_ack_lost_then_legal_refresh_uses_shorter_current_expiry(
    aad_oauth, tmp_path, monkeypatch, later_minutes
):
    """真实 confirmed commit 丢 ACK/lease 后合法刷新抢先提交；当前截止线立即收紧。

    只在实际 after_commit 制造故障。后续刷新仍走共享 coordinator、完整 CAS 与版本化
    审计，原确认事实仍闭合；禁止将历史 45 分钟 candidate 用作当前期限。
    """
    from sqlalchemy import event, select

    from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
    from ai_employee.application.ports.oauth import OAuthTokenSet
    from ai_employee.application.ports.oauth_refresh import OAuthRefreshRequest
    from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError
    from ai_employee.domain.connections import ConnectionCapability
    from ai_employee.infrastructure.db.models.tasks import AuditEventModel
    from ai_employee.infrastructure.db.repositories.credential_rotation import (
        SqlAlchemyCredentialRotationRepository,
    )
    from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
        PostgreSQLOAuthRefreshLease,
        SqlAlchemyOAuthRefreshCoordinator,
    )
    from tests.integration.m2.test_credential_rotation_repository import (
        CONNECTION_ID,
        SCOPES,
        USER_ID,
        FakeRefreshProvider,
    )

    sessions, cipher, coordinator, _ = aad_oauth
    await seed_pair(sessions)
    original_confirm = SqlAlchemyCredentialRotationRepository.confirm
    original_assert = PostgreSQLOAuthRefreshLease.assert_owned
    original_result = coordinator.read_result
    leases = []
    lost = []
    later_provider = FakeRefreshProvider(
        OAuthTokenSet("synthetic-later-access", None, later_minutes * 60, SCOPES)
    )

    async def capture(lease):
        """观察真实 lease；故障只在旧 token 已确实提交之后触发。"""
        await original_assert(lease)
        leases.append(lease)

    async def confirm(self, claim, tokens, *, completed_at):
        """只给 rollout 的 confirm 安装一次 after_commit，不影响后续合法普通刷新。"""
        result = await original_confirm(self, claim, tokens, completed_at=completed_at)
        if claim.request.source == "calendar_aad_preflight":

            def lost_ack(session):
                """关闭原物理连接，使另一个 coordinator 合法拿到连接 lease。"""
                leases[-1].connection.sync_connection.invalidate()
                lost.append(True)
                raise RuntimeError("synthetic acknowledgement lost")

            event.listen(self.session.sync_session, "after_commit", lost_ack, once=True)
        return result

    async def read_result_after_later_refresh(**kwargs):
        """原调用第一次只读核对前，另一个真实 coordinator 提交较短的 access expiry。"""
        assert lost == [True]
        other = SqlAlchemyOAuthRefreshCoordinator(
            session_factory=sessions,
            cipher=cipher,
            identity=OAuthRefreshIdentity(bytes(range(32)), key_version=7),
            clock=lambda: NOW,
        )
        await other.refresh(
            OAuthRefreshRequest(
                user_id=USER_ID,
                connection_id=CONNECTION_ID,
                capability=ConnectionCapability.CALENDAR_READ,
            ),
            later_provider,
        )
        return await original_result(**kwargs)

    monkeypatch.setattr(PostgreSQLOAuthRefreshLease, "assert_owned", capture)
    monkeypatch.setattr(SqlAlchemyCredentialRotationRepository, "confirm", confirm)
    monkeypatch.setattr(coordinator, "read_result", read_result_after_later_refresh)
    adapters = FakeAdapters()
    if later_minutes == 10:
        with pytest.raises(CalendarAadRolloutError) as failure:
            await run_fixture(aad_oauth, tmp_path, adapters=adapters)
        assert failure.value.error_code == "calendar_aad_rollout_deadline_exceeded"
        assert adapters.reader.scopes == []
        assert not list(tmp_path.iterdir())
    else:
        artifact = await run_fixture(aad_oauth, tmp_path, adapters=adapters)
        assert artifact.rollout_deadline == NOW + timedelta(minutes=15)
        assert adapters.accesses == ["synthetic-later-access"]
    assert lost == [True]
    assert adapters.provider.calls == later_provider.calls == 1
    async with sessions() as session:
        rows = list(
            await session.scalars(
                select(AuditEventModel).where(
                    AuditEventModel.event_type == "oauth.refresh_confirmed"
                )
            )
        )
        assert len(rows) == 2
        historical = next(
            row for row in rows if row.event_metadata["source"] == "calendar_aad_preflight"
        )
        assert (
            historical.event_metadata["rollout_deadline_candidate"] == "2030-01-01T00:45:00.000000Z"
        )
