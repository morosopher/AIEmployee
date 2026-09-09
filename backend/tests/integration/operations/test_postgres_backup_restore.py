"""验证普通备份三件套、恢复隔离与跨进程维护事实；仅使用可抛弃合成目标。"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import json
import os
import runpy
import subprocess
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.config import Config
from sqlalchemy import URL, create_engine, text
from sqlalchemy.pool import NullPool

from ai_employee.infrastructure.db.alembic import set_alembic_database_url
from tests.integration.alembic_commands import run_alembic_upgrade

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ("20260809_0018", "20260809_0019"))
async def test_calendar_owner_facts_isolated_from_app_identity(
    empty_migration_database: URL, tmp_path: Path, revision: str, monkeypatch
) -> None:
    """应用基线拒绝revision；固定owner reader在独立RR/RO中读全套事实且不扩权。"""
    from sqlalchemy.exc import DBAPIError

    from ai_employee.cli import database_maintenance as cli
    from ai_employee.infrastructure.db.repositories import calendar_aad_preflight as repository
    from ai_employee.infrastructure.db.session import build_session_factory

    config = Config(ROOT / "backend/alembic.ini")
    set_alembic_database_url(config, empty_migration_database.render_as_string(hide_password=False))
    run_alembic_upgrade(config, revision)
    app_url = empty_migration_database.set(
        username="ai_employee_app", password="app-role-integration-password"
    )
    sessions = build_session_factory(app_url.render_as_string(hide_password=False))
    secret = tmp_path / "synthetic-owner-secret"
    secret.write_text(empty_migration_database.password or "", encoding="utf-8")
    secret.chmod(0o600)
    endpoint = cli.DatabaseEndpoint(
        empty_migration_database.host,
        empty_migration_database.port,
        empty_migration_database.username,
        empty_migration_database.database,
    )
    original = repository.read_calendar_aad_facts
    observations = []

    def observe(connection, *, expected_revision):
        """仅观察当前session、事务和权限；完整revision+pair查询仍由实际reader执行。"""
        observations.append(
            connection.execute(
                text(
                    "SELECT current_user,session_user,current_setting('transaction_read_only'),"
                    "current_setting('transaction_isolation'),current_database(),"
                    "has_table_privilege('ai_employee_app','alembic_version','SELECT')"
                )
            ).one()
        )
        return original(connection, expected_revision=expected_revision)

    try:
        with pytest.raises(DBAPIError) as denied:
            await repository.SqlAlchemyCalendarAadPreflightRepository(sessions).read_facts(
                expected_revision=revision
            )
        assert denied.value.orig.sqlstate == "42501"
        assert hasattr(cli, "CalendarAadOwnerFactsReader"), "fixed owner facts reader is absent"
        monkeypatch.setattr(repository, "read_calendar_aad_facts", observe)
        reader = cli.CalendarAadOwnerFactsReader(endpoint, secret)
        facts = await repository.SqlAlchemyCalendarAadPreflightRepository(
            sessions, facts_reader=reader
        ).read_facts(expected_revision=revision)
        assert facts.revision == revision and facts.pairs == ()
        assert sessions.engine.url.username == "ai_employee_app"
        assert observations == [
            (
                endpoint.owner_role,
                endpoint.owner_role,
                "on",
                "repeatable read",
                endpoint.database_name,
                False,
            )
        ]
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_formal_calendar_oneoffs_keep_app_business_and_owner_facts(
    empty_migration_database: URL, tmp_path: Path
) -> None:
    """正式preflight→migration→resync零分支用真实app会话；owner只负责当前事实。"""
    from ai_employee.cli.calendar_aad_0019 import run_recovery
    from ai_employee.cli.calendar_aad_preflight_0019 import run_preflight
    from ai_employee.cli.database_maintenance import CalendarAadOwnerFactsReader, DatabaseEndpoint
    from ai_employee.config import Settings
    from ai_employee.infrastructure.db.session import build_session_factory
    from tests.integration.operations.test_calendar_aad_0019_preflight import FakeAdapters

    config = Config(ROOT / "backend/alembic.ini")
    set_alembic_database_url(config, empty_migration_database.render_as_string(hide_password=False))
    run_alembic_upgrade(config, "20260809_0018")
    app_url = empty_migration_database.set(
        username="ai_employee_app", password="app-role-integration-password"
    )
    sessions = build_session_factory(app_url.render_as_string(hide_password=False))
    secret = tmp_path / "synthetic-owner-secret"
    secret.write_text(empty_migration_database.password or "", encoding="utf-8")
    secret.chmod(0o600)
    endpoint = DatabaseEndpoint(
        empty_migration_database.host,
        empty_migration_database.port,
        empty_migration_database.username,
        empty_migration_database.database,
    )
    reader = CalendarAadOwnerFactsReader(endpoint, secret)
    adapters = FakeAdapters()
    kwargs = {
        "coordinator": object(),
        "adapters": adapters,
        "backup_directory": tmp_path,
        "basename": "synthetic-formal-oneoff",
        "immutable_image_id": "sha256:" + "a" * 64,
        "clock": lambda: datetime(2030, 1, 1, tzinfo=UTC),
        "facts_reader": reader,
    }
    try:
        artifact = await run_preflight(sessions=sessions, **kwargs)
        assert artifact.affected_pair_count == 0
    finally:
        await sessions.dispose()
    run_alembic_upgrade(config, "20260809_0019")
    sessions = build_session_factory(app_url.render_as_string(hide_password=False))
    try:
        result = await run_recovery(
            sessions=sessions,
            cipher=object(),
            settings=Settings(app_env="test", app_test_mode=True),
            **kwargs,
        )
        assert result.planned == () and result.remaining_markers == 0
        assert sessions.engine.url.username == "ai_employee_app" and adapters.accesses == []
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "audit_failure",
    (
        None,
        "late_publish",
        "post_publish_guard",
        "cancel_publish",
        "lease_exit",
        "missing_cursor",
        "disconnected",
        "capability_disabled",
        "missing_access",
        "missing_refresh",
        "missing_calendar",
        "sealed_callback",
        "sealed_snapshot_mismatch",
    ),
)
async def test_calendar_audit_nonzero_phases_with_real_app_oneoffs(
    empty_migration_database: URL, tmp_path: Path, monkeypatch, audit_failure
) -> None:
    """真实app贯穿audit、资格拒绝和sealed回调；同ID跨连接事实必须保持精确隔离。"""
    import asyncio

    from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
    from ai_employee.application.use_cases.calendar_aad_rollout import (
        CalendarAadBinding,
        CalendarAadRolloutError,
    )
    from ai_employee.cli import calendar_aad_audit_0019 as audit_module
    from ai_employee.cli.calendar_aad_0019 import run_recovery
    from ai_employee.cli.calendar_aad_audit_0019 import load_audit, run_audit
    from ai_employee.cli.calendar_aad_preflight_0019 import run_preflight
    from ai_employee.cli.database_maintenance import (
        CalendarAadOwnerFactsReader,
        DatabaseEndpoint,
        MigrateArguments,
        _open_maintenance_context,
    )
    from ai_employee.cli.verify_restored_backup import read_restore_fingerprint
    from ai_employee.config import Settings
    from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
        CalendarAadArtifactFile,
        CalendarAadMigrationArtifactGuard,
        calendar_aad_rollout_lease,
    )
    from ai_employee.infrastructure.db.repositories.oauth_refresh_coordinator import (
        SqlAlchemyOAuthRefreshCoordinator,
    )
    from ai_employee.infrastructure.db.session import build_session_factory
    from ai_employee.infrastructure.security.encryption import AeadCipher
    from tests.integration.m2.test_credential_rotation_repository import (
        OTHER_USER_ID,
        seed_oauth_connection,
    )
    from tests.integration.operations.test_calendar_aad_0019_preflight import (
        BASENAME,
        IMAGE,
        NOW,
        FakeAdapters,
        seed_pair,
    )
    from tests.integration.operations.test_calendar_aad_0019_recovery import (
        SECOND_SCOPE,
        seed_recovery_isolation,
    )

    config = Config(ROOT / "backend/alembic.ini")
    set_alembic_database_url(config, empty_migration_database.render_as_string(hide_password=False))
    run_alembic_upgrade(config, "20260809_0018")
    app_url = empty_migration_database.set(
        username="ai_employee_app", password="app-role-integration-password"
    )
    sessions = build_session_factory(app_url.render_as_string(hide_password=False))
    cipher = AeadCipher(bytes(range(32)), key_version=7)
    secret = tmp_path / "synthetic-owner-secret"
    secret.write_text(empty_migration_database.password or "", encoding="utf-8")
    secret.chmod(0o600)
    endpoint = DatabaseEndpoint(
        empty_migration_database.host,
        empty_migration_database.port,
        empty_migration_database.username,
        empty_migration_database.database,
    )
    reader = CalendarAadOwnerFactsReader(endpoint, secret)
    coordinator = SqlAlchemyOAuthRefreshCoordinator(
        session_factory=sessions,
        cipher=cipher,
        identity=OAuthRefreshIdentity(bytes(range(32)), key_version=7),
        clock=lambda: NOW,
    )
    adapters = FakeAdapters()
    audit_clock = [NOW]
    kwargs = {
        "sessions": sessions,
        "coordinator": coordinator,
        "adapters": adapters,
        "backup_directory": tmp_path,
        "basename": BASENAME,
        "immutable_image_id": IMAGE,
        "clock": lambda: NOW,
        "facts_reader": reader,
    }
    try:
        await seed_oauth_connection(sessions, cipher)
        await seed_pair(sessions)
        if audit_failure in {"sealed_callback", "sealed_snapshot_mismatch"}:
            # restored completion要求精确单管理员；只在本例首次构造fixture时移除未使用隔离用户。
            await seed_pair(sessions, scope=SECOND_SCOPE)
            async with sessions.begin() as session:
                await session.execute(text("DELETE FROM users WHERE id=:id"), {"id": OTHER_USER_ID})
        else:
            await seed_recovery_isolation(sessions, cipher)
        preflight = await run_preflight(**kwargs)
        assert preflight.affected_pair_count == 2 and preflight.affected_connection_count == 1
        module, old_dump, payload = _manifest_group(tmp_path)
        dump = tmp_path / (BASENAME + ".dump.enc")
        old_dump.rename(dump)
        Path(f"{old_dump}.manifest.json").unlink()
        Path(f"{old_dump}.sha256").unlink()
        payload.update(
            {
                "dump_basename": dump.name,
                "checksum_filename": dump.name + ".sha256",
                "immutable_image_id": IMAGE,
                "alembic_revision": "20260809_0018",
            }
        )
        engine = create_engine(
            empty_migration_database.set(drivername="postgresql+psycopg"),
            poolclass=NullPool,
            hide_parameters=True,
        )
        try:
            with engine.connect() as connection, connection.begin():
                connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                payload["restore_fingerprint_v1"] = read_restore_fingerprint(connection).digest
        finally:
            engine.dispose()
        # 此用例的密文成员为合成文件；原生dump/快照绑定由独立image gate验证。
        _rewrite_manifest_group(dump, payload)
        group = module._group_at(dump)

        published = []

        def host_guard():
            """宿主重查失败只发生在实际publish返回之后，不替代任何数据库或期限检查。"""
            if audit_failure == "post_publish_guard" and published:
                raise CalendarAadRolloutError("calendar_aad_host_changed")

        async def audit(phase):
            with (
                _open_maintenance_context(MigrateArguments(secret), endpoint) as context,
                context.acquire_management_lifecycle_lock() as management,
                management.acquire_target_lock() as target,
                target.acquire_schema_lifecycle_lock(),
                target.read_only_holder() as holder,
            ):
                artifact = await run_audit(
                    phase=phase,
                    group=group,
                    holder=holder,
                    sessions=sessions,
                    facts_reader=CalendarAadOwnerFactsReader(endpoint, secret, holder=holder),
                    clock=lambda: audit_clock[0],
                    host_guard=host_guard,
                )
            assert load_audit(group, phase) == artifact
            return artifact

        invalid_facts = {
            "missing_cursor": "DELETE FROM sync_cursors WHERE resource_kind='calendar'",
            "disconnected": "UPDATE oauth_connections SET status='disconnected'",
            "capability_disabled": "UPDATE connection_capabilities SET status='disabled' WHERE capability='calendar.read'",
            "missing_access": "DELETE FROM encrypted_credentials WHERE credential_kind='access_token'",
            "missing_refresh": "DELETE FROM encrypted_credentials WHERE credential_kind='refresh_token'",
            "missing_calendar": "DELETE FROM provider_calendars",
        }
        if audit_failure in invalid_facts:
            # 缺口在合法preflight之后真实提交，audit必须重读当前事实，不能相信旧artifact。
            async with sessions.begin() as session:
                await session.execute(text(invalid_facts[audit_failure]))
            files_before = {
                path: (path.stat().st_ino, path.read_bytes()) for path in tmp_path.iterdir()
            }
            provider_before = (adapters.provider.calls, tuple(adapters.reader.scopes))
            with pytest.raises(CalendarAadRolloutError) as rejected:
                await audit("pre-migration")
            assert rejected.value.error_code == "calendar_aad_local_recoverability_failed"
            assert {
                path: (path.stat().st_ino, path.read_bytes()) for path in tmp_path.iterdir()
            } == files_before
            assert (adapters.provider.calls, tuple(adapters.reader.scopes)) == provider_before
            return

        if audit_failure == "late_publish":
            actual_stage, actual_link = audit_module.tempfile.mkstemp, os.link
            links = []

            def stage_then_expire(*args, **kwargs):
                result = actual_stage(*args, **kwargs)
                if kwargs.get("prefix") == ".calendar-aad-audit-":
                    audit_clock[0] = NOW + timedelta(hours=1)
                return result

            def observe_link(source, destination, **kwargs):
                if str(destination).endswith(".calendar-aad-pre-migration.json"):
                    links.append(destination)
                return actual_link(source, destination, **kwargs)

            monkeypatch.setattr(audit_module.tempfile, "mkstemp", stage_then_expire)
            monkeypatch.setattr(audit_module.os, "link", observe_link)
            with pytest.raises(CalendarAadRolloutError):
                await audit("pre-migration")
            assert links == [], "expired audit became briefly visible before compensation"
            return
        if audit_failure in {"post_publish_guard", "cancel_publish", "lease_exit"}:
            from contextlib import asynccontextmanager

            actual_publish = audit_module._publish
            actual_discard = audit_module.discard_calendar_aad_publication
            actual_lease = audit_module.async_calendar_aad_rollout_lease
            held_during_compensation = []
            loop = asyncio.get_running_loop()
            owner_task = asyncio.current_task()
            assert owner_task is not None

            def publish_then_interrupt(*args):
                """真实链接/fsync成功后才触发取消；controller必须取回该线程的receipt。"""
                result = actual_publish(*args)
                published.append(args[0])
                if audit_failure == "cancel_publish":
                    loop.call_soon_threadsafe(owner_task.cancel)
                return result

            def observe_compensation(path, identity):
                """从独立session检查真实revision advisory lease，读取不改变任务或业务状态。"""
                if path.name.endswith(".calendar-aad-pre-migration.json"):
                    probe = create_engine(
                        empty_migration_database.set(drivername="postgresql+psycopg"),
                        poolclass=NullPool,
                        hide_parameters=True,
                    )
                    try:
                        with probe.connect() as connection:
                            acquired = connection.scalar(
                                text("SELECT pg_try_advisory_lock(20260809,19)")
                            )
                            held_during_compensation.append(acquired is False)
                            if acquired:
                                connection.execute(text("SELECT pg_advisory_unlock(20260809,19)"))
                    finally:
                        probe.dispose()
                return actual_discard(path, identity)

            @asynccontextmanager
            async def failing_exit(url):
                """保留真实lease完整生命周期，仅在真实退出之后报告不可确认的退出失败。"""
                async with actual_lease(url) as lease:
                    yield lease
                raise RuntimeError("synthetic lease exit failure")

            monkeypatch.setattr(audit_module, "_publish", publish_then_interrupt)
            monkeypatch.setattr(
                audit_module, "discard_calendar_aad_publication", observe_compensation
            )
            if audit_failure == "lease_exit":
                monkeypatch.setattr(audit_module, "async_calendar_aad_rollout_lease", failing_exit)
            expected = (
                asyncio.CancelledError
                if audit_failure == "cancel_publish"
                else RuntimeError
                if audit_failure == "lease_exit"
                else CalendarAadRolloutError
            )
            with pytest.raises(expected):
                await audit("pre-migration")
            assert len(published) == 1 and not published[0].exists()
            assert not tuple(tmp_path.glob(".calendar-aad-audit-*"))
            assert held_during_compensation == [audit_failure != "lease_exit"], (
                "owned audit compensation escaped the live revision lease"
            )
            return
        before = await audit("pre-migration")
        existing = {
            path: (path.stat().st_ino, path.read_bytes())
            for path in tmp_path.glob("*.calendar-aad-*.json")
        }
        assert await audit("pre-migration") == before
        assert {path: (path.stat().st_ino, path.read_bytes()) for path in existing} == existing
        if audit_failure in {"sealed_callback", "sealed_snapshot_mismatch"}:
            from sqlalchemy import Engine, event

            from ai_employee.cli import calendar_aad_verify_restored_0018 as sealed
            from ai_employee.cli import database_maintenance as restore_cli
            from ai_employee.cli.postgres_restore import RestoreStateStore
            from ai_employee.infrastructure.db import database_maintenance as maintenance

            monkeypatch.setenv("CALENDAR_AAD_RESTORE_IMAGE_ID", IMAGE)
            if audit_failure == "sealed_snapshot_mismatch":
                # 仍是schema/binding合法的原audit，但精确目录摘要不同；不能据此重开CONNECT。
                changed = before.model_copy(
                    update={
                        "snapshot": before.snapshot.model_copy(
                            update={"directory_digest": "f" * 64}
                        )
                    }
                )
                (tmp_path / f"{BASENAME}.calendar-aad-pre-migration.json").write_bytes(
                    audit_module.serialize_audit_artifact(changed)
                )
            verifier = sealed.sealed_restore_verifier(group)
            claim = maintenance.RestoreClaim(
                "sealed_0018",
                group.manifest_sha256,
                maintenance.RestoreFingerprint("20260809_0018", payload["restore_fingerprint_v1"]),
            )
            state = tmp_path.parent / (tmp_path.name + "-restore-state")
            state.mkdir(mode=0o700)
            # 源文件目录独立于state，实际dump流另由原生镜像演练证明；本例只走catalog合法exact-post。
            store = RestoreStateStore(state, (tmp_path,))
            await sessions.dispose()
            with (
                _open_maintenance_context(MigrateArguments(secret), endpoint) as context,
                context.acquire_management_lifecycle_lock() as management,
                management.acquire_restore_target_lock(claim) as target,
                target.acquire_schema_lifecycle_lock(),
                target.restore_holder(claim) as holder,
            ):
                holder._establish_gate(holder.admit(), store)
            holders, reads, denied_dml = [], [], []
            actual_initialize = maintenance.RestoreMaintenanceHolder.__init__

            def initialize(self, *args, **kwargs):
                """观察新holder的真实物理连接；不替换admission或事务管理。"""
                actual_initialize(self, *args, **kwargs)
                holders.append(
                    (self.connection, self.connection.scalar(text("SELECT pg_backend_pid()")))
                )
                self.connection.commit()

            def observed_verifier(connection, expected):
                """实际sealed回调与先前25006必须来自同一个holder、同一个RR/RO事务。"""
                reads.append(
                    (
                        connection,
                        tuple(
                            connection.execute(
                                text(
                                    "SELECT pg_backend_pid(),session_user,current_user,"
                                    "current_setting('transaction_read_only'),current_setting('transaction_isolation')"
                                )
                            ).one()
                        ),
                    )
                )
                verifier(connection, expected)

            def rejected_write(context):
                """只观察PostgreSQL原始25006，不能用抛出的假异常替代数据库拒绝。"""
                if context.statement == "UPDATE public.users SET id=id WHERE false":
                    denied_dml.append(
                        (context.connection, getattr(context.original_exception, "sqlstate", None))
                    )

            monkeypatch.setattr(maintenance.RestoreMaintenanceHolder, "__init__", initialize)
            event.listen(Engine, "handle_error", rejected_write)
            request = {
                "endpoint": endpoint,
                "owner_password_file": secret,
                "claim": claim,
                "dump": dump,
                "reader": read_restore_fingerprint,
                "verifier": observed_verifier,
                "evidence": store,
                "consumer_environment": {},
                "host_guard": lambda: None,
            }
            try:
                if audit_failure == "sealed_snapshot_mismatch":
                    with pytest.raises(CalendarAadRolloutError) as rejected:
                        restore_cli.run_restore_maintenance(**request)
                    assert rejected.value.error_code == "calendar_aad_restore_verification_failed"
                else:
                    assert restore_cli.run_restore_maintenance(**request) == "restore_completed"
            finally:
                event.remove(Engine, "handle_error", rejected_write)
            assert len(holders) == 1
            held_connection, pid = holders[0]
            assert reads == [
                (
                    held_connection,
                    (pid, endpoint.owner_role, "ai_employee_app", "on", "repeatable read"),
                )
            ]
            assert denied_dml == [(held_connection, "25006")]
            assert adapters.provider.calls == 1 and len(adapters.reader.scopes) == 2
            return
        # 正式migration发生在app one-off结束后，revision lease本身使用migration owner身份。
        await sessions.dispose()

        def migrate():
            with calendar_aad_rollout_lease(empty_migration_database) as lease:
                config.attributes["calendar_aad_0019_guard"] = CalendarAadMigrationArtifactGuard(
                    artifact_file=CalendarAadArtifactFile(
                        tmp_path, CalendarAadBinding(BASENAME, IMAGE)
                    ),
                    lease=lease,
                    clock=lambda: NOW,
                )
                run_alembic_upgrade(config, "20260809_0019")

        await asyncio.to_thread(migrate)
        migrated = await audit("post-migration")
        assert migrated.remaining_historical_v1_fields == 4
        result = await run_recovery(
            cipher=cipher, settings=Settings(app_env="test", app_test_mode=True), **kwargs
        )
        assert result.remaining_markers == 0 and len(result.planned) == 2
        completed = await audit("post-resync")
        assert completed.remaining_historical_v1_fields == 0
        assert before.snapshot.directory_digest == completed.snapshot.directory_digest
        assert sessions.engine.url.username == "ai_employee_app"
        assert adapters.provider.calls == 1 and len(adapters.reader.scopes) == 4
        for artifact in tmp_path.glob("*.calendar-aad-*.json"):
            assert artifact.stat().st_mode & 0o777 == 0o600
            raw = artifact.read_text()
            assert all(
                value not in raw
                for value in (
                    "synthetic-calendar-opaque",
                    "synthetic-old-access",
                    "Synthetic Description",
                    "postgresql",
                )
            )
    finally:
        await sessions.dispose()


@pytest.mark.parametrize("revision", ("20260809_0018", "20260809_0019"))
def test_fingerprint_preserves_full_constraint_semantics_across_native_reparse(
    empty_migration_database: URL, revision: str
) -> None:
    """PG自己的constraint deparser再解析不改变语义；相邻实质修改仍改变完整fingerprint。"""
    from ai_employee.cli.verify_restored_backup import read_restore_fingerprint

    config = Config(ROOT / "backend/alembic.ini")
    set_alembic_database_url(config, empty_migration_database.render_as_string(hide_password=False))
    run_alembic_upgrade(config, revision)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    pairs = (
        ("oauth_connections", "ck_oauth_connections_microsoft_identity"),
        ("tool_executions", "ck_tool_executions_manual_resolution_value"),
    )
    try:
        with engine.connect() as connection, connection.begin() as transaction:
            before = read_restore_fingerprint(connection)
            for table, constraint in pairs:
                definition = connection.scalar(
                    text(
                        "SELECT pg_get_constraintdef(oid,false) FROM pg_constraint "
                        "WHERE conrelid=CAST(:table AS regclass) AND conname=:constraint"
                    ),
                    {"table": f"public.{table}", "constraint": constraint},
                )
                assert isinstance(definition, str)
                # 两个标识符来自固定测试表，定义只在内存中由PG返回，不落盘或输出。
                connection.execute(
                    text(f'ALTER TABLE public."{table}" DROP CONSTRAINT "{constraint}"')
                )
                connection.execute(
                    text(f'ALTER TABLE public."{table}" ADD CONSTRAINT "{constraint}" {definition}')
                )
            assert read_restore_fingerprint(connection) == before
            connection.execute(
                text(
                    "ALTER TABLE public.oauth_connections DROP CONSTRAINT "
                    '"ck_oauth_connections_microsoft_identity"'
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE public.oauth_connections ADD CONSTRAINT "
                    "\"ck_oauth_connections_microsoft_identity\" CHECK (provider <> 'microsoft')"
                )
            )
            assert read_restore_fingerprint(connection) != before
            transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize("mismatch", (None, "revision", "table", "constraint", "expression"))
def test_constraint_normalization_matches_only_complete_registered_binding(
    database_url, mismatch: str | None
) -> None:
    """闭合模板同时绑定revision/表/约束/全文摘要，任一不同都原样保留差异。"""
    from sqlalchemy.engine import make_url

    from ai_employee.cli import verify_restored_backup as module

    assert hasattr(module, "_CONSTRAINT_EXPRESSION_DIGEST_SQL"), (
        "fingerprint does not normalize exact known native constraint representations"
    )
    values = {
        "fingerprint_revision": "20260809_0019",
        "table": "oauth_connections",
        "constraint": "ck_oauth_connections_microsoft_identity",
        "expression": "a00757999e186c05f49066ea519b8a46a843d0ddf32a5963a759c7254ec69a82",
    }
    if mismatch is not None:
        values["fingerprint_revision" if mismatch == "revision" else mismatch] = "different"
    engine = create_engine(
        make_url(str(database_url)).set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection, connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            actual = connection.scalar(
                text(
                    "SELECT "
                    + module._CONSTRAINT_EXPRESSION_DIGEST_SQL
                    + " FROM (SELECT CAST(:table AS text) AS relname) c "
                    "CROSS JOIN (SELECT CAST(:constraint AS text) AS conname) co "
                    "CROSS JOIN (SELECT CAST(:expression AS text) AS expression_digest) d"
                ),
                values,
            )
            assert actual == (
                "ef16888e1c46515ccbaaf05d19cc631f744e8273604c25ead47a45e3d913e4f1"
                if mismatch is None
                else values["expression"]
            )
    finally:
        engine.dispose()


def test_fingerprint_requires_manifest_revision_and_readonly_snapshot_boundary() -> None:
    """当前备份入口必须提供完整业务摘要与导出快照API，不能把行数当内容相等证据。"""
    name = "ai_employee.cli.verify_restored_backup"
    assert importlib.util.find_spec(name) is not None, (
        "backup has no full-data fingerprint verifier"
    )
    module = importlib.import_module(name)
    assert callable(module.read_restore_fingerprint)
    assert callable(module.export_backup_snapshot)


def test_backup_parent_and_remote_lifecycle_are_explicit() -> None:
    """完整父流程与私有target/run远程组协议缺失时失败，不能把旧pair finish视为完成。"""
    name = "ai_employee.cli.postgres_backup"
    assert importlib.util.find_spec(name) is not None, "complete backup parent is absent"
    module = importlib.import_module(name)
    assert callable(module.run_backup)
    manifest = importlib.import_module("ai_employee.infrastructure.db.postgres_backup_manifest")
    assert callable(manifest.publish_remote_backup)
    assert callable(manifest.cleanup_local_backups)
    assert manifest.ORPHAN_GRACE_SECONDS == 3600


def test_generic_accepts_changed_image_with_same_fixed_code_while_sealed_does_not(
    tmp_path, monkeypatch
) -> None:
    """generic支持相同受管代码的镜像重建；sealed仍绑定原ID，代码变化两者都拒绝。"""
    from ai_employee.cli import postgres_restore

    module, dump, payload = _manifest_group(tmp_path)
    monkeypatch.setenv("OPERATIONS_IMMUTABLE_IMAGE_ID", "sha256:" + "d" * 64)
    monkeypatch.setattr(
        postgres_restore, "image_executable_digests", lambda: payload["executable_digests"]
    )
    group = postgres_restore.validate_restore_input("generic", dump)
    assert group.manifest.immutable_image_id == "sha256:" + "a" * 64
    with pytest.raises((RuntimeError, ValueError)):
        postgres_restore.validate_restore_input("sealed_0018", dump)
    monkeypatch.setattr(
        postgres_restore,
        "image_executable_digests",
        lambda: {key: "e" * 64 for key in module.OPERATIONS_EXECUTABLES},
    )
    with pytest.raises(RuntimeError):
        postgres_restore.validate_restore_input("generic", dump)


@pytest.mark.parametrize(
    "options",
    (
        ("--approve-call-ordinal", "+1"),
        ("--approve-call-ordinal", "01"),
        ("--approve-call-ordinal", "١"),
        ("--approve-call-ordinal", "1", "--approve-call-ordinal", "2"),
        ("--approve-call-ordinal", "1", "--approve-reopen-ordinal", "2"),
    ),
)
def test_restore_cli_rejects_noncanonical_ambiguous_ordinal_before_execution(
    monkeypatch, options
) -> None:
    """operator ordinal只是精确请求，宽松数字、重复及同时请求两类重试都在owner前失败。"""
    from ai_employee.cli import postgres_restore as module

    calls = []
    monkeypatch.setattr(
        module,
        "_execute",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "restore_completed",
    )
    assert module.main(["execute", "generic", "/backups/synthetic.dump.enc", *options]) == 1
    assert calls == []


def test_legacy_resources_require_durable_registry_and_dedicated_entrypoints() -> None:
    """隔离转换必须有真实registry/scavenger入口，不能给旧字节补manifest或借通用restore。"""
    script = ROOT / "scripts/legacy-backup-operations.py"
    assert script.is_file(), "legacy registry/scavenger lifecycle is absent"
    for name in ("convert-legacy-backup.sh", "scavenge-legacy-backups.sh"):
        assert (ROOT / "scripts" / name).is_file()
    module = runpy.run_path(str(script), run_name="legacy_contract_test")
    assert module["ORPHAN_GRACE_SECONDS"] == 3600
    assert callable(module["convert"]) and callable(module["scavenge"])


def test_legacy_configuration_explicitly_initializes_only_its_private_empty_volume(tmp_path):
    """官方Postgres入口以root初始化专用空卷后降权；converter仍以宿主UID写私有备份。"""
    module = runpy.run_path(
        str(ROOT / "scripts/legacy-backup-operations.py"), run_name="legacy_config_test"
    )
    record = _legacy_registry(tmp_path, module)
    configuration = module["_configuration"](record, {}, tmp_path / "legacy.dump.enc")
    postgres = configuration["services"]["legacy-conversion-postgres"]
    converter = configuration["services"]["legacy-backup-converter"]
    assert postgres.get("user") == "0:0", "bootstrap user must not inherit rootless keep-id default"
    assert postgres["volumes"] == ["legacy_conversion_data:/var/lib/postgresql/data"]
    assert converter["user"] == f"{os.geteuid()}:{os.getegid()}"
    assert configuration["networks"]["legacy_conversion"]["internal"] is True
    assert set(configuration["volumes"]) == {"legacy_conversion_data"}
    assert all(not service.get("ports") for service in configuration["services"].values())


def test_legacy_registry_and_configuration_use_specification_label_names(tmp_path):
    """规格§22的三项label是精确归属协议，不能用另一组拼写产生不可互查的资源。"""
    module = runpy.run_path(
        str(ROOT / "scripts/legacy-backup-operations.py"), run_name="legacy_label_test"
    )
    record = _legacy_registry(tmp_path, module)
    expected = {
        "com.ai-employee.maintenance.attempt": record.attempt_id,
        "com.ai-employee.maintenance.kind": "legacy_conversion",
        "com.ai-employee.maintenance.created_at": record.created_at,
    }
    assert record.labels == expected
    config = module["_configuration"](record, {}, tmp_path / "legacy.dump.enc")
    for resource in (
        *config["services"].values(),
        *config["networks"].values(),
        *config["volumes"].values(),
    ):
        assert resource["labels"] == expected


def test_legacy_health_reads_real_app_role_and_leaves_baseline_unchanged(
    empty_migration_database, tmp_path
):
    """新隔离目标健康检查只读同一快照；实证app身份、25006与全部baseline grant不变。"""
    from sqlalchemy import event

    from ai_employee.infrastructure.db.database_grants import GrantPhase, verify_object_grants

    module = runpy.run_path(
        str(ROOT / "scripts/legacy-backup-operations.py"), run_name="legacy_health_test"
    )
    assert callable(module.get("_verify_isolated_health")), (
        "isolated app-compatible health path is absent"
    )
    _, _, expected, _, _ = _restore_fixture(empty_migration_database, tmp_path)
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    roles, failures, statements = [], [], []

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        if statement.startswith("SELECT count(*) FROM public."):
            roles.append(
                tuple(
                    connection.execute(
                        text(
                            "SELECT pg_backend_pid(),current_user,current_setting('transaction_read_only')"
                        )
                    ).one()
                )
            )

    def error(context):
        failures.append(getattr(context.original_exception, "sqlstate", None))

    event.listen(engine, "before_cursor_execute", observe)
    event.listen(engine, "handle_error", error)
    try:
        with engine.connect() as connection:
            initial = verify_object_grants(
                connection, revision=expected.revision, phase=GrantPhase.BASELINE
            )
            connection.commit()
            with connection.begin():
                connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                pid = connection.scalar(text("SELECT pg_backend_pid()"))
                assert module["_verify_isolated_health"](connection) == expected
            assert (
                connection.scalar(text("SELECT current_user")) == empty_migration_database.username
            )
            assert (
                verify_object_grants(
                    connection, revision=expected.revision, phase=GrantPhase.BASELINE
                )
                == initial
            )
        assert roles and all(row == (pid, "ai_employee_app", "on") for row in roles)
        assert failures == ["25006"]
        assert not any(
            statement.lstrip()
            .upper()
            .startswith(("ALTER ", "GRANT ", "REVOKE ", "INSERT ", "DELETE "))
            for statement in statements
        )
    finally:
        engine.dispose()


def _legacy_registry(tmp_path: Path, module, *, created_at="2030-01-01T00:00:00.000000Z"):
    """仅为隔离资源测试建立规范registry，不建立容器、workspace角色或任何恢复catalog事实。"""
    attempt = "00000000-0000-0000-0000-000000000701"
    project = "aiemployee-legacy-" + attempt.replace("-", "")
    record = module["LegacyRegistry"](
        "ai_employee.legacy_backup_conversion.v1",
        attempt,
        created_at,
        "legacy_conversion",
        str(tmp_path),
        "legacy.dump.enc",
        "a" * 64,
        19,
        "converted",
        "sha256:" + "b" * 64,
        project,
        project + "-postgres",
        project + "-converter",
        project + "-internal",
        project + "-postgres-data",
        str(tmp_path / ".legacy-conversion-registry" / (attempt + ".secret")),
    )
    module["_publish_registry"](record)
    secret = Path(record.secret_path)
    secret.write_text("synthetic-ephemeral-only", encoding="ascii")
    secret.chmod(0o600)
    return record


class FakeLegacyEngine:
    """只替换daemon进程，真实registry、资源解析、labels校验和cleanup流程仍执行。"""

    def __init__(self, record, *, running=False):
        self.record = record
        labels = {**record.labels, "com.docker.compose.project": record.project}
        self.values = {
            ("container", "1" * 64): {
                "Id": "1" * 64,
                "Name": "/" + record.postgres_container,
                "Config": {"Labels": labels},
                "State": {
                    "Running": running,
                    "Paused": False,
                    "Restarting": False,
                    "Status": "running" if running else "exited",
                },
            },
            ("network", "2" * 64): {
                "Id": "2" * 64,
                "Name": record.network,
                "Labels": labels,
                "Internal": True,
            },
            ("volume", record.volume): {"Name": record.volume, "Labels": labels},
        }
        self.calls = []
        self.removals = []

    def query(self, *arguments, **kwargs):
        self.calls.append(arguments)
        if arguments[0] == "inspect":
            return json.dumps([self.values[(arguments[2], arguments[3])]])
        kind = "container" if arguments[0] == "ps" else arguments[0]
        assert arguments[:2] == (("ps", "--all") if kind == "container" else (kind, "ls"))
        return "\n".join(identity for category, identity in self.values if category == kind)

    def down(self, record, configuration, *arguments):
        assert record == self.record
        assert set(configuration["services"]) == {
            "legacy-conversion-postgres",
            "legacy-backup-converter",
        }
        assert configuration["name"] == record.project
        assert arguments == ("down", "--volumes", "--remove-orphans")
        self.removals.extend(self.values)
        self.values.clear()
        return ""


def test_legacy_unknown_container_state_never_authorizes_cleanup(tmp_path, monkeypatch) -> None:
    """daemon缺少真实活动状态不是exited证明，必须保留registry、Secret及所有资源。"""
    module = runpy.run_path(
        str(ROOT / "scripts/legacy-backup-operations.py"), run_name="legacy_state_test"
    )
    record = _legacy_registry(tmp_path, module)
    engine = FakeLegacyEngine(record)
    engine.values[("container", "1" * 64)]["State"] = {}
    monkeypatch.setitem(module["_cleanup"].__globals__, "_docker", engine.query)
    monkeypatch.setitem(module["_cleanup"].__globals__, "_compose", engine.down)
    with pytest.raises(RuntimeError):
        module["_cleanup"](record, allow_active=False)
    assert engine.removals == [] and Path(record.secret_path).is_file()


def test_legacy_main_filters_database_failures(monkeypatch, capsys) -> None:
    """驱动失败只能输出固定码，不能让异常SQL/参数或Secret落入CLI traceback。"""
    from sqlalchemy.exc import OperationalError

    module = runpy.run_path(
        str(ROOT / "scripts/legacy-backup-operations.py"), run_name="legacy_error_test"
    )

    def fail():
        raise OperationalError(
            "synthetic private SQL", None, RuntimeError("synthetic private diagnostic")
        )

    monkeypatch.setitem(module["main"].__globals__, "isolated", fail)
    assert module["main"](["isolated"]) == 1
    assert capsys.readouterr().err == "legacy_conversion_failed\n"


@pytest.mark.parametrize(
    "state", ("expired", "recent", "locked", "active", "wrong_labels", "daemon_failure")
)
def test_legacy_scavenger_requires_registry_grace_lock_labels_and_inactive_state(
    tmp_path, monkeypatch, state
) -> None:
    """固定grace和两次daemon检查后只删除精确project；活动/不确定/外来资源保持原样。"""
    module = runpy.run_path(
        str(ROOT / "scripts/legacy-backup-operations.py"), run_name="legacy_scavenge_test"
    )
    record = _legacy_registry(tmp_path, module)
    engine = FakeLegacyEngine(record, running=state == "active")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setitem(module["scavenge"].__globals__, "_docker", engine.query)
    monkeypatch.setitem(module["scavenge"].__globals__, "_compose", engine.down)
    now = datetime(2030, 1, 1, 0, 59 if state == "recent" else 0, tzinfo=UTC)
    if state != "recent":
        now += timedelta(hours=2)
    if state == "wrong_labels":
        engine.values[("network", "2" * 64)]["Labels"] = {}
    if state == "daemon_failure":

        def fail(*args, **kwargs):
            raise module["LegacyConversionError"]("legacy_docker_failed")

        monkeypatch.setitem(module["scavenge"].__globals__, "_docker", fail)
    registry = tmp_path / ".legacy-conversion-registry" / (record.attempt_id + ".json")
    if state == "locked":
        with module["_attempt_lock"](record) as held:
            assert held
            assert module["scavenge"](tmp_path, now=now) == 0
        assert engine.calls == []
    elif state in {"wrong_labels", "daemon_failure"}:
        with pytest.raises(RuntimeError):
            module["scavenge"](tmp_path, now=now)
    else:
        assert module["scavenge"](tmp_path, now=now) == (1 if state == "expired" else 0)
    assert registry.exists() == (state != "expired")
    assert Path(record.secret_path).exists() == (state != "expired")
    assert bool(engine.removals) == (state == "expired")


def test_audit_and_sealed_callbacks_are_separate_from_generic_restore() -> None:
    """独立sealed输入验证/同holder回调与三阶段audit模块必须真实存在。"""
    for name in ("calendar_aad_audit_0019", "calendar_aad_verify_restored_0018"):
        assert importlib.util.find_spec("ai_employee.cli." + name) is not None, name + " is absent"
    audit = importlib.import_module("ai_employee.cli.calendar_aad_audit_0019")
    assert set(audit.AUDIT_PHASES) == {"pre-migration", "post-migration", "post-resync"}


@pytest.mark.parametrize("version", (True, 1.0, "1"))
def test_audit_version_rejects_coercion(version) -> None:
    """artifact中的AAD版本必须是JSON integer；布尔/浮点/字符串不能被Literal隐式接纳。"""
    from ai_employee.cli.calendar_aad_audit_0019 import CalendarAuditEvent

    with pytest.raises(ValueError):
        CalendarAuditEvent(
            identity_digest="a" * 64,
            non_aad_digest="b" * 64,
            description_digest="c" * 64,
            location_digest=None,
            description_version=version,
            location_version=None,
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "entrypoint",
        "config",
        "secret_target",
        "privilege",
        "environment",
        "unhashable_mount",
        "unhashable_secret_target",
    ),
)
def test_operations_host_rejects_alternate_executable_or_secret_boundary(mutation) -> None:
    """镜像内摘要不能授权被entrypoint/Config/Secret/Python环境覆盖的owner可执行边界。"""
    host = runpy.run_path(
        str(ROOT / "scripts/run-postgres-operations.py"), run_name="operations_host_mount_test"
    )
    service = {
        "volumes": [{"type": "bind", "source": "/synthetic/data", "target": "/backups"}],
        "secrets": [
            {"source": "postgres_bootstrap_password", "target": "postgres_bootstrap_password"}
        ],
        "environment": {},
    }
    if mutation == "entrypoint":
        service["entrypoint"] = ["/synthetic-owner-code"]
    elif mutation == "config":
        service["configs"] = [{"source": "synthetic", "target": "/app/scripts/restore-postgres.sh"}]
    elif mutation == "secret_target":
        service["secrets"][0]["target"] = "/app/scripts/restore-postgres.sh"
    elif mutation == "environment":
        service["environment"]["PYTHONPATH"] = "/backups"
    elif mutation == "unhashable_mount":
        service["volumes"][0]["target"] = []
    elif mutation == "unhashable_secret_target":
        service["secrets"][0]["target"] = []
    else:
        service["privileged"] = True
    with pytest.raises(host["OperationsHostError"]):
        host["_reject_executable_mounts"](service)


def test_operations_host_rejects_incomplete_group_before_owner_service(
    tmp_path: Path, monkeypatch
) -> None:
    """宿主缺manifest即停止，不能因目标环境为test而先启动owner容器或解密进程。"""
    path = ROOT / "scripts/run-postgres-operations.py"
    assert path.is_file(), "pure-file/image host guard is absent"
    host = runpy.run_path(str(path), run_name="operations_host_test")
    dump = tmp_path / "synthetic.dump.enc"
    dump.write_bytes(b"synthetic")
    dump.chmod(0o600)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("owner/image resources touched before file admission")

    monkeypatch.setitem(host["run"].__globals__, "_docker", forbidden)
    with pytest.raises(RuntimeError):
        host["run"]("restore", str(dump))
    assert calls == []


def _operations_host_probe(tmp_path, monkeypatch, operation="restore"):
    """只在engine进程边界提供合成响应，文件验证、固定argv、host重查与握手均执行产品代码。"""
    from ai_employee.application.use_cases.calendar_aad_rollout import (
        CalendarAadBinding,
        CalendarAadFacts,
        create_rollout_artifact,
        serialize_rollout_artifact,
    )
    from ai_employee.cli.calendar_aad_audit_0019 import (
        CalendarAadAuditArtifact,
        CalendarAuditSnapshot,
        serialize_audit_artifact,
    )

    source = tmp_path / "source with spaces"
    source.mkdir(mode=0o700)
    module, dump, payload = _manifest_group(source)
    if operation in {"sealed", "audit"}:
        payload["alembic_revision"] = "20260809_0018"
        _rewrite_manifest_group(dump, payload)
        preflight = create_rollout_artifact(
            CalendarAadBinding("synthetic", payload["immutable_image_id"]),
            CalendarAadFacts("20260809_0018", (), ()),
        )
        raw = serialize_rollout_artifact(preflight)
        preflight_path = source / "synthetic.calendar-aad-preflight.json"
        preflight_path.write_bytes(raw)
        preflight_path.chmod(0o600)
        group = module._group_at(dump)
        for phase in ("pre-migration", "post-migration"):
            artifact = CalendarAadAuditArtifact(
                phase=phase,
                revision="20260809_0018" if phase == "pre-migration" else "20260809_0019",
                rollout_digest_v1=preflight.rollout_digest_v1,
                backup_artifact_basename="synthetic",
                immutable_image_id=payload["immutable_image_id"],
                manifest_sha256=group.manifest_sha256,
                preflight_sha256=hashlib.sha256(raw).hexdigest(),
                effective_deadline=None,
                affected_pair_count=0,
                affected_connection_count=0,
                pair_digests=(),
                connection_digests=(),
                remaining_historical_v1_fields=0,
                snapshot=CalendarAuditSnapshot(
                    events=(), cursors=(), directory_digest="a" * 64, credential_digest="b" * 64
                ),
            )
            artifact_path = source / f"synthetic.calendar-aad-{phase}.json"
            artifact_path.write_bytes(serialize_audit_artifact(artifact))
            artifact_path.chmod(0o600)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("BACKUP_DIR", str(source))
    monkeypatch.setenv("RESTORE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("BACKUP_ARTIFACT_BASENAME", raising=False)
    monkeypatch.delenv("ALLOW_PRODUCTION_RESTORE", raising=False)
    host = runpy.run_path(
        str(ROOT / "scripts/run-postgres-operations.py"), run_name="operations_process_test"
    )

    class Probe:
        """Fake只提供本地镜像/Compose观察及子进程流，不替换任何产品admission函数。"""

        def __init__(self):
            self.image_id = payload["immutable_image_id"]
            self.config = {"name": "synthetic-operations", "services": {}}
            for name in (
                "backup",
                "postgres-restore",
                "calendar-aad-restore-0018",
                "api",
                "worker",
                "scheduler",
            ):
                self.config["services"][name] = {
                    "image": "synthetic-backend:fixed",
                    "environment": {
                        "DATABASE_URL": "postgresql+psycopg://ai_employee_owner@postgres:5432/synthetic_test"
                    },
                    "volumes": [{"type": "bind", "source": str(source), "target": "/backups"}],
                    "secrets": [
                        "postgres_bootstrap_password",
                        "app_database_password",
                        "backup_passphrase",
                    ],
                }
            self.calls, self.starts, self.confirmations = [], [], []
            self.config_reads = 0
            self.on_config = None
            self.rows = []
            self.own = None
            self.inspect_mutation = None
            self.stdout_factory = io.StringIO
            code = (
                "postgres_backup_completed"
                if operation == "backup"
                else "calendar_aad_audit_passed"
                if operation == "audit"
                else "restore_completed"
            )
            self.output = (
                ("" if operation == "backup" else "operations.guard.request\n") + code + "\n"
            )

        def docker(self, *arguments):
            self.calls.append(arguments)
            if arguments == ("compose", "--profile", "operations", "config", "--format", "json"):
                self.config_reads += 1
                if self.on_config:
                    self.on_config(self.config_reads)
                return json.dumps(self.config)
            if arguments[:2] == ("image", "inspect"):
                return self.image_id
            if arguments[:2] == ("run", "--rm"):
                assert arguments[arguments.index("--network") + 1] == "none"
                assert not any(
                    argument in {"--mount", "--volume", "-v", "-e"} for argument in arguments
                )
                return json.dumps(payload["executable_digests"])
            if arguments == ("compose", "ps", "--all", "--format", "json"):
                own = (
                    []
                    if self.own is None
                    else [
                        {
                            "ID": "d" * 64,
                            "Name": self.own["Name"].removeprefix("/"),
                            "Service": self.own["Config"]["Labels"]["com.docker.compose.service"],
                            "State": "running",
                        }
                    ]
                )
                return json.dumps([*self.rows, *own])
            assert arguments == ("inspect", "--type", "container", "d" * 64)
            observed = json.loads(json.dumps(self.own))
            if self.inspect_mutation:
                self.inspect_mutation(observed)
            return json.dumps([observed])

        def popen(self, command, **kwargs):
            rendered = Path(command[command.index("-f") + 1])
            assert rendered.stat().st_mode & 0o777 == 0o600
            config = json.loads(rendered.read_text())
            selected = config["services"][command[-1]]
            self.starts.append((command, selected))
            self.own = {
                "Id": "d" * 64,
                "Name": "/" + command[command.index("--name") + 1],
                "Image": selected["image"],
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": config["name"],
                        "com.docker.compose.service": command[-1],
                    }
                },
                "State": {"Running": True},
            }
            probe = self

            class Input(io.StringIO):
                def write(self, value):
                    probe.confirmations.append(value)
                    return super().write(value)

            return SimpleNamespace(
                stdin=Input(),
                stdout=self.stdout_factory(self.output),
                wait=lambda **kwargs: 0,
                poll=lambda: 0,
            )

    probe = Probe()
    monkeypatch.setitem(host["run"].__globals__, "_docker", probe.docker)
    monkeypatch.setattr(subprocess, "Popen", probe.popen)
    return host, probe, dump


@pytest.mark.parametrize(
    "operation,phase,options",
    (
        ("restore", None, ()),
        ("restore", None, ("--approve-call-ordinal", "2")),
        ("restore", None, ("--approve-reopen-ordinal", "2")),
        ("sealed", None, ()),
        ("sealed", None, ("--approve-call-ordinal", "2")),
        ("sealed", None, ("--approve-reopen-ordinal", "2")),
        ("audit", "pre-migration", ()),
        ("audit", "post-migration", ()),
        ("audit", "post-resync", ()),
        ("backup", None, ()),
    ),
)
def test_operations_host_runs_fixed_image_command_and_rechecks_handshake(
    tmp_path, monkeypatch, operation, phase, options
):
    """默认与显式ordinal均保留固定脚本、无pull、独立mount，并在握手时重新查实际容器。"""
    host, probe, dump = _operations_host_probe(tmp_path, monkeypatch, operation)
    path = str(dump).removesuffix(".dump.enc") if operation == "audit" else str(dump)
    host["run"](operation, path, phase, ordinal_options=options)
    assert len(probe.starts) == 1
    command, selected = probe.starts[0]
    assert "--no-deps" in command and command[command.index("--pull") + 1] == "never"
    assert selected["image"] == probe.image_id and selected["pull_policy"] == "never"
    assert selected["command"][0] == "bash" and selected["command"][1].startswith("/app/scripts/")
    assert not {"PGUSER", "PGPASSWORD"} & set(selected["environment"])
    mounts = {mount["target"]: mount for mount in selected["volumes"]}
    if operation in {"restore", "sealed"}:
        assert selected["command"][2:] == ["/backups/synthetic.dump.enc", *options]
        assert mounts["/backups"]["read_only"] is True
        assert mounts["/var/lib/ai-employee/restore-state"]["read_only"] is False
    elif operation == "audit":
        assert selected["command"][2:] == [phase, "/backups/synthetic"]
    assert probe.confirmations == (
        [] if operation == "backup" else ["operations.guard.confirmed\n"]
    )
    assert probe.config_reads == (1 if operation == "backup" else 3)


@pytest.mark.parametrize(
    "mutation",
    (
        "endpoint_password",
        "endpoint_owner",
        "endpoint_missing",
        "endpoint_drift",
        "image_drift",
        "source_drift",
        "executable_mount",
        "state_overlap",
        "state_symlink",
        "state_mode",
        "running_service",
        "running_owner",
        "switch",
        "unknown_environment",
        "production_opt_in",
        "production_confirmation",
    ),
)
def test_operations_host_refuses_invalid_target_or_changed_inputs_before_owner(
    tmp_path, monkeypatch, mutation
):
    """非法endpoint、当前观察变化和确认缺失必须在owner Popen之前拒绝；Fake绝不执行恢复。"""
    host, probe, dump = _operations_host_probe(tmp_path, monkeypatch)
    selected = probe.config["services"]["postgres-restore"]
    if mutation.startswith("endpoint_"):
        values = {
            "endpoint_password": "postgresql+psycopg://ai_employee_owner:synthetic@postgres:5432/synthetic_test",
            "endpoint_owner": "postgresql+psycopg://ai_employee_app@postgres:5432/synthetic_test",
            "endpoint_missing": "",
            "endpoint_drift": "postgresql+psycopg://ai_employee_owner@postgres:5432/other_test",
        }
        if mutation == "endpoint_drift":
            probe.on_config = lambda count: (
                selected["environment"].update(DATABASE_URL=values[mutation])
                if count == 2
                else None
            )
        else:
            selected["environment"]["DATABASE_URL"] = values[mutation]
    elif mutation == "image_drift":
        probe.on_config = lambda count: (
            setattr(probe, "image_id", "sha256:" + "e" * 64) if count == 2 else None
        )
    elif mutation == "source_drift":
        probe.on_config = lambda count: (
            dump.write_bytes(b"changed synthetic ciphertext") if count == 2 else None
        )
    elif mutation == "executable_mount":
        selected["volumes"][0]["target"] = "/app/scripts"
    elif mutation == "state_overlap":
        monkeypatch.setenv("RESTORE_STATE_DIR", str(dump.parent))
    elif mutation in {"state_symlink", "state_mode"}:
        state = tmp_path / "state"
        if mutation == "state_symlink":
            state.symlink_to(dump.parent, target_is_directory=True)
        else:
            state.mkdir(mode=0o755)
    elif mutation.startswith("running_"):
        probe.rows = [
            {
                "State": "running",
                "Service": "role-bootstrap" if mutation == "running_owner" else "caddy",
            }
        ]
    elif mutation == "switch":
        probe.config["services"]["worker"]["environment"]["GOOGLE_WRITES_ENABLED"] = "true"
    elif mutation == "unknown_environment":
        monkeypatch.setenv("APP_ENV", "staging")
    else:
        monkeypatch.setenv("APP_ENV", "production")
        if mutation == "production_confirmation":
            monkeypatch.setenv("ALLOW_PRODUCTION_RESTORE", "yes")
            monkeypatch.setattr("sys.stdin", io.StringIO("another-file\n"))
    with pytest.raises((RuntimeError, ValueError)):
        host["run"]("restore", str(dump))
    assert probe.starts == []


@pytest.mark.parametrize("identity", ("image", "project", "service", "container_id", "running"))
def test_operations_host_refuses_own_container_mismatch_before_guard_confirmation(
    tmp_path, monkeypatch, identity
):
    """本次name只定位容器，实际ID/image/project/service/Running不匹配时不发owner继续许可。"""
    host, probe, dump = _operations_host_probe(tmp_path, monkeypatch)

    def mutate(value):
        if identity == "image":
            value["Image"] = "sha256:" + "e" * 64
        elif identity in {"project", "service"}:
            value["Config"]["Labels"]["com.docker.compose." + identity] = "foreign"
        elif identity == "container_id":
            value["Id"] = "e" * 64
        else:
            value["State"]["Running"] = False

    probe.inspect_mutation = mutate
    with pytest.raises(host["OperationsHostError"]):
        host["run"]("restore", str(dump))
    assert len(probe.starts) == 1 and probe.confirmations == []


@pytest.mark.parametrize("fault", ("bounded_read", "duplicate_result", "guard_after_result"))
def test_operations_host_stream_is_bounded_and_has_one_terminal_result(
    tmp_path, monkeypatch, fault
):
    """同一owner输出通道必须有界读取且只有一个terminal码，不能吞掉前一个相反结果。"""
    host, probe, dump = _operations_host_probe(tmp_path, monkeypatch)
    if fault == "bounded_read":

        class BoundedOutput(io.StringIO):
            def __next__(self):
                raise AssertionError("unbounded stdout iterator")

            def readline(self, size=-1):
                assert 0 < size <= 128
                return super().readline(size)

        probe.stdout_factory = BoundedOutput
        host["run"]("restore", str(dump))
    else:
        probe.output = "operations.guard.request\nrestore_completed\n" + (
            "restore_already_applied\n"
            if fault == "duplicate_result"
            else "operations.guard.request\n"
        )
        with pytest.raises(host["OperationsHostError"]):
            host["run"]("restore", str(dump))


@pytest.mark.parametrize("replacement", (None, "regular", "fifo", "symlink", "directory"))
def test_local_cleanup_captures_before_delete_and_preserves_replacements(
    tmp_path: Path, monkeypatch, replacement: str | None
) -> None:
    """过期且无marker的已识别组可清理；precheck后的外来替换不得被unlink或跟随。"""
    module = _manifest_module()
    assert hasattr(module, "cleanup_local_backups"), "locked candidate cleanup is absent"
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    target = root / "ai_employee-expired.dump.enc"
    target.write_bytes(b"synthetic-old-partial")
    target.chmod(0o600)
    now = datetime(2030, 1, 2, tzinfo=UTC)
    old = (now - timedelta(hours=2)).timestamp()
    os.utime(target, (old, old))
    real_rename = os.rename
    changed = []

    def replace_then_capture(source, destination, **kwargs):
        if str(source) == target.name and replacement and not changed:
            target.unlink()
            if replacement == "regular":
                target.write_bytes(b"foreign")
            elif replacement == "fifo":
                os.mkfifo(target, 0o600)
            elif replacement == "symlink":
                target.symlink_to("missing-foreign")
            else:
                target.mkdir()
            changed.append(target.lstat())
        return real_rename(source, destination, **kwargs)

    monkeypatch.setattr(module.os, "rename", replace_then_capture)
    kwargs = {
        "directory": root,
        "current_run": "00000000-0000-0000-0000-000000000701",
        "now": now,
        "assert_locked": lambda: None,
    }
    if replacement:
        with pytest.raises(module.BackupManifestError):
            module.cleanup_local_backups(**kwargs)
        candidates = [target, *root.glob(".postgres-backup-custody-*/" + target.name)]
        assert any(
            os.path.lexists(path) and os.path.samestat(path.lstat(), changed[0])
            for path in candidates
        )
    else:
        module.cleanup_local_backups(**kwargs)
        assert not target.exists()


def test_restore_claim_validates_exact_binding_before_holder_connection() -> None:
    """调用方只能提交闭合kind、manifest摘要和支持版本，不能扩展动作或注入状态。"""
    module = importlib.import_module("ai_employee.infrastructure.db.database_maintenance")
    assert hasattr(module, "RestoreClaim"), "restore has no typed matching claim"
    with pytest.raises(module.DatabaseMaintenanceInvariantError):
        module.RestoreClaim(
            "generic", "not-a-digest", module.RestoreFingerprint("20260809_0019", "b" * 64)
        )
    with pytest.raises(module.DatabaseMaintenanceInvariantError):
        module.RestoreClaim(
            "legacy", "a" * 64, module.RestoreFingerprint("20260809_0019", "b" * 64)
        )


def _restore_fixture(database_url: URL, tmp_path: Path, *, revision: str = "20260809_0019"):
    """建立实际baseline目标和独立state；合成管理员可inactive，fixture不伪造恢复catalog。"""
    from ai_employee.cli.database_maintenance import DatabaseEndpoint
    from ai_employee.cli.postgres_restore import RestoreStateStore
    from ai_employee.cli.verify_restored_backup import read_restore_fingerprint

    config = Config(ROOT / "backend/alembic.ini")
    set_alembic_database_url(config, database_url.render_as_string(hide_password=False))
    run_alembic_upgrade(config, revision)
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"), poolclass=NullPool, hide_parameters=True
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO public.users(id,email,display_name,password_hash,timezone,locale,brief_time,is_active,email_body_retention_days,source_metadata_retention_days,workspace_history_retention_days) VALUES('00000000-0000-0000-0000-000000000701','synthetic@example.invalid','synthetic-before',NULL,'UTC','zh-CN','08:00',false,30,180,365)"
            )
        )
    with engine.connect() as connection, connection.begin():
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        expected = read_restore_fingerprint(connection)
    engine.dispose()
    secret = tmp_path / "synthetic-owner-secret"
    secret.write_text(database_url.password or "", encoding="utf-8")
    secret.chmod(0o600)
    state = tmp_path / "state"
    source = tmp_path / "source"
    state.mkdir(mode=0o700)
    source.mkdir(mode=0o700)
    endpoint = DatabaseEndpoint(
        database_url.host, database_url.port, database_url.username, database_url.database
    )
    return endpoint, secret, expected, RestoreStateStore(state, (source,)), source / "unused.dump"


@pytest.mark.parametrize("kind", ("generic", "sealed_0018"))
@pytest.mark.parametrize("prior_completion", (False, True))
def test_restore_no_gate_is_complete_database_zero_write_and_zero_spawn(
    empty_migration_database: URL, tmp_path: Path, monkeypatch, kind, prior_completion
) -> None:
    """独立观察每条owner语句、全部DB事实/sequence/password指纹和真实spawn边界，证明无gate分支只读。"""
    import asyncio

    from sqlalchemy import event

    from ai_employee.cli import database_maintenance as cli
    from ai_employee.cli.verify_restored_backup import (
        read_restore_fingerprint,
        verify_restored_backup,
    )
    from ai_employee.infrastructure.db import database_maintenance as maintenance
    from tests.integration.operations.test_database_maintenance_gate import (
        _capture_sync_transactional_state,
    )

    endpoint, secret, expected, store, dump = _restore_fixture(
        empty_migration_database,
        tmp_path,
        revision="20260809_0018" if kind == "sealed_0018" else "20260809_0019",
    )
    # sealed原artifact的专属回调另有native覆盖；这里独立观察common no-gate零写与kind命名。
    claim = maintenance.RestoreClaim(kind, "a" * 64, expected)
    if prior_completion:
        with (
            cli._open_maintenance_context(cli.MigrateArguments(secret), endpoint) as context,
            context.acquire_management_lifecycle_lock() as management,
            management.acquire_restore_target_lock(claim) as target,
            target.acquire_schema_lifecycle_lock(),
            target.restore_holder(claim) as holder,
        ):
            holder._establish_gate(holder.admit(), store)
        assert (
            cli.run_restore_maintenance(
                endpoint=endpoint,
                owner_password_file=secret,
                claim=claim,
                dump=dump,
                reader=read_restore_fingerprint,
                verifier=verify_restored_backup,
                evidence=store,
                consumer_environment={},
                host_guard=lambda: None,
            )
            == "restore_completed"
        )
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )

    def snapshot():
        with engine.connect() as connection:
            catalog = _capture_sync_transactional_state(connection)
            sequences = tuple(
                connection.execute(
                    text(
                        "SELECT sequencename,last_value FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename"
                    )
                )
            )
            roles = tuple(
                connection.execute(
                    text(
                        "SELECT rolname,md5(COALESCE(rolpassword,'')) FROM pg_authid ORDER BY rolname"
                    )
                )
            )
            settings = tuple(
                connection.execute(
                    text(
                        "SELECT setdatabase,setrole,setconfig FROM pg_db_role_setting ORDER BY setdatabase,setrole"
                    )
                )
            )
            return catalog, sequences, roles, settings

    before = snapshot()
    spawns, statements, reads = [], [], []

    def forbidden(*args, **kwargs):
        spawns.append(args)
        raise AssertionError("no-gate path attempted a real process spawn")

    def observe(connection, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement)

    def reader(connection):
        reads.append(
            tuple(
                connection.execute(
                    text(
                        "SELECT current_user,current_setting('transaction_read_only'),current_setting('transaction_isolation')"
                    )
                ).one()
            )
        )
        return read_restore_fingerprint(connection)

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    event.listen(
        cli.Engine if hasattr(cli, "Engine") else type(engine), "before_cursor_execute", observe
    )
    try:
        for _ in range(2):
            result = cli.run_restore_maintenance(
                endpoint=endpoint,
                owner_password_file=secret,
                claim=claim,
                dump=dump,
                reader=reader,
                verifier=verify_restored_backup,
                evidence=store,
                consumer_environment={},
                host_guard=lambda: None,
            )
            assert result == "restore_already_applied"
        assert spawns == []
        assert reads == [(endpoint.owner_role, "on", "repeatable read")] * 2
        # SELECT/SET LOCAL输出规范、只读事务和advisory协调均允许；所有持久SQL一律拒绝。
        assert all(
            statement.lstrip().split()[0].upper() in {"SELECT", "SET", "SHOW", "WITH"}
            for statement in statements
        )
        assert not any(
            "setval(" in statement or "nextval(" in statement or "ALTER " in statement
            for statement in statements
        )
        assert snapshot() == before
        evidence = tuple(store.directory.glob("*.already-applied.json"))
        assert len(evidence) == 1 and f".{kind}." in evidence[0].name
    finally:
        event.remove(
            cli.Engine if hasattr(cli, "Engine") else type(engine), "before_cursor_execute", observe
        )
        engine.dispose()


@pytest.mark.parametrize("applied", (True, False))
def test_restore_real_completion_ack_uses_new_holder_and_explicit_reopen(
    empty_migration_database: URL, tmp_path: Path, monkeypatch, applied
) -> None:
    """真实最终事务分别COMMIT/ROLLBACK后丢ACK；wrapper必须关闭原物理连接，新holder只核对catalog。"""
    from sqlalchemy.exc import OperationalError

    from ai_employee.cli import database_maintenance as cli
    from ai_employee.cli.verify_restored_backup import (
        read_restore_fingerprint,
        verify_restored_backup,
    )
    from ai_employee.infrastructure.db import database_maintenance as maintenance

    endpoint, secret, expected, store, dump = _restore_fixture(empty_migration_database, tmp_path)
    claim = maintenance.RestoreClaim("generic", "b" * 64, expected)
    with (
        cli._open_maintenance_context(cli.MigrateArguments(secret), endpoint) as context,
        context.acquire_management_lifecycle_lock() as management,
        management.acquire_restore_target_lock(claim) as target,
        target.acquire_schema_lifecycle_lock(),
        target.restore_holder(claim) as holder,
    ):
        holder._establish_gate(holder.admit(), store)
    pids, verifier_pids, commits = [], [], []
    original_init = maintenance.RestoreMaintenanceHolder.__init__
    original_commit = maintenance.RestoreMaintenanceHolder._commit_completion

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        pids.append(self.connection.scalar(text("SELECT pg_backend_pid()")))
        self.connection.commit()

    def commit_with_lost_ack(self, transaction, before, completed):
        class LostAck:
            """只丢弃ACK观察；实际数据库提交或回滚先完成，测试不伪造completed事实。"""

            def commit(inner):
                commits.append(applied)
                transaction.commit() if applied else transaction.rollback()
                raise OperationalError("synthetic COMMIT", None, RuntimeError("synthetic ACK loss"))

        return original_commit(self, LostAck(), before, completed)

    def verifier(connection, frozen):
        verifier_pids.append(
            tuple(
                connection.execute(
                    text(
                        "SELECT pg_backend_pid(),session_user,current_user,current_setting('transaction_read_only')"
                    )
                ).one()
            )
        )
        verify_restored_backup(connection, frozen)

    monkeypatch.setattr(maintenance.RestoreMaintenanceHolder, "__init__", initialize)
    monkeypatch.setattr(
        maintenance.RestoreMaintenanceHolder, "_commit_completion", commit_with_lost_ack
    )
    request = {
        "endpoint": endpoint,
        "owner_password_file": secret,
        "claim": claim,
        "dump": dump,
        "reader": read_restore_fingerprint,
        "verifier": verifier,
        "evidence": store,
        "consumer_environment": {},
        "host_guard": lambda: None,
    }
    result = cli.run_restore_maintenance(**request)
    assert result == ("restore_completed" if applied else "restore_reopen_not_applied")
    assert len(pids) == 2 and pids[0] != pids[1] and commits == [applied]
    assert verifier_pids == [(pids[0], endpoint.owner_role, "ai_employee_app", "on")]
    monkeypatch.setattr(maintenance.RestoreMaintenanceHolder, "_commit_completion", original_commit)
    if not applied:
        assert cli.run_restore_maintenance(**request) == "restore_reopen_not_applied"
        assert (
            cli.run_restore_maintenance(**request, approve_reopen_ordinal=1)
            == "restore_reopen_not_applied"
        )
        assert (
            cli.run_restore_maintenance(**request, approve_reopen_ordinal=2) == "restore_completed"
        )
        assert len(verifier_pids) == 1
    engine = create_engine(
        empty_migration_database.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            count = connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events WHERE event_type='database.restore.completed'"
                )
            )
            assert count == 1
            assert connection.scalar(text("SELECT bool_and(NOT is_active) FROM users")) is True
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM pg_stat_activity WHERE pid=:pid"), {"pid": pids[0]}
                )
                == 0
            )
    finally:
        engine.dispose()


class FakeBackupRemote:
    """模拟独占target/run对象存储，记录写/删顺序及可注入的未知失败；不连接真实remote。"""

    def __init__(self, module, now: datetime) -> None:
        self.module, self.now = module, now
        self.objects: dict[tuple[str, str], tuple[bytes, datetime]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_copy: int | None = None

    def inventory(self, target_digest):
        return tuple(
            self.module.RemoteBackupObject(path, len(raw), hashlib.sha256(raw).hexdigest(), at)
            for (target, path), (raw, at) in sorted(self.objects.items())
            if target == target_digest
        )

    def copy(self, source, target_digest, relative_path):
        self.calls.append(("copy", relative_path))
        assert (target_digest, relative_path) not in self.objects
        self.objects[target_digest, relative_path] = (source.read_bytes(), self.now)
        if sum(operation == "copy" for operation, _ in self.calls) == self.fail_copy:
            raise self.module.BackupManifestError("synthetic_remote_ack_unknown")

    def read(self, target_digest, relative_path, *, limit):
        return self.objects[target_digest, relative_path][0][: limit + 1]

    def delete(self, target_digest, relative_path):
        self.calls.append(("delete", relative_path))
        del self.objects[target_digest, relative_path]


@pytest.mark.parametrize("failure", (None, 1, 2, 3))
def test_remote_manifest_last_conflict_and_owned_compensation(tmp_path, failure) -> None:
    """remote要求整组三件套、跨run同basename冲突；未知copy仅补偿自己的精确prefix。"""
    module, dump, _ = _manifest_group(tmp_path)
    group = module._group_at(dump)
    remote = FakeBackupRemote(module, datetime(2030, 1, 1, tzinfo=UTC))
    remote.fail_copy = failure
    run = "00000000-0000-0000-0000-000000000711"
    kwargs = {"target_digest": "a" * 64, "run_id": run, "assert_locked": lambda: None}
    if failure is not None:
        with pytest.raises(module.BackupManifestError, match="synthetic_remote_ack_unknown"):
            module.publish_remote_backup(group, remote, **kwargs)
        assert not remote.objects
    else:
        receipt = module.publish_remote_backup(group, remote, **kwargs)
        assert [path for op, path in remote.calls if op == "copy"] == [
            run + "/" + dump.name,
            run + "/" + dump.name + ".sha256",
            run + "/" + dump.name + ".manifest.json",
        ]
        with pytest.raises(module.BackupManifestError, match="collision"):
            module.publish_remote_backup(
                group, remote, **{**kwargs, "run_id": "00000000-0000-0000-0000-000000000712"}
            )
        module.publish_remote_backup(group, remote, **{**kwargs, "target_digest": "b" * 64})
        module.discard_remote_backup(remote, receipt, assert_locked=lambda: None)
        assert all(target == "b" * 64 for target, _ in remote.objects)
        assert next(path for op, path in remote.calls if op == "delete").endswith(".manifest.json")


def test_remote_retention_counts_current_group_and_rejects_malformed_inventory(tmp_path) -> None:
    """本次complete也占用当日保留点；畸形adapter事实必须稳定拒绝而非绕过清理规则。"""
    from uuid import UUID

    module, dump, payload = _manifest_group(tmp_path)
    now = datetime(2030, 1, 21, tzinfo=UTC)
    remote = FakeBackupRemote(module, now)
    runs = []
    for day in range(20):
        run = str(UUID(int=day + 1))
        runs.append(run)
        created = now - timedelta(days=day)
        payload["created_at"] = created.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        _rewrite_manifest_group(dump, payload)
        for member in module._group_at(dump).members:
            remote.objects["a" * 64, run + "/" + member.path.name] = (
                member.path.read_bytes(),
                created,
            )
    expected = module.retained_backup_names(
        {run: now - timedelta(days=day) for day, run in enumerate(runs)}
    )
    module.cleanup_remote_backups(
        remote, target_digest="a" * 64, current_run=runs[0], now=now, assert_locked=lambda: None
    )
    assert {path.split("/", 1)[0] for _, path in remote.objects} == expected
    remote.inventory = lambda target: (object(),)
    with pytest.raises(module.BackupManifestError, match="inventory_invalid"):
        module.cleanup_remote_backups(
            remote, target_digest="a" * 64, current_run=runs[0], now=now, assert_locked=lambda: None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("sealed", (False, True))
async def test_backup_cancel_drains_remote_publication_and_keeps_receipt(
    tmp_path, monkeypatch, sealed
) -> None:
    """取消发生在最后remote copy完成后仍收回receipt；本地和remote补偿必须在锁内结束。"""
    import asyncio
    import threading
    from types import SimpleNamespace

    from ai_employee.cli import postgres_backup as backup

    stage = tmp_path / ".partial.00000000-0000-0000-0000-000000000711"
    stage.mkdir(mode=0o700)
    module, dump, payload = _manifest_group(stage)
    remote = FakeBackupRemote(module, datetime(2030, 1, 1, tzinfo=UTC))
    entered, release = threading.Event(), threading.Event()
    original = remote.copy

    def copy(*args):
        original(*args)
        if args[-1].endswith(".manifest.json"):
            entered.set()
            assert release.wait(5)

    remote.copy = copy
    publisher = backup._Publisher(
        backup.BackupRequest(
            tmp_path,
            "synthetic",
            payload["immutable_image_id"],
            payload["executable_digests"],
            "0.1.0",
            tmp_path / "synthetic-secret",
            sealed,
        ),
        SimpleNamespace(assert_live=lambda: None, target_identity_digest="a" * 64),
        SimpleNamespace(assert_live=lambda: None),
        stage,
        "00000000-0000-0000-0000-000000000711",
        {},
        remote,
    )
    monkeypatch.setattr(publisher, "_prepare", lambda path: None)
    monkeypatch.setattr(publisher, "_fresh", lambda: None)

    async def verify():
        return None

    task = asyncio.create_task(
        publisher.publish(dump, tmp_path / dump.name, SimpleNamespace(verify=verify))
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not remote.objects
    assert not list(tmp_path.glob("*.dump.enc*"))


def test_local_cleanup_preserves_unknown_custody_and_reports_final_fsync(
    tmp_path, monkeypatch
) -> None:
    """进入private目录后的未知成员不得被搬到公共目录或静默遗留；原候选安全返还。"""
    module = _manifest_module()
    target = tmp_path / "ai_employee-expired.dump.enc"
    target.write_bytes(b"synthetic")
    target.chmod(0o600)
    now = datetime(2030, 1, 2, tzinfo=UTC)
    old = (now - timedelta(hours=2)).timestamp()
    os.utime(target, (old, old))
    actual_rename = os.rename
    foreign = []

    def add_unknown(source, destination, **kwargs):
        actual_rename(source, destination, **kwargs)
        custody = next(tmp_path.glob(".postgres-backup-custody-*"))
        path = custody / "foreign-unrecognized"
        path.write_bytes(b"synthetic-foreign")
        foreign.append(path)

    monkeypatch.setattr(module.os, "rename", add_unknown)
    with pytest.raises(module.BackupManifestError):
        module.cleanup_local_backups(
            directory=tmp_path,
            current_run="00000000-0000-0000-0000-000000000701",
            now=now,
            assert_locked=lambda: None,
        )
    assert target.read_bytes() == b"synthetic"
    assert foreign[0].read_bytes() == b"synthetic-foreign"
    assert not (tmp_path / "foreign-unrecognized").exists()


def test_state_directory_rejects_source_overlap_and_already_applied_is_no_clobber(
    tmp_path: Path,
) -> None:
    """state独立于只读备份，already-applied证据按kind隔离且冲突不可覆盖。"""
    name = "ai_employee.cli.postgres_restore"
    assert importlib.util.find_spec(name) is not None, "restore has no state-v4 projection store"
    module = importlib.import_module(name)
    maintenance = importlib.import_module("ai_employee.infrastructure.db.database_maintenance")
    source = tmp_path / "backups"
    source.mkdir(mode=0o700)
    overlap = source / "state"
    overlap.mkdir(mode=0o700)
    with pytest.raises(module.RestoreInputError):
        module.RestoreStateStore(overlap, (source,))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    store = module.RestoreStateStore(state, (source,))
    expected = maintenance.RestoreFingerprint("20260809_0018", "b" * 64)
    generic = maintenance.RestoreClaim("generic", "a" * 64, expected)
    sealed = maintenance.RestoreClaim("sealed_0018", "a" * 64, expected)
    store.already_applied("c" * 64, generic, expected)
    store.already_applied("c" * 64, generic, expected)
    store.already_applied("c" * 64, sealed, expected)
    paths = sorted(state.glob("*.already-applied.json"))
    assert len(paths) == 2
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    assert all("created_at" not in json.loads(path.read_text()) for path in paths)
    for path in paths:
        value = json.loads(path.read_text())
        assert set(value) == {
            "schema",
            "kind",
            "target_identity_digest_v1",
            "source_binding_digest_v1",
            "manifest_sha256",
            "expected_revision",
            "expected_restore_fingerprint_v1",
            "observed_revision",
            "observed_restore_fingerprint_v1",
            "result_code",
        }
        assert value["manifest_sha256"] == value["source_binding_digest_v1"] == "a" * 64
        assert value["result_code"] == "restore_already_applied"
    generic_path = next(path for path in paths if ".generic." in path.name)
    generic_path.write_bytes(b"foreign")
    with pytest.raises(module.RestoreInputError):
        store.already_applied("c" * 64, generic, expected)
    assert generic_path.read_bytes() == b"foreign"


def _manifest_module():
    """验证纯文件三件套边界存在，所有输入均来自当前测试临时目录。"""
    name = "ai_employee.infrastructure.db.postgres_backup_manifest"
    assert importlib.util.find_spec(name) is not None, (
        "backup has no manifest-bound group validator"
    )
    return importlib.import_module(name)


def test_backup_group_recheck_accepts_read_atime_but_rejects_replaced_inode(tmp_path):
    """真实文件首次读取会推进atime；重新验证应保持相等，但同字节的新inode必须判为漂移。"""
    module, dump, _ = _manifest_group(tmp_path)
    for path in (dump, Path(str(dump) + ".sha256"), Path(str(dump) + ".manifest.json")):
        os.utime(path, ns=(1_000_000_000, path.stat().st_mtime_ns))
    before = module._group_at(dump)
    after = module._group_at(dump)
    assert before.members[0].stat.st_atime_ns != after.members[0].stat.st_atime_ns
    assert before == after, "content-preserving filesystem read changed the frozen group"
    replacement = tmp_path / "owned-replacement"
    replacement.write_bytes(dump.read_bytes())
    replacement.chmod(0o600)
    os.replace(replacement, dump)
    assert module._group_at(dump) != before


def _manifest_group(tmp_path: Path):
    """生成闭合 schema 的合成三件套；测试校验和不调用生产序列化/哈希函数。"""
    module = _manifest_module()
    dump = tmp_path / "synthetic.dump.enc"
    dump.write_bytes(b"synthetic-ciphertext")
    payload = {
        "schema": "ai_employee.postgres_backup_manifest.v1",
        "dump_basename": dump.name,
        "created_at": "2030-01-01T00:00:00.000000Z",
        "postgres_server_version": "17.5",
        "alembic_revision": "20260809_0019",
        "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "dump_size": dump.stat().st_size,
        "checksum_filename": dump.name + ".sha256",
        "immutable_image_id": "sha256:" + "a" * 64,
        "build_metadata": {"release_version": "0.1.0"},
        "restore_fingerprint_v1": "b" * 64,
        "executable_digests": {name: "c" * 64 for name in module.OPERATIONS_EXECUTABLES},
    }
    _rewrite_manifest_group(dump, payload)
    return module, dump, payload


def _rewrite_manifest_group(dump: Path, payload: dict[str, object]) -> None:
    """独立写 canonical manifest 和双成员 checksum，保留变异 fixture 的精确字节。"""
    manifest = Path(f"{dump}.manifest.json")
    manifest.write_bytes(
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    Path(f"{dump}.sha256").write_text(
        f"{hashlib.sha256(dump.read_bytes()).hexdigest()}  {dump.name}\n"
        f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  {manifest.name}\n",
        encoding="ascii",
    )
    for path in (dump, manifest, Path(f"{dump}.sha256")):
        path.chmod(0o600)


@pytest.mark.parametrize(
    "mutation",
    (
        "extra-key",
        "duplicate-key",
        "size",
        "revision",
        "image",
        "script",
        "checksum-extra",
        "mode",
        "symlink",
        "timestamp",
        "fingerprint",
    ),
)
def test_manifest_group_rejects_each_binding_or_ownership_drift(
    tmp_path: Path, mutation: str
) -> None:
    """无 owner 连接的纯文件校验必须交叉验证完整组，不能把任何一件当独立成功备份。"""
    module, dump, payload = _manifest_group(tmp_path)
    options = {
        "supported_revisions": frozenset({"20260809_0019"}),
        "expected_image_id": "sha256:" + "a" * 64,
        "expected_executable_digests": dict(payload["executable_digests"]),
    }
    assert (
        module.validate_backup_group(dump, **options).manifest.alembic_revision == "20260809_0019"
    )
    if mutation in {"extra-key", "size", "revision", "image", "script", "timestamp", "fingerprint"}:
        if mutation == "extra-key":
            payload["unapproved"] = "synthetic"
        if mutation == "size":
            payload["dump_size"] = 999
        if mutation == "revision":
            payload["alembic_revision"] = "unknown"
        if mutation == "image":
            payload["immutable_image_id"] = "sha256:" + "d" * 64
        if mutation == "script":
            payload["executable_digests"][next(iter(payload["executable_digests"]))] = "e" * 64
        if mutation == "timestamp":
            payload["created_at"] = "2030-01-01T08:00:00+08:00"
        if mutation == "fingerprint":
            payload["restore_fingerprint_v1"] = "B" * 64
        _rewrite_manifest_group(dump, payload)
    elif mutation == "duplicate-key":
        manifest = Path(f"{dump}.manifest.json")
        manifest.write_bytes(manifest.read_bytes().replace(b"{", b'{"schema":"duplicate",', 1))
    elif mutation == "checksum-extra":
        with Path(f"{dump}.sha256").open("a") as stream:
            stream.write(f"{'0' * 64}  another.dump.enc\n")
    elif mutation == "mode":
        dump.chmod(0o640)
    elif mutation == "symlink":
        moved = tmp_path / "other"
        dump.rename(moved)
        dump.symlink_to(moved)
    with pytest.raises(module.BackupManifestError):
        module.validate_backup_group(dump, **options)


def test_group_publication_is_no_clobber_and_preserves_foreign_member(tmp_path: Path) -> None:
    """最终名任一冲突不得覆盖；失败补偿只撤回本次 receipt，不删除外来替换者。"""
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    destination = tmp_path / "published"
    destination.mkdir(mode=0o700)
    module, dump, _ = _manifest_group(stage)
    foreign = destination / dump.name
    foreign.write_bytes(b"foreign")
    foreign.chmod(0o600)
    with pytest.raises(module.BackupManifestError):
        module.publish_backup_group(dump, destination)
    assert foreign.read_bytes() == b"foreign"
    foreign.unlink()
    receipt = module.publish_backup_group(dump, destination)
    replacement = destination / "replacement"
    replacement.write_bytes(b"replacement")
    replacement.chmod(0o600)
    replacement.replace(foreign)
    module.discard_backup_publication(receipt)
    assert foreign.read_bytes() == b"replacement"
    assert not Path(f"{foreign}.manifest.json").exists()
    assert not Path(f"{foreign}.sha256").exists()


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    """本模块只在显式可抛弃 fixture 内迁移，文件拒绝测试保持数据库零访问。"""
    yield


@pytest.fixture(autouse=True)
async def isolated_database() -> AsyncIterator[None]:
    """不让全局 TRUNCATE 掩盖 restore 自己的事务和恢复结果。"""
    yield


def _executable(path: Path, source: str) -> None:
    """写入不接触网络的本测试专有命令替身，输出仅为计数标记。"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def test_generic_rejects_legacy_bytes_before_any_restore_child(tmp_path: Path) -> None:
    """只有旧 checksum 的字节绝不能直接进入 generic 恢复，即使解密命令可成功。"""
    dump = tmp_path / "synthetic-legacy.dump.enc"
    dump.write_bytes(b"synthetic-encrypted-backup")
    dump.chmod(0o600)
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    checksum = Path(f"{dump}.sha256")
    checksum.write_text(f"{digest}  {dump.name}\n", encoding="ascii")
    checksum.chmod(0o600)
    commands = tmp_path / "commands"
    commands.mkdir(mode=0o700)
    calls = tmp_path / "calls"
    _executable(commands / "openssl", '#!/bin/sh\nprintf "decrypt\\n" >> "$TEST_CALLS"\n')
    _executable(commands / "pg_restore", '#!/bin/sh\nprintf "restore\\n" >> "$TEST_CALLS"\n')
    secret = tmp_path / "synthetic-passphrase"
    secret.write_text("synthetic-only", encoding="ascii")
    secret.chmod(0o600)
    environment = {
        **os.environ,
        "APP_ENV": "test",
        "BACKUP_PASSPHRASE_FILE": str(secret),
        "TEST_CALLS": str(calls),
        "PATH": f"{commands}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/restore-postgres.sh"), str(dump)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not calls.exists(), "generic restore decrypted/restored a legacy file without a manifest"
    assert result.returncode != 0


def test_release_wrapper_explicitly_executes_fixed_audit_subprocess(tmp_path: Path) -> None:
    """初始 release 门禁必须直接执行 audit 子进程，不能把 just ci 当作替代。"""
    release = ROOT / "scripts/test-m2-release.sh"
    assert release.is_file(), "release wrapper has no explicit Task13 audit subprocess"
    commands = tmp_path / "commands"
    commands.mkdir(mode=0o700)
    calls = tmp_path / "audit-called"
    _executable(
        commands / "bash",
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        'if sys.argv[1:] != ["scripts/test-calendar-aad-0019-audit.sh"]:\n'
        "    raise SystemExit(81)\n"
        "from urllib.parse import urlsplit\n"
        'url = urlsplit(os.environ.get("TEST_DATABASE_URL", ""))\n'
        "if (url.scheme, url.hostname, url.port, url.path) != "
        '("postgresql+asyncpg", "127.0.0.1", 55443, "/ai_employee_task13_test"):\n'
        "    raise SystemExit(82)\n"
        'pathlib.Path(os.environ["TEST_CALLS"]).write_text("audit-executed")\n',
    )
    result = subprocess.run(
        ["/bin/bash", str(release)],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}", "TEST_CALLS": str(calls)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert calls.read_text() == "audit-executed"
