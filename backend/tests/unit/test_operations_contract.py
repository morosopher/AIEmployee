"""冻结 M2 运维手册的安全协议与生命周期交接，不把文字检查当作发布演练证据。"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _document(path: str) -> str:
    """读取仓库内 UTF-8 文档并归一化换行，允许排版变化但保留协议字节和顺序。"""
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


@pytest.mark.parametrize(
    "fragments",
    (
        pytest.param(
            (
                "effective_deadline = min(original_artifact_deadline, current_deadline)",
                "历史 `oauth.refresh_confirmed.rollout_deadline_candidate`",
                "Task 16A 是仓库首次创建并发布 `20260809_0019`",
                "Task 27C/27D 的运维契约",
                "0016～0018",
                "resume candidate",
                "autocommit",
                "0019-last-callback 全 rollback",
            ),
            id="migration-deadline-and-resume",
        ),
        pytest.param(
            (
                "frame(raw_bytes) = uint32_be(len(raw_bytes)) || raw_bytes",
                "67ba9e40f2157a49de2d987e1f4d8e61c2a78a7d71d4da7ce13421fde58a8e70",
                "52e707318449a55779a023931f5914909d662944f5364545e1795edbcf555dad",
                "0bdf656c1b037423e71c950df9f6bb5c56a737bcef8fe7d6e58e26d7dee04370",
                "四个 Uvicorn 入口都必须关闭默认 access log",
                "发布 canary 必须启动真实 Uvicorn 子进程",
                "stdout、 stderr、JSON logs 中三个值零命中",
            ),
            id="framed-aad-and-four-entry-canary",
        ),
        pytest.param(
            (
                "PostgreSQL 17",
                "owner `is_grantable=false`",
                "`SUPERUSER/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS=false`",
                "`CONNLIMIT=-1`、`VALID UNTIL NULL`、 `rolconfig NULL`",
                "以任一角色作为 `roleid` 或 `member`",
                "fresh/default 只允许两角色 同时缺失或同时已安全",
                "pre-protocol legacy 要求两角色都已安全",
                "role-bootstrap→typed migration 且无 post-repair",
                "pre-step grant drift 零写",
                "per-step new-object/policy delta",
            ),
            id="role-bootstrap-and-grant-inventory",
        ),
        pytest.param(
            (
                "ai_employee.postgres_backup_manifest.v1",
                "manifest-last",
                "SCHEMA_LIFECYCLE_LOCK=(20260806, 143)",
                "management lifecycle lock → target lock → schema lifecycle lock",
                "global backup lock/`${BACKUP_DIR}` flock",
                "pre-post revision CAS",
                "`ai_employee.maintenance_gate`",
                "`ai_employee.restore_call_authority`",
                "`ai_employee.restore_completion`",
                "`.env.example`",
                "ai_employee.postgres_restore_state.v4",
            ),
            id="backup-locks-and-restore-catalog",
        ),
        pytest.param(
            (
                "restore_already_applied",
                "`psql_calls=0`",
                "数据库写入、audit、gate/call/completion GUC mutation 全为零",
                "ai_employee.postgres_restore_already_applied.v1",
                "SQL-byte-zero",
                "backend-ready-before-feed",
                "deferred completion guard",
                "mid-stream EOF rollback",
                "call_ordinal",
                "RESET completion",
                "zero-slot 重算",
                "audit/local projection 可缺失，completed call 不可缺失",
                "exact ACL、safe roles、zero membership",
                "SET ROLE ai_employee_app",
                "SQLSTATE `25006`",
            ),
            id="restore-stream-and-atomic-reopen",
        ),
        pytest.param(
            (
                "just restore-legacy-to-isolated",
                ".legacy-conversion-registry",
                "just legacy-backup-scavenge",
                "just calendar-aad-restore-0018 file",
                "generic/sealed 共享同一 admission/access/grant primitive",
                "obsolete 0019",
                "非生产目标创建取证备份",
                "生产发现任何未知、同名或 obsolete 0019 时立即 停止发布",
                "只有 `post-resync` 审计通过后，才启动",
            ),
            id="legacy-sealed-and-release-boundaries",
        ),
        pytest.param(
            (
                "## M2 数据保留与隐私删除",
                "30/180/365",
                "description_aad_version",
                "location_aad_version",
                "action_content_expired",
                "source_thread_id IS NOT NULL OR source_message_id IS NOT NULL",
                "target_event_id IS NOT NULL",
                "checkpoint_migrations",
                "adelete_thread",
                "连续持有 TaskRun→user 锁",
            ),
            id="lifecycle-content-and-checkpoints",
        ),
        pytest.param(
            (
                "LOCK TABLE oauth_connections IN EXCLUSIVE MODE NOWAIT",
                "LOCK TABLE encrypted_credentials IN EXCLUSIVE MODE NOWAIT",
                "oauth.refresh_started",
                "oauth.refresh_confirmed",
                "oauth.refresh_recovery_authorization_started",
                "oauth.refresh_recovery_unsatisfied",
                "oauth.refresh_credential_replaced",
                "组内最大 `created_at < cutoff`",
                "普通审计清理不读取 token、密文、expiry、generation 或后续 lineage",
            ),
            id="lifecycle-oauth-closed-groups",
        ),
        pytest.param(
            (
                "privacy_deletion_started.v1",
                "privacy_reconciliation_started",
                "INACTIVE_ALL_DATA_RECOVERY",
                "task.execute:{task_id}:inactive-deletion-recovery:{bucket_start_utc_iso8601}",
                "privacy.deletion_completed",
                "credential 删除提交后",
                "Microsoft",
                "UNKNOWN",
                "DELETE ALL DATA",
                "删除本地数据不能撤回已发送的邮件或已生效的日程变更",
                "不得手工重新激活用户",
                "database.restore.completed",
                "唯一匿名用户",
            ),
            id="lifecycle-deletion-recovery-and-warning",
        ),
    ),
)
def test_operations_preserves_frozen_protocols(fragments: tuple[str, ...]) -> None:
    """每组只检查稳定协议标识与关键禁令，缺失时报告完整差集供运维交接定位。"""
    document = _document("docs/operations.md")
    assert not [fragment for fragment in fragments if fragment not in document]


def test_operations_database_acl_sql_and_tuple_multisets_are_exact() -> None:
    """四个 ACL profile 必须完整且 owner 不可授予；文档不得退化成最低权限子集。"""
    document = _document("docs/operations.md")
    sql = """SELECT acl.grantee, acl.grantor, acl.privilege_type, acl.is_grantable
        FROM pg_database AS db CROSS JOIN LATERAL aclexplode(
            COALESCE(db.datacl, acldefault('d', db.datdba))
        ) AS acl WHERE db.oid = :target_database_oid"""
    profiles = """fresh_default =
        (O, O, CREATE, false) (O, O, CONNECT, false) (O, O, TEMPORARY, false)
        (P, O, CONNECT, false) (P, O, TEMPORARY, false)
        pre_protocol_legacy = fresh_default + (A, O, CONNECT, false) (R, O, CONNECT, false)
        baseline =
        (O, O, CREATE, false) (O, O, CONNECT, false) (O, O, TEMPORARY, false)
        (A, O, CONNECT, false) (R, O, CONNECT, false)
        active = (O, O, CREATE, false) (O, O, CONNECT, false) (O, O, TEMPORARY, false)
        """
    assert f"```sql {' '.join(sql.split())} ```" in document
    assert f"```text {' '.join(profiles.split())} ```" in document


def test_acceptance_requires_lifecycle_and_inherited_operator_evidence() -> None:
    """验收清单必须要求执行证据，保留 Task27D 边界且明确任务验证不等于发布签署。"""
    document = _document("docs/acceptance-checklist.md")
    required = (
        "任务级验证不等于发布验收",
        "30/180/365",
        "privacy.deletion_started",
        "INACTIVE_ALL_DATA_RECOVERY",
        "privacy_reconciliation_started",
        "database.restore.completed",
        "四个 Uvicorn 入口",
        "effective_deadline",
        "Task 16A/27C",
        "framed AAD",
        "四个 exact ACL tuple multiset",
        "is_grantable=false",
        "0016～0018",
        "0019-last-callback",
        "manifest",
        "state-v4",
        "zero-slot",
        "already-applied",
        "deferred guard",
        "legacy",
        "sealed",
        "obsolete 0019",
        "post-resync",
        "just ci",
        "DELETE ALL DATA",
    )
    assert not [fragment for fragment in required if fragment not in document]
