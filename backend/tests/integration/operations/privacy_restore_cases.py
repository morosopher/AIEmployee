"""把真实 restore 完成事实与 Task27E 用户数据生命周期连接，不改动恢复服务。

复用官方 typed migration 和 holder 最终事务；清理通过真实 app/retention 登录执行。
catalog 准入只验证互相绑定的完成事实和完整权限，不以审计行或业务 fingerprint 代替。
"""

import asyncio
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import URL, create_engine, event, select, text
from sqlalchemy.pool import NullPool

from ai_employee.cli import database_maintenance as cli
from ai_employee.cli.verify_restored_backup import read_restore_fingerprint, verify_restored_backup
from ai_employee.infrastructure.db import database_maintenance as maintenance
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.privacy_checkpoints import (
    PostgresPrivacyCheckpointCleaner,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.workers.privacy import PrivacyDeletionWorker
from ai_employee.workers.retention import RetentionCleanupWorker
from tests.integration.privacy.test_all_data_deletion import (
    BARRIER_NOW,
    _DeletionClock,
    _seed_owned_deletion_lease,
)

_USER_ID = UUID("00000000-0000-0000-0000-000000000701")


async def _run_cleanup(database_url: URL, *, mode: str) -> None:
    """各入口使用原角色，无恢复catalog读取或权限提升；维护重入前必须释放运行时池。"""
    app_url = database_url.set(username="ai_employee_app", password="app-role-integration-password")
    retention_url = database_url.set(
        username="ai_employee_retention", password="retention-role-integration-password"
    )
    app = build_session_factory(app_url.render_as_string(hide_password=False))
    retention = build_session_factory(retention_url.render_as_string(hide_password=False))
    checkpoint_cleaner = PostgresPrivacyCheckpointCleaner(
        app_url.render_as_string(hide_password=False)
    )
    try:
        if mode == "retention":
            assert (
                await RetentionCleanupWorker(
                    retention, checkpoint_cleaner=checkpoint_cleaner
                ).execute(
                    now=BARRIER_NOW,
                    batch_size=1,
                )
                == 1
            )
        else:
            lease = await _seed_owned_deletion_lease(
                app, user_id=_USER_ID, request_id="synthetic-restore-privacy"
            )
            await PrivacyDeletionWorker(
                retention,
                checkpoint_cleaner=checkpoint_cleaner,
                clock=_DeletionClock(),
            ).delete_all_data(
                user_id=_USER_ID,
                task_id=lease.task_id,
                request_id="synthetic-restore-privacy",
                lease_owner=lease.lease_owner,
                lease_mode=lease.lease_mode,
                batch_size=1,
            )
        async with app() as session:
            users = (await session.scalars(select(UserModel))).all()
            assert len(users) == 1 and users[0].id == _USER_ID
            assert users[0].is_active is (mode == "retention")
            audits = (
                await session.scalars(
                    select(AuditEventModel).where(AuditEventModel.user_id == _USER_ID)
                )
            ).all()
            assert [row.event_type for row in audits] == [
                "retention.cleanup_completed"
                if mode == "retention"
                else "privacy.deletion_completed",
            ]
            assert not (await session.scalars(select(TaskRunModel.id))).all()
            if mode == "privacy":
                assert users[0].password_hash is None
                assert users[0].email == f"deleted-{_USER_ID}@invalid.local"
    finally:
        await retention.dispose()
        await app.dispose()


def assert_restore_completion_survives_user_audit_cleanup(
    database_url: URL,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
) -> None:
    """删除真实完成审计后，fresh holder 仍按原精确pair完成准入/ACK，后续恢复可绑定匿名用户。"""
    from tests.integration.operations.test_postgres_backup_restore import _restore_fixture

    endpoint, secret, _, store, dump = _restore_fixture(database_url, tmp_path)
    engine = create_engine(
        database_url.set(drivername="postgresql+psycopg"), poolclass=NullPool, hide_parameters=True
    )
    try:
        # 初始inactive是恢复fixture的特性；在首个完成事实生成前准备真实活动用户。
        # 此处没有把已匿名用户重新启用，后续全数据删除的匿名状态必须一直保留。
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE users SET is_active=true WHERE id=:user"), {"user": _USER_ID}
            )

        def fingerprint():
            """仅为新恢复claim冻结业务数据；已完成ACK从不拿此值检查审计是否还在。"""
            with engine.connect() as connection, connection.begin():
                connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                return read_restore_fingerprint(connection)

        def complete(claim):
            """真实gate/phase/最终commit产生精确facts；无restore subprocess、无手工GUC。"""
            captured = []
            original_commit = maintenance.RestoreMaintenanceHolder._commit_completion

            def capture(self, transaction, before, completed):
                unknown = maintenance.RestoreCompletionAckUnknown(before, completed)
                original_commit(self, transaction, before, completed)
                captured.append(unknown)

            with (
                cli._open_maintenance_context(cli.MigrateArguments(secret), endpoint) as context,
                context.acquire_management_lifecycle_lock() as management,
                management.acquire_restore_target_lock(claim) as target,
                target.acquire_schema_lifecycle_lock(),
                target.restore_holder(claim) as holder,
            ):
                holder._establish_gate(holder.admit(), store)
            with monkeypatch.context() as scoped:
                scoped.setattr(maintenance.RestoreMaintenanceHolder, "_commit_completion", capture)
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
            assert len(captured) == 1
            return captured[0]

        def admit_and_ack(claim, unknown):
            """每次全新holder验证catalog pair、gate缺席和基线ACL；不读取用户审计数据。"""
            statements = []

            def observe(connection, cursor, statement, parameters, context, executemany):
                del connection, cursor, parameters, context, executemany
                statements.append(statement.lower())

            with (
                cli._open_maintenance_context(cli.MigrateArguments(secret), endpoint) as context,
                context.acquire_management_lifecycle_lock() as management,
                management.acquire_restore_target_lock(claim) as target,
                target.acquire_schema_lifecycle_lock(),
                target.restore_holder(claim) as holder,
            ):
                event.listen(holder.connection, "before_cursor_execute", observe)
                try:
                    assert holder.admit() == unknown.completed
                    assert unknown.completed.maintenance_gate is None
                    assert holder.reconcile_completion_ack(unknown, store) == "restore_completed"
                    assert holder.read_facts() == unknown.completed
                finally:
                    event.remove(holder.connection, "before_cursor_execute", observe)
            assert not any(
                "from public.audit_events" in statement or "from audit_events" in statement
                for statement in statements
            )

        first_claim = maintenance.RestoreClaim("generic", "b" * 64, fingerprint())
        first = complete(first_claim)
        admit_and_ack(first_claim, first)
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM audit_events WHERE event_type='database.restore.completed'"
                    )
                )
                == 1
            )

        asyncio.run(_run_cleanup(database_url, mode=mode))
        for _ in range(2):
            admit_and_ack(first_claim, first)

        # 后续恢复以清理后的真实内容冻结新claim；旧completion目录事实只作为前驱digest。
        # 最终事务仍绑定唯一用户，无需恢复邮箱地址、密码或活动状态。
        next_claim = maintenance.RestoreClaim("generic", "c" * 64, fingerprint())
        following = complete(next_claim)
        admit_and_ack(next_claim, following)
        assert following.completed != first.completed
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM users")) == 1
            assert connection.scalar(
                text("SELECT is_active FROM users WHERE id=:user"), {"user": _USER_ID}
            ) is (mode == "retention")
            audits = connection.execute(
                text(
                    "SELECT user_id,task_id,actor_id FROM audit_events WHERE event_type='database.restore.completed'"
                )
            ).all()
            assert [tuple(row) for row in audits] == [(_USER_ID, None, "database_restore")]
    finally:
        engine.dispose()
