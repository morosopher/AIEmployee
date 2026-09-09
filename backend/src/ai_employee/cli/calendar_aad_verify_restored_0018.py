"""sealed0018独立纯文件验证及既有owner holder上的app只读回调。

没有独立CLI、连接、grant或post-reopen验证入口。窗口期限用于向前rollout；恢复发生在
截止线到达后也可能合法，因此纯输入验证只绑定原始期限，恢复回调不延长或重建它。
"""

from __future__ import annotations

import os
from collections.abc import Callable

from sqlalchemy import Connection, text

from ai_employee.application.use_cases.calendar_aad_rollout import CalendarAadRolloutError
from ai_employee.cli.calendar_aad_audit_0019 import load_audit, load_preflight, read_audit_snapshot
from ai_employee.cli.verify_restored_backup import verify_restored_backup
from ai_employee.infrastructure.db.database_maintenance import RestoreFingerprint
from ai_employee.infrastructure.db.postgres_backup_manifest import ValidatedBackupGroup
from ai_employee.infrastructure.db.repositories.calendar_aad_preflight import (
    read_calendar_aad_facts,
)


def validate_sealed_restore_input(group: ValidatedBackupGroup) -> None:
    """在任何Secret/服务/连接前额外要求0018、原preflight/pre-migration与同一真实image。"""
    if (
        group.manifest.alembic_revision != "20260809_0018"
        or os.environ.get("CALENDAR_AAD_RESTORE_IMAGE_ID") != group.manifest.immutable_image_id
    ):
        raise CalendarAadRolloutError("calendar_aad_restore_binding_invalid")
    preflight, _ = load_preflight(group)
    baseline = load_audit(group, "pre-migration")
    if (
        baseline.pair_digests != preflight.pair_digests
        or baseline.connection_digests != preflight.connection_digests
    ):
        raise CalendarAadRolloutError("calendar_aad_restore_binding_invalid")


def sealed_restore_verifier(
    group: ValidatedBackupGroup,
) -> Callable[[Connection, RestoreFingerprint], None]:
    """冻结已验证原artifact；回调在同一holder SET ROLE app/READ ONLY/25006后才运行。"""
    validate_sealed_restore_input(group)
    baseline = load_audit(group, "pre-migration")

    def verify(connection: Connection, expected: RestoreFingerprint) -> None:
        """先完整manifest fingerprint，再比较0018事件/目录/credential及游标精确摘要。"""
        verify_restored_backup(connection, expected)
        if connection.scalar(text("SELECT current_user")) != "ai_employee_app":
            raise CalendarAadRolloutError("calendar_aad_restore_verifier_invalid")
        facts = read_calendar_aad_facts(connection, expected_revision="20260809_0018")
        snapshot = read_audit_snapshot(connection, revision="20260809_0018")
        if (
            facts.pair_digests != baseline.pair_digests
            or facts.connection_digests != baseline.connection_digests
            or snapshot != baseline.snapshot
        ):
            raise CalendarAadRolloutError("calendar_aad_restore_verification_failed")

    return verify
