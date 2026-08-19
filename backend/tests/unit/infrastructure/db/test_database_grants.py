"""冻结 revision/phase 对象 ACL inventory 与逐步 policy delta 的闭集契约。"""

from __future__ import annotations

import inspect
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import cast

import pytest
from sqlalchemy import Connection

import ai_employee.infrastructure.db.database_grants as database_grants_module
from ai_employee.infrastructure.db.database_access import (
    APP_RUNTIME_ROLE_NAME,
    RETENTION_RUNTIME_ROLE_NAME,
)
from ai_employee.infrastructure.db.database_grants import (
    COLUMN_GRANTS_SQL,
    SCHEMA_GRANTS_SQL,
    SEQUENCE_GRANTS_SQL,
    TABLE_GRANTS_SQL,
    CatalogObjectSnapshot,
    GrantPhase,
    MigrationGrantDelta,
    ObjectGrantInvariantError,
    ObjectGrantTuple,
    ObjectKind,
    _expected_object_grants,
    _migration_grant_delta,
    _verify_exact_grant_multiset,
    apply_migration_grant_delta,
    read_object_grants,
    verify_object_grants,
    verify_pre_migration_object_grants,
)

RELATION_OWNER = "synthetic_owner"
OTHER_GRANTOR = "unexpected_grantor"
PUBLIC_GRANTEE = "PUBLIC"
PUBLIC_SCHEMA_OWNER = "pg_database_owner"
BASE_REVISION = "base"
HEAD_REVISION = "20260809_0018"
DESTINATION_REVISION = "20260809_0019"
SPECIAL_CATALOG_OWNER_NAMES = (
    pytest.param("owner-name", id="hyphen"),
    pytest.param("所有者", id="unicode"),
    pytest.param('owner"name', id="double-quote"),
)

REVISION_ORDER = (
    BASE_REVISION,
    "20260730_0001",
    "20260730_0002",
    "20260803_0003",
    "20260803_0004",
    "20260803_0005",
    "20260803_0006",
    "20260804_0007_google_sources",
    "20260804_0007",
    "20260804_0008",
    "20260730_0004",
    "20260804_0009",
    "20260804_0010",
    "20260806_0011",
    "20260806_0012",
    "20260806_0013",
    "20260807_0014",
    "20260808_0015",
    "20260808_0016",
    "20260808_0017",
    HEAD_REVISION,
    DESTINATION_REVISION,
)

IDENTITY_TABLES = {"alembic_version", "users", "user_sessions"}
TASK_TABLES = {
    "approval_requests",
    "audit_events",
    "outbox_events",
    "task_runs",
    "task_steps",
    "tool_executions",
}
CHECKPOINT_TABLES = {
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
}
SOURCE_TABLES = {
    "calendar_events",
    "email_analyses",
    "email_messages",
    "email_threads",
    "encrypted_credentials",
    "oauth_attempts",
    "oauth_connections",
    "sync_cursors",
}
WORKSPACE_TABLES = {
    "conversations",
    "daily_brief_items",
    "daily_briefs",
    "llm_invocations",
    "messages",
}
CONNECTION_TABLES = {"connection_capabilities", "provider_calendars"}
TRUSTED_ACTION_TABLES = {
    "calendar_change_proposals",
    "calendar_change_snapshots",
    "mail_draft_versions",
    "mail_drafts",
}
ALL_TABLES = (
    IDENTITY_TABLES
    | TASK_TABLES
    | CHECKPOINT_TABLES
    | SOURCE_TABLES
    | WORKSPACE_TABLES
    | CONNECTION_TABLES
    | TRUSTED_ACTION_TABLES
)

TABLES_BY_REVISION: dict[str, set[str]] = {
    BASE_REVISION: set(),
    "20260730_0001": set(IDENTITY_TABLES),
    "20260730_0002": IDENTITY_TABLES | TASK_TABLES,
    "20260803_0003": IDENTITY_TABLES | TASK_TABLES,
    "20260803_0004": IDENTITY_TABLES | TASK_TABLES | CHECKPOINT_TABLES,
    "20260803_0005": IDENTITY_TABLES | TASK_TABLES | CHECKPOINT_TABLES,
    "20260803_0006": IDENTITY_TABLES | TASK_TABLES | CHECKPOINT_TABLES,
    "20260804_0007_google_sources": (
        IDENTITY_TABLES | TASK_TABLES | CHECKPOINT_TABLES | SOURCE_TABLES
    ),
    "20260804_0007": IDENTITY_TABLES | TASK_TABLES | CHECKPOINT_TABLES | SOURCE_TABLES,
    "20260804_0008": IDENTITY_TABLES | TASK_TABLES | CHECKPOINT_TABLES | SOURCE_TABLES,
    "20260730_0004": (
        IDENTITY_TABLES | TASK_TABLES | CHECKPOINT_TABLES | SOURCE_TABLES | WORKSPACE_TABLES
    ),
    "20260804_0009": (
        IDENTITY_TABLES | TASK_TABLES | CHECKPOINT_TABLES | SOURCE_TABLES | WORKSPACE_TABLES
    ),
    "20260804_0010": (
        IDENTITY_TABLES | TASK_TABLES | CHECKPOINT_TABLES | SOURCE_TABLES | WORKSPACE_TABLES
    ),
    "20260806_0011": (
        IDENTITY_TABLES
        | TASK_TABLES
        | CHECKPOINT_TABLES
        | SOURCE_TABLES
        | WORKSPACE_TABLES
        | CONNECTION_TABLES
    ),
    "20260806_0012": set(ALL_TABLES),
    "20260806_0013": set(ALL_TABLES),
    "20260807_0014": set(ALL_TABLES),
    "20260808_0015": set(ALL_TABLES),
    "20260808_0016": set(ALL_TABLES),
    "20260808_0017": set(ALL_TABLES),
    HEAD_REVISION: set(ALL_TABLES),
    DESTINATION_REVISION: set(ALL_TABLES),
}

SEQUENCES_BY_REVISION: dict[str, set[str]] = {
    revision: (
        set()
        if revision in {BASE_REVISION, "20260730_0001"}
        else (
            {"audit_events_id_seq"}
            if revision in {"20260730_0002", "20260803_0003"}
            else {"audit_events_id_seq", "checkpoint_migrations_v_seq"}
        )
    )
    for revision in REVISION_ORDER
}

OWNER_TABLE_PRIVILEGES = {
    "DELETE",
    "INSERT",
    "MAINTAIN",
    "REFERENCES",
    "SELECT",
    "TRIGGER",
    "TRUNCATE",
    "UPDATE",
}
OWNER_SEQUENCE_PRIVILEGES = {"SELECT", "UPDATE", "USAGE"}
APP_TABLE_PRIVILEGES = {"SELECT", "INSERT", "UPDATE", "DELETE"}

FINAL_RETENTION_TABLE_PRIVILEGES: dict[str, set[str]] = {
    "connection_capabilities": {"SELECT", "DELETE"},
    "provider_calendars": {"SELECT", "DELETE"},
    "mail_drafts": {"SELECT", "DELETE"},
    "mail_draft_versions": {"SELECT", "DELETE"},
    "calendar_change_proposals": {"SELECT", "DELETE"},
    "calendar_change_snapshots": {"SELECT", "DELETE"},
    "oauth_attempts": {"SELECT", "DELETE"},
    "oauth_connections": {"SELECT", "DELETE"},
    "sync_cursors": {"SELECT", "DELETE"},
    "email_messages": {"SELECT", "DELETE"},
    "calendar_events": {"SELECT", "DELETE"},
    "approval_requests": {"SELECT", "DELETE"},
    "tool_executions": {"SELECT", "DELETE"},
    "task_runs": {"SELECT", "DELETE"},
    "users": {"SELECT"},
    "audit_events": {"SELECT", "INSERT", "DELETE"},
    "messages": {"SELECT", "DELETE"},
    "conversations": {"SELECT", "DELETE"},
    "daily_brief_items": {"SELECT", "DELETE"},
    "daily_briefs": {"SELECT", "DELETE"},
    "llm_invocations": {"SELECT", "DELETE"},
    "task_steps": {"SELECT", "DELETE"},
    "outbox_events": {"SELECT", "DELETE"},
    "user_sessions": {"SELECT", "DELETE"},
    "email_analyses": {"SELECT", "DELETE"},
    "email_threads": {"SELECT", "DELETE"},
    "encrypted_credentials": {"SELECT", "DELETE"},
}

FINAL_RETENTION_UPDATE_COLUMNS: dict[str, set[str]] = {
    "mail_drafts": {"status", "updated_at"},
    "mail_draft_versions": {"body_ciphertext", "body_nonce", "body_key_version"},
    "calendar_change_proposals": {"status", "updated_at"},
    "calendar_change_snapshots": {
        "content_ciphertext",
        "content_nonce",
        "content_key_version",
    },
    "sync_cursors": {"cursor", "last_success_at", "last_attempt_at", "last_error_code"},
    "email_messages": {
        "body_ciphertext",
        "body_nonce",
        "body_key_version",
        "updated_at",
    },
    "calendar_events": {
        "description_ciphertext",
        "description_nonce",
        "description_key_version",
        "description_aad_version",
        "location_ciphertext",
        "location_nonce",
        "location_key_version",
        "location_aad_version",
        "updated_at",
    },
    "approval_requests": {
        "status",
        "payload_ciphertext",
        "payload_nonce",
        "payload_key_version",
    },
    "tool_executions": {"status", "error_code"},
    "task_runs": {
        "status",
        "error_code",
        "scheduled_for",
        "retry_recovery_at",
        "approval_checkpoint_recovery_at",
        "lease_owner",
        "lease_expires_at",
        "finished_at",
        "updated_at",
    },
    "users": {
        "email",
        "display_name",
        "password_hash",
        "is_active",
        "timezone",
        "locale",
        "brief_time",
        "email_body_retention_days",
        "source_metadata_retention_days",
        "workspace_history_retention_days",
        "default_mail_connection_id",
        "default_calendar_connection_id",
        "default_calendar_id",
        "working_hours",
        "meeting_buffer_minutes",
        "updated_at",
    },
}

HISTORICAL_RETENTION_TABLE_PRIVILEGES: dict[str, frozenset[str]] = {
    "approval_requests": frozenset({"SELECT", "DELETE"}),
    "audit_events": frozenset({"SELECT", "INSERT", "DELETE"}),
    "calendar_events": frozenset({"SELECT", "DELETE"}),
    "conversations": frozenset({"SELECT", "DELETE"}),
    "daily_brief_items": frozenset({"SELECT", "DELETE"}),
    "daily_briefs": frozenset({"SELECT", "DELETE"}),
    "email_analyses": frozenset({"SELECT", "DELETE"}),
    "email_messages": frozenset({"SELECT", "DELETE"}),
    "email_threads": frozenset({"SELECT", "DELETE"}),
    "encrypted_credentials": frozenset({"SELECT", "DELETE"}),
    "llm_invocations": frozenset({"SELECT", "DELETE"}),
    "messages": frozenset({"SELECT", "DELETE"}),
    "oauth_connections": frozenset({"SELECT", "DELETE"}),
    "outbox_events": frozenset({"SELECT", "DELETE"}),
    "sync_cursors": frozenset({"SELECT"}),
    "task_runs": frozenset({"SELECT", "DELETE"}),
    "task_steps": frozenset({"SELECT", "DELETE"}),
    "tool_executions": frozenset({"SELECT", "DELETE"}),
    "user_sessions": frozenset({"SELECT", "DELETE"}),
    "users": frozenset({"SELECT"}),
}

_IDENTITY_RETENTION_UPDATE_POLICY: dict[str, frozenset[str]] = {
    "users": frozenset({"email", "display_name", "password_hash", "is_active"}),
}
_SOURCE_RETENTION_UPDATE_POLICY: dict[str, frozenset[str]] = {
    **_IDENTITY_RETENTION_UPDATE_POLICY,
    "sync_cursors": frozenset({"cursor", "last_success_at", "last_attempt_at", "last_error_code"}),
}
_WORKSPACE_RETENTION_UPDATE_POLICY: dict[str, frozenset[str]] = {
    **_SOURCE_RETENTION_UPDATE_POLICY,
    "users": frozenset(
        {
            "email",
            "display_name",
            "password_hash",
            "is_active",
            "email_body_retention_days",
            "source_metadata_retention_days",
            "workspace_history_retention_days",
        }
    ),
}
_BODY_RETENTION_UPDATE_POLICY: dict[str, frozenset[str]] = {
    **_WORKSPACE_RETENTION_UPDATE_POLICY,
    "email_messages": frozenset({"body_ciphertext", "body_nonce", "body_key_version"}),
}

# 每个 revision 都显式绑定自己的历史列权限阶段；这里不读取生产 registry 或 builder。
RETENTION_UPDATE_COLUMNS_BY_REVISION: dict[str, dict[str, frozenset[str]]] = {
    BASE_REVISION: {},
    "20260730_0001": _IDENTITY_RETENTION_UPDATE_POLICY,
    "20260730_0002": _IDENTITY_RETENTION_UPDATE_POLICY,
    "20260803_0003": _IDENTITY_RETENTION_UPDATE_POLICY,
    "20260803_0004": _IDENTITY_RETENTION_UPDATE_POLICY,
    "20260803_0005": _IDENTITY_RETENTION_UPDATE_POLICY,
    "20260803_0006": _IDENTITY_RETENTION_UPDATE_POLICY,
    "20260804_0007_google_sources": _SOURCE_RETENTION_UPDATE_POLICY,
    "20260804_0007": _SOURCE_RETENTION_UPDATE_POLICY,
    "20260804_0008": _SOURCE_RETENTION_UPDATE_POLICY,
    "20260730_0004": _WORKSPACE_RETENTION_UPDATE_POLICY,
    "20260804_0009": _WORKSPACE_RETENTION_UPDATE_POLICY,
    "20260804_0010": _BODY_RETENTION_UPDATE_POLICY,
    "20260806_0011": _BODY_RETENTION_UPDATE_POLICY,
    "20260806_0012": _BODY_RETENTION_UPDATE_POLICY,
    "20260806_0013": _BODY_RETENTION_UPDATE_POLICY,
    "20260807_0014": _BODY_RETENTION_UPDATE_POLICY,
    "20260808_0015": _BODY_RETENTION_UPDATE_POLICY,
    "20260808_0016": _BODY_RETENTION_UPDATE_POLICY,
    "20260808_0017": _BODY_RETENTION_UPDATE_POLICY,
    HEAD_REVISION: _BODY_RETENTION_UPDATE_POLICY,
    DESTINATION_REVISION: {
        table_name: frozenset(column_names)
        for table_name, column_names in FINAL_RETENTION_UPDATE_COLUMNS.items()
    },
}

# 历史 broad sequence USAGE 到 0018 都覆盖两条已存在 sequence；0019 首次收窄。
RETENTION_SEQUENCES_BY_REVISION: dict[str, frozenset[str]] = {
    BASE_REVISION: frozenset(),
    "20260730_0001": frozenset(),
    "20260730_0002": frozenset({"audit_events_id_seq"}),
    "20260803_0003": frozenset({"audit_events_id_seq"}),
    "20260803_0004": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260803_0005": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260803_0006": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260804_0007_google_sources": frozenset(
        {"audit_events_id_seq", "checkpoint_migrations_v_seq"}
    ),
    "20260804_0007": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260804_0008": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260730_0004": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260804_0009": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260804_0010": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260806_0011": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260806_0012": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260806_0013": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260807_0014": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260808_0015": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260808_0016": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    "20260808_0017": frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    HEAD_REVISION: frozenset({"audit_events_id_seq", "checkpoint_migrations_v_seq"}),
    DESTINATION_REVISION: frozenset({"audit_events_id_seq"}),
}


def _independent_grant(
    kind: ObjectKind,
    object_name: str,
    grantee: str,
    grantor: str,
    privilege_type: str,
    *,
    column_name: str | None = None,
) -> ObjectGrantTuple:
    """只用测试常量构造一条 public/non-grantable expected tuple。"""
    return ObjectGrantTuple(
        object_kind=kind,
        schema_name="public",
        object_name=object_name,
        column_name=column_name,
        grantee=grantee,
        grantor=grantor,
        privilege_type=privilege_type,
        is_grantable=False,
    )


def _independent_expected_inventory(
    revision: str,
    phase: GrantPhase,
) -> tuple[ObjectGrantTuple, ...]:
    """从测试侧独立事实生成一个 revision/phase 的完整八字段 multiset。"""
    assert revision in REVISION_ORDER
    assert phase in {GrantPhase.BASELINE, GrantPhase.ACTIVE}
    grants = [
        _independent_grant(
            ObjectKind.SCHEMA,
            "public",
            PUBLIC_GRANTEE,
            PUBLIC_SCHEMA_OWNER,
            "USAGE",
        ),
        _independent_grant(
            ObjectKind.SCHEMA,
            "public",
            PUBLIC_SCHEMA_OWNER,
            PUBLIC_SCHEMA_OWNER,
            "CREATE",
        ),
        _independent_grant(
            ObjectKind.SCHEMA,
            "public",
            PUBLIC_SCHEMA_OWNER,
            PUBLIC_SCHEMA_OWNER,
            "USAGE",
        ),
        _independent_grant(
            ObjectKind.SCHEMA,
            "public",
            APP_RUNTIME_ROLE_NAME,
            PUBLIC_SCHEMA_OWNER,
            "USAGE",
        ),
        _independent_grant(
            ObjectKind.SCHEMA,
            "public",
            RETENTION_RUNTIME_ROLE_NAME,
            PUBLIC_SCHEMA_OWNER,
            "USAGE",
        ),
    ]
    retention_tables = (
        FINAL_RETENTION_TABLE_PRIVILEGES
        if revision == DESTINATION_REVISION
        else HISTORICAL_RETENTION_TABLE_PRIVILEGES
    )
    retention_columns = RETENTION_UPDATE_COLUMNS_BY_REVISION[revision]
    for table_name in sorted(TABLES_BY_REVISION[revision]):
        for privilege_type in sorted(OWNER_TABLE_PRIVILEGES):
            grants.append(
                _independent_grant(
                    ObjectKind.TABLE,
                    table_name,
                    RELATION_OWNER,
                    RELATION_OWNER,
                    privilege_type,
                )
            )
        if table_name != "alembic_version":
            app_privileges = (
                {"SELECT", "INSERT"} if table_name == "audit_events" else APP_TABLE_PRIVILEGES
            )
            for privilege_type in sorted(app_privileges):
                grants.append(
                    _independent_grant(
                        ObjectKind.TABLE,
                        table_name,
                        APP_RUNTIME_ROLE_NAME,
                        RELATION_OWNER,
                        privilege_type,
                    )
                )
        for privilege_type in sorted(retention_tables.get(table_name, ())):
            grants.append(
                _independent_grant(
                    ObjectKind.TABLE,
                    table_name,
                    RETENTION_RUNTIME_ROLE_NAME,
                    RELATION_OWNER,
                    privilege_type,
                )
            )
        for column_name in sorted(retention_columns.get(table_name, ())):
            grants.append(
                _independent_grant(
                    ObjectKind.COLUMN,
                    table_name,
                    RETENTION_RUNTIME_ROLE_NAME,
                    RELATION_OWNER,
                    "UPDATE",
                    column_name=column_name,
                )
            )

    retention_sequences = RETENTION_SEQUENCES_BY_REVISION[revision]
    for sequence_name in sorted(SEQUENCES_BY_REVISION[revision]):
        for privilege_type in sorted(OWNER_SEQUENCE_PRIVILEGES):
            grants.append(
                _independent_grant(
                    ObjectKind.SEQUENCE,
                    sequence_name,
                    RELATION_OWNER,
                    RELATION_OWNER,
                    privilege_type,
                )
            )
        grants.append(
            _independent_grant(
                ObjectKind.SEQUENCE,
                sequence_name,
                APP_RUNTIME_ROLE_NAME,
                RELATION_OWNER,
                "USAGE",
            )
        )
        if sequence_name in retention_sequences:
            grants.append(
                _independent_grant(
                    ObjectKind.SEQUENCE,
                    sequence_name,
                    RETENTION_RUNTIME_ROLE_NAME,
                    RELATION_OWNER,
                    "USAGE",
                )
            )
    result = tuple(sorted(grants))
    assert len(result) == len(set(result))
    return result


INDEPENDENT_EXPECTED_INVENTORY_BY_KEY = {
    (revision, phase): _independent_expected_inventory(revision, phase)
    for revision in REVISION_ORDER
    for phase in (GrantPhase.BASELINE, GrantPhase.ACTIVE)
}


def _assert_inventory_matches_independent_contract(
    *,
    revision: str,
    phase: GrantPhase,
    actual: tuple[ObjectGrantTuple, ...],
) -> None:
    """以带 revision/phase 的 missing/extra multiset 诊断比较生产输出。"""
    expected = INDEPENDENT_EXPECTED_INVENTORY_BY_KEY[(revision, phase)]
    actual_counts = Counter(actual)
    expected_counts = Counter(expected)
    missing = tuple(sorted((expected_counts - actual_counts).elements()))
    extra = tuple(sorted((actual_counts - expected_counts).elements()))
    if actual != expected:
        raise AssertionError(
            f"{revision}/{phase.value} inventory mismatch: "
            f"missing={missing!r}, extra={extra!r}, "
            f"order_mismatch={not missing and not extra}"
        )


def _object_names(
    grants: Iterable[ObjectGrantTuple],
    *,
    kind: ObjectKind,
) -> set[str]:
    """从完整 ACL tuple 中提取一种对象的精确名称集合。"""
    return {grant.object_name for grant in grants if grant.object_kind is kind}


def _table_privileges(
    grants: Iterable[ObjectGrantTuple],
    *,
    grantee: str,
) -> dict[str, set[str]]:
    """把指定角色的表级 tuple 规范化为测试侧独立矩阵。"""
    result: dict[str, set[str]] = {}
    for grant in grants:
        if grant.object_kind is ObjectKind.TABLE and grant.grantee == grantee:
            result.setdefault(grant.object_name, set()).add(grant.privilege_type)
    return result


def _column_privileges(
    grants: Iterable[ObjectGrantTuple],
    *,
    grantee: str,
    privilege_type: str,
) -> dict[str, set[str]]:
    """提取指定角色与 privilege 的逐列授权，不把表权限展开为列 tuple。"""
    result: dict[str, set[str]] = {}
    for grant in grants:
        if (
            grant.object_kind is ObjectKind.COLUMN
            and grant.grantee == grantee
            and grant.privilege_type == privilege_type
        ):
            assert grant.column_name is not None
            result.setdefault(grant.object_name, set()).add(grant.column_name)
    return result


def _inventory(
    revision: str,
    phase: GrantPhase = GrantPhase.BASELINE,
) -> tuple[ObjectGrantTuple, ...]:
    """以固定合成 owner 读取生产 registry 的完整 expected inventory。"""
    return _expected_object_grants(
        revision=revision,
        phase=phase,
        relation_owner=RELATION_OWNER,
    )


def _inventory_with_relation_owner(
    revision: str,
    relation_owner: str,
    phase: GrantPhase = GrantPhase.BASELINE,
) -> tuple[ObjectGrantTuple, ...]:
    """把测试侧已冻结 inventory 的 relation owner 原文替换为指定 catalog 文本。"""
    return tuple(
        sorted(
            replace(
                grant,
                grantee=(relation_owner if grant.grantee == RELATION_OWNER else grant.grantee),
                grantor=(relation_owner if grant.grantor == RELATION_OWNER else grant.grantor),
            )
            for grant in _inventory(revision, phase)
        )
    )


def _snapshot(
    revision: str,
    phase: GrantPhase = GrantPhase.BASELINE,
) -> CatalogObjectSnapshot:
    """构造已由 pre-migration verifier 证明的 source snapshot。"""
    return CatalogObjectSnapshot(revision=revision, phase=phase, grants=_inventory(revision, phase))


def _pre_runtime_inventory(
    revision: str = HEAD_REVISION,
    phase: GrantPhase = GrantPhase.BASELINE,
) -> tuple[ObjectGrantTuple, ...]:
    """移除全部 app/retention tuple，构造角色尚未创建时的精确 catalog shape。"""
    return tuple(
        grant
        for grant in _inventory(revision, phase)
        if grant.grantee not in {APP_RUNTIME_ROLE_NAME, RETENTION_RUNTIME_ROLE_NAME}
    )


def _render_expected_runtime_grant(grant: ObjectGrantTuple) -> str:
    """由测试侧独立渲染冻结 runtime tuple，避免只断言生产 planner 的输出子集。"""
    quoted_schema = f'"{grant.schema_name}"'
    quoted_object = f'"{grant.object_name}"'
    quoted_grantee = f'"{grant.grantee}"'
    if grant.object_kind is ObjectKind.SCHEMA:
        target = f"SCHEMA {quoted_schema}"
        privilege = grant.privilege_type
    elif grant.object_kind is ObjectKind.TABLE:
        target = f"TABLE {quoted_schema}.{quoted_object}"
        privilege = grant.privilege_type
    elif grant.object_kind is ObjectKind.SEQUENCE:
        target = f"SEQUENCE {quoted_schema}.{quoted_object}"
        privilege = grant.privilege_type
    else:
        assert grant.column_name is not None
        target = f"TABLE {quoted_schema}.{quoted_object}"
        privilege = f'{grant.privilege_type} ("{grant.column_name}")'
    return f"GRANT {privilege} ON {target} TO {quoted_grantee}"


def _pre_delta_grants(delta: MigrationGrantDelta) -> tuple[ObjectGrantTuple, ...]:
    """独立计算 DDL 已完成、显式 grant delta 尚未应用的 catalog tuple。"""
    destination = set(_inventory(delta.destination_revision, delta.phase))
    return tuple(sorted((destination - set(delta.grants_to_apply)) | set(delta.grants_to_revoke)))


def _assert_invariant(call: Callable[[], object]) -> None:
    """断言对象 ACL 漂移只产生稳定且不含 catalog 内容的 typed error。"""
    with pytest.raises(
        ObjectGrantInvariantError,
        match=r"^object grant invariant violation$",
    ):
        call()


class _FakeMappingResult:
    """模拟 SQLAlchemy buffered mapping result。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        """返回当前 catalog source 的全部合成行。"""
        return list(self._rows)


class _FakeResult:
    """提供 ``Result.mappings()`` 的最小同步替身。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeMappingResult:
        """切换到按稳定列名读取的 mapping 视图。"""
        return _FakeMappingResult(self._rows)


class _FakeConnection:
    """用合成 raw catalog 行记录只读查询与精确 grant/revoke SQL。"""

    def __init__(
        self,
        grants: tuple[ObjectGrantTuple, ...],
        *,
        relation_owner: str = RELATION_OWNER,
    ) -> None:
        self.grants = grants
        self.relation_owner = relation_owner
        self.calls: list[tuple[str, dict[str, object]]] = []

    @property
    def mutation_sql(self) -> list[str]:
        """返回排除四个 canonical SELECT 后的全部 SQL。"""
        readers = {SCHEMA_GRANTS_SQL, TABLE_GRANTS_SQL, COLUMN_GRANTS_SQL, SEQUENCE_GRANTS_SQL}
        return [sql for sql, _ in self.calls if sql not in readers]

    def _rows(self, kind: ObjectKind) -> list[dict[str, object]]:
        """把强类型 tuple 转回 reader 接收的 raw catalog 列。"""
        rows: list[dict[str, object]] = []
        for grant in self.grants:
            if grant.object_kind is not kind:
                continue
            rows.append(
                {
                    "object_kind": grant.object_kind.value,
                    "schema_name": grant.schema_name,
                    "object_name": grant.object_name,
                    "column_name": grant.column_name,
                    "grantee": grant.grantee,
                    "grantor": grant.grantor,
                    "privilege_type": grant.privilege_type,
                    "is_grantable": grant.is_grantable,
                    "object_owner": (
                        PUBLIC_SCHEMA_OWNER
                        if grant.object_kind is ObjectKind.SCHEMA
                        else self.relation_owner
                    ),
                    "database_owner": self.relation_owner,
                }
            )
        return rows

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _FakeResult:
        """按逐字 SQL 返回一种 catalog source，其他语句只记录不执行。"""
        sql = str(statement)
        params = {} if parameters is None else dict(parameters)
        self.calls.append((sql, params))
        if sql == SCHEMA_GRANTS_SQL:
            return _FakeResult(self._rows(ObjectKind.SCHEMA))
        if sql == TABLE_GRANTS_SQL:
            return _FakeResult(self._rows(ObjectKind.TABLE))
        if sql == COLUMN_GRANTS_SQL:
            return _FakeResult(self._rows(ObjectKind.COLUMN))
        if sql == SEQUENCE_GRANTS_SQL:
            return _FakeResult(self._rows(ObjectKind.SEQUENCE))
        return _FakeResult([])


@pytest.mark.parametrize(
    ("revision", "phase"),
    [
        pytest.param(revision, phase, id=f"{revision}-{phase.value}")
        for revision in REVISION_ORDER
        for phase in (GrantPhase.BASELINE, GrantPhase.ACTIVE)
    ],
)
def test_inventory_registry_freezes_every_revision_and_phase_without_fallback(
    revision: str,
    phase: GrantPhase,
) -> None:
    """每个显式 registry key 都必须逐 tuple 匹配测试侧独立历史契约。"""
    actual = _expected_object_grants(
        revision=revision,
        phase=phase,
        relation_owner=RELATION_OWNER,
    )

    _assert_inventory_matches_independent_contract(
        revision=revision,
        phase=phase,
        actual=actual,
    )
    assert actual == tuple(sorted(actual))
    assert len(set(actual)) == len(actual)
    assert all(grant.is_grantable is False for grant in actual)


def test_inventory_registry_rejects_unknown_revision_and_raw_phase() -> None:
    """未知 revision 或绕过 GrantPhase 的原始字符串都不能触发隐式 fallback。"""
    _assert_invariant(
        lambda: _expected_object_grants(
            revision="unknown_revision",
            phase=GrantPhase.BASELINE,
            relation_owner=RELATION_OWNER,
        )
    )
    _assert_invariant(
        lambda: _expected_object_grants(
            revision=HEAD_REVISION,
            phase=cast(GrantPhase, "baseline"),
            relation_owner=RELATION_OWNER,
        )
    )


@pytest.mark.parametrize("relation_owner", SPECIAL_CATALOG_OWNER_NAMES)
def test_expected_inventory_accepts_catalog_owner_text(
    relation_owner: str,
) -> None:
    """catalog owner 是原始角色文本，不应受 SQL identifier 闭集格式限制。"""
    actual = _expected_object_grants(
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
        relation_owner=relation_owner,
    )

    assert actual == _inventory_with_relation_owner(HEAD_REVISION, relation_owner)


@pytest.mark.parametrize(
    "relation_owner",
    (
        pytest.param(None, id="none"),
        pytest.param(1, id="non-string"),
        pytest.param("", id="empty"),
        pytest.param("owner\x00name", id="nul"),
    ),
)
def test_expected_inventory_rejects_invalid_catalog_owner_text(
    relation_owner: object,
) -> None:
    """catalog owner 仍须严格为非空无 NUL 字符串，不能做隐式类型转换。"""
    _assert_invariant(
        lambda: _expected_object_grants(
            revision=HEAD_REVISION,
            phase=GrantPhase.BASELINE,
            relation_owner=cast(str, relation_owner),
        )
    )


@pytest.mark.parametrize("relation_owner", SPECIAL_CATALOG_OWNER_NAMES)
def test_reader_and_verifier_preserve_catalog_owner_text(
    relation_owner: str,
) -> None:
    """Fake catalog reader 与完整 verifier 必须逐字保留特殊字符 owner。"""
    grants = _inventory_with_relation_owner(HEAD_REVISION, relation_owner)
    connection = _FakeConnection(grants, relation_owner=relation_owner)

    snapshot = read_object_grants(
        cast(Connection, connection),
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
    )
    verified = verify_object_grants(
        cast(Connection, connection),
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
    )

    expected = CatalogObjectSnapshot(
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
        grants=grants,
    )
    assert snapshot == expected
    assert verified == expected


def test_catalog_owner_text_never_enters_rendered_mutation_sql() -> None:
    """grantor 只参与 tuple 比较，含特殊字符的 owner 绝不能进入 GRANT/REVOKE SQL。"""
    relation_owner = 'owner"name'
    source_grants = _inventory_with_relation_owner(HEAD_REVISION, relation_owner)
    destination_grants = _inventory_with_relation_owner(
        DESTINATION_REVISION,
        relation_owner,
    )
    delta = _migration_grant_delta(
        source_revision=HEAD_REVISION,
        destination_revision=DESTINATION_REVISION,
        phase=GrantPhase.BASELINE,
        relation_owner=relation_owner,
    )
    pre_delta_grants = tuple(
        sorted((set(destination_grants) - set(delta.grants_to_apply)) | set(delta.grants_to_revoke))
    )
    connection = _FakeConnection(pre_delta_grants, relation_owner=relation_owner)
    before = CatalogObjectSnapshot(
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
        grants=source_grants,
    )

    apply_migration_grant_delta(
        cast(Connection, connection),
        source_revision=HEAD_REVISION,
        destination_revision=DESTINATION_REVISION,
        phase=GrantPhase.BASELINE,
        before=before,
    )

    assert connection.mutation_sql
    assert relation_owner not in "\n".join(connection.mutation_sql)


@pytest.mark.parametrize(
    (
        "revision",
        "phase",
        "defect",
        "missing_empty",
        "extra_empty",
        "diagnostic_fragments",
    ),
    [
        pytest.param(
            BASE_REVISION,
            GrantPhase.BASELINE,
            "missing",
            False,
            True,
            ("ObjectGrantTuple",),
            id="missing",
        ),
        pytest.param(
            "20260730_0001",
            GrantPhase.ACTIVE,
            "extra",
            True,
            False,
            ("unexpected_table",),
            id="extra",
        ),
        pytest.param(
            "20260730_0002",
            GrantPhase.BASELINE,
            "wrong_grantor",
            False,
            False,
            (f"grantor='{RELATION_OWNER}'", f"grantor='{OTHER_GRANTOR}'"),
            id="wrong-grantor",
        ),
        pytest.param(
            DESTINATION_REVISION,
            GrantPhase.ACTIVE,
            "wrong_column",
            False,
            False,
            ("column_name='status'", "column_name='unexpected_column'"),
            id="wrong-column",
        ),
    ],
)
def test_independent_inventory_diagnostic_locates_exact_tuple_drift(
    revision: str,
    phase: GrantPhase,
    defect: str,
    missing_empty: bool,
    extra_empty: bool,
    diagnostic_fragments: tuple[str, ...],
) -> None:
    """独立比较器必须用 revision/phase 与完整 tuple 定位四类关键偏差。"""
    expected = INDEPENDENT_EXPECTED_INVENTORY_BY_KEY[(revision, phase)]
    if defect == "missing":
        actual = expected[1:]
    elif defect == "extra":
        actual = tuple(
            sorted(
                (
                    *expected,
                    _independent_grant(
                        ObjectKind.TABLE,
                        "unexpected_table",
                        RETENTION_RUNTIME_ROLE_NAME,
                        RELATION_OWNER,
                        "SELECT",
                    ),
                )
            )
        )
    elif defect == "wrong_grantor":
        target = next(
            grant
            for grant in expected
            if grant.object_kind is ObjectKind.TABLE
            and grant.object_name == "users"
            and grant.grantee == RELATION_OWNER
            and grant.privilege_type == "SELECT"
        )
        actual = tuple(
            sorted(
                replace(grant, grantor=OTHER_GRANTOR) if grant == target else grant
                for grant in expected
            )
        )
    else:
        assert defect == "wrong_column"
        target = next(
            grant
            for grant in expected
            if grant.object_kind is ObjectKind.COLUMN
            and grant.object_name == "mail_drafts"
            and grant.column_name == "status"
        )
        actual = tuple(
            sorted(
                replace(grant, column_name="unexpected_column") if grant == target else grant
                for grant in expected
            )
        )

    with pytest.raises(AssertionError) as exc_info:
        _assert_inventory_matches_independent_contract(
            revision=revision,
            phase=phase,
            actual=actual,
        )

    message = str(exc_info.value)
    assert f"{revision}/{phase.value}" in message
    assert "missing=" in message
    assert "extra=" in message
    assert ("missing=()" in message) is missing_empty
    assert ("extra=()" in message) is extra_empty
    assert all(fragment in message for fragment in diagnostic_fragments)


@pytest.mark.parametrize("revision", REVISION_ORDER)
def test_revision_inventory_freezes_every_managed_table_and_sequence(revision: str) -> None:
    """所有 destination 从首步起包含 alembic_version，并精确追踪两条 identity sequence。"""
    grants = _inventory(revision)
    assert _object_names(grants, kind=ObjectKind.TABLE) == TABLES_BY_REVISION[revision]
    assert _object_names(grants, kind=ObjectKind.SEQUENCE) == SEQUENCES_BY_REVISION[revision]

    if revision == BASE_REVISION:
        assert "alembic_version" not in _object_names(grants, kind=ObjectKind.TABLE)
    else:
        assert "alembic_version" in _object_names(grants, kind=ObjectKind.TABLE)


def test_owner_and_application_grants_are_exact_per_object_kind() -> None:
    """PG17 owner 完整权限合法，app 只保留业务所需权限且不能改删审计。"""
    grants = _inventory(DESTINATION_REVISION)
    owner_tables = _table_privileges(grants, grantee=RELATION_OWNER)
    assert owner_tables == {table: OWNER_TABLE_PRIVILEGES for table in ALL_TABLES}

    owner_sequences = {
        grant.object_name: grant.privilege_type
        for grant in grants
        if grant.object_kind is ObjectKind.SEQUENCE and grant.grantee == RELATION_OWNER
    }
    assert {
        privilege
        for grant in grants
        if grant.object_kind is ObjectKind.SEQUENCE and grant.grantee == RELATION_OWNER
        for privilege in (grant.privilege_type,)
    } == OWNER_SEQUENCE_PRIVILEGES
    assert set(owner_sequences) == {"audit_events_id_seq", "checkpoint_migrations_v_seq"}

    app_tables = _table_privileges(grants, grantee=APP_RUNTIME_ROLE_NAME)
    assert "alembic_version" not in app_tables
    assert app_tables["audit_events"] == {"SELECT", "INSERT"}
    assert all(
        privileges == APP_TABLE_PRIVILEGES
        for table, privileges in app_tables.items()
        if table != "audit_events"
    )
    assert set(app_tables) == ALL_TABLES - {"alembic_version"}


def test_destination_0019_retention_matrix_is_complete_and_exact() -> None:
    """0019 不得从遗漏项推断权限，表/列/schema/sequence 全部逐 tuple 冻结。"""
    grants = _inventory(DESTINATION_REVISION)

    assert (
        _table_privileges(
            grants,
            grantee=RETENTION_RUNTIME_ROLE_NAME,
        )
        == FINAL_RETENTION_TABLE_PRIVILEGES
    )
    assert (
        _column_privileges(
            grants,
            grantee=RETENTION_RUNTIME_ROLE_NAME,
            privilege_type="UPDATE",
        )
        == FINAL_RETENTION_UPDATE_COLUMNS
    )

    retention_schema = {
        grant.privilege_type
        for grant in grants
        if grant.object_kind is ObjectKind.SCHEMA and grant.grantee == RETENTION_RUNTIME_ROLE_NAME
    }
    retention_sequences = {
        (grant.object_name, grant.privilege_type)
        for grant in grants
        if grant.object_kind is ObjectKind.SEQUENCE and grant.grantee == RETENTION_RUNTIME_ROLE_NAME
    }
    assert retention_schema == {"USAGE"}
    assert retention_sequences == {("audit_events_id_seq", "USAGE")}


def test_destination_0019_rejects_every_forbidden_retention_privilege() -> None:
    """runtime policy 不得继承 owner 的 MAINTAIN 等合法 owner tuple。"""
    grants = _inventory(DESTINATION_REVISION)
    retention_grants = tuple(
        grant for grant in grants if grant.grantee == RETENTION_RUNTIME_ROLE_NAME
    )
    assert all(grant.is_grantable is False for grant in retention_grants)
    assert not any(
        grant.object_kind is ObjectKind.SCHEMA and grant.privilege_type == "CREATE"
        for grant in retention_grants
    )
    assert not any(
        grant.object_kind is ObjectKind.SEQUENCE and grant.privilege_type in {"SELECT", "UPDATE"}
        for grant in retention_grants
    )
    assert not any(
        grant.object_kind is ObjectKind.TABLE
        and grant.privilege_type in {"TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN"}
        for grant in retention_grants
    )
    assert not any(
        grant.object_kind is ObjectKind.TABLE
        and grant.object_name == "users"
        and grant.privilege_type == "DELETE"
        for grant in retention_grants
    )


def test_0018_retention_inventory_remains_historical_until_0019() -> None:
    """0018 仍保留旧 sequence/body/user policy，不能在 migration 前预修复成最终矩阵。"""
    before = _inventory(HEAD_REVISION)
    after = _inventory(DESTINATION_REVISION)

    before_tables = _table_privileges(before, grantee=RETENTION_RUNTIME_ROLE_NAME)
    before_columns = _column_privileges(
        before,
        grantee=RETENTION_RUNTIME_ROLE_NAME,
        privilege_type="UPDATE",
    )
    assert "connection_capabilities" not in before_tables
    assert "provider_calendars" not in before_tables
    assert "mail_drafts" not in before_tables
    assert "oauth_attempts" not in before_tables
    assert before_tables["sync_cursors"] == {"SELECT"}
    assert before_columns["email_messages"] == {
        "body_ciphertext",
        "body_nonce",
        "body_key_version",
    }
    assert "updated_at" not in before_columns["email_messages"]
    assert before_columns["users"] == {
        "email",
        "display_name",
        "password_hash",
        "is_active",
        "email_body_retention_days",
        "source_metadata_retention_days",
        "workspace_history_retention_days",
    }
    assert {
        grant.object_name
        for grant in before
        if grant.object_kind is ObjectKind.SEQUENCE
        and grant.grantee == RETENTION_RUNTIME_ROLE_NAME
        and grant.privilege_type == "USAGE"
    } == {"audit_events_id_seq", "checkpoint_migrations_v_seq"}
    assert before != after


def test_0019_leaves_application_role_policy_unchanged() -> None:
    """0018→0019 只收紧 retention；没有新对象时 app tuple 必须逐字相同。"""
    before = tuple(
        grant for grant in _inventory(HEAD_REVISION) if grant.grantee == APP_RUNTIME_ROLE_NAME
    )
    after = tuple(
        grant
        for grant in _inventory(DESTINATION_REVISION)
        if grant.grantee == APP_RUNTIME_ROLE_NAME
    )
    assert before == after


@pytest.mark.parametrize(
    ("mutator", "case_id"),
    (
        pytest.param(lambda grants: grants[:-1], "missing", id="missing"),
        pytest.param(
            lambda grants: (*grants, grants[-1]),
            "duplicate",
            id="duplicate",
        ),
        pytest.param(
            lambda grants: (
                *grants,
                ObjectGrantTuple(
                    ObjectKind.TABLE,
                    "public",
                    "unexpected_table",
                    None,
                    RELATION_OWNER,
                    RELATION_OWNER,
                    "SELECT",
                    False,
                ),
            ),
            "extra",
            id="extra",
        ),
        pytest.param(
            lambda grants: (
                replace(grants[0], grantor=OTHER_GRANTOR),
                *grants[1:],
            ),
            "wrong-grantor",
            id="wrong-grantor",
        ),
        pytest.param(
            lambda grants: (
                replace(grants[0], is_grantable=True),
                *grants[1:],
            ),
            "grant-option",
            id="grant-option",
        ),
    ),
)
def test_pre_migration_verifier_rejects_full_multiset_drift_before_mutation(
    mutator: Callable[[tuple[ObjectGrantTuple, ...]], tuple[ObjectGrantTuple, ...]],
    case_id: str,
) -> None:
    """missing/extra/duplicate/grantor/grant option 漂移必须在首个 DDL 前 fail closed。"""
    del case_id
    connection = _FakeConnection(tuple(mutator(_inventory(HEAD_REVISION))))

    _assert_invariant(
        lambda: verify_pre_migration_object_grants(
            cast(Connection, connection),
            revision=HEAD_REVISION,
            phase=GrantPhase.BASELINE,
        )
    )
    assert connection.mutation_sql == []


def test_reader_executes_four_raw_catalog_sources_and_returns_sorted_snapshot() -> None:
    """reader 必须逐一读取 schema/table/column/sequence，且不依赖 effective API。"""
    grants = _inventory(HEAD_REVISION)
    connection = _FakeConnection(grants)

    snapshot = read_object_grants(
        cast(Connection, connection),
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
    )

    assert snapshot == CatalogObjectSnapshot(
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
        grants=grants,
    )
    assert [sql for sql, _ in connection.calls] == [
        SCHEMA_GRANTS_SQL,
        TABLE_GRANTS_SQL,
        COLUMN_GRANTS_SQL,
        SEQUENCE_GRANTS_SQL,
    ]
    rendered_sql = "\n".join(sql.lower() for sql, _ in connection.calls)
    for forbidden in (
        "information_schema",
        "has_schema_privilege",
        "has_table_privilege",
        "has_sequence_privilege",
        "pg_auth_members",
        "security definer",
    ):
        assert forbidden not in rendered_sql


@pytest.mark.parametrize(
    "bad_grant",
    (
        ObjectGrantTuple(
            ObjectKind.TABLE,
            "public",
            "users",
            None,
            APP_RUNTIME_ROLE_NAME,
            RELATION_OWNER,
            "UNKNOWN",
            False,
        ),
        ObjectGrantTuple(
            ObjectKind.TABLE,
            "public",
            "users",
            None,
            APP_RUNTIME_ROLE_NAME,
            RELATION_OWNER,
            "SELECT",
            True,
        ),
    ),
)
def test_reader_rejects_unknown_privilege_and_any_grant_option(
    bad_grant: ObjectGrantTuple,
) -> None:
    """raw catalog 未知 privilege 或 grant option 不能留给 inventory 比较宽松处理。"""
    connection = _FakeConnection((bad_grant,))
    _assert_invariant(
        lambda: read_object_grants(
            cast(Connection, connection),
            revision=HEAD_REVISION,
            phase=GrantPhase.BASELINE,
        )
    )


def test_first_install_delta_owns_exact_alembic_table_shape_without_granting_it() -> None:
    """base 可无 version table；首步 DDL 后只接纳 Alembic 自动表的 owner ACL。"""
    delta = _migration_grant_delta(
        source_revision=BASE_REVISION,
        destination_revision="20260730_0001",
        phase=GrantPhase.BASELINE,
        relation_owner=RELATION_OWNER,
    )
    assert "alembic_version" not in _object_names(_inventory(BASE_REVISION), kind=ObjectKind.TABLE)
    alembic_grants = tuple(
        grant for grant in _inventory("20260730_0001") if grant.object_name == "alembic_version"
    )
    assert {grant.grantee for grant in alembic_grants} == {RELATION_OWNER}
    assert {grant.privilege_type for grant in alembic_grants} == OWNER_TABLE_PRIVILEGES
    assert not any(grant.object_name == "alembic_version" for grant in delta.grants_to_apply)

    connection = _FakeConnection(_pre_delta_grants(delta))
    apply_migration_grant_delta(
        cast(Connection, connection),
        source_revision=BASE_REVISION,
        destination_revision="20260730_0001",
        phase=GrantPhase.BASELINE,
        before=_snapshot(BASE_REVISION),
    )
    assert connection.mutation_sql
    assert all("alembic_version" not in sql for sql in connection.mutation_sql)
    assert all(
        any(table in sql for table in ("users", "user_sessions")) for sql in connection.mutation_sql
    )


def test_new_table_and_sequence_delta_touches_only_objects_created_by_that_step() -> None:
    """checkpoint step 只能授权四个新表与该步新 sequence，旧对象漂移不在修复范围。"""
    source = "20260803_0003"
    destination = "20260803_0004"
    delta = _migration_grant_delta(
        source_revision=source,
        destination_revision=destination,
        phase=GrantPhase.BASELINE,
        relation_owner=RELATION_OWNER,
    )
    created_objects = CHECKPOINT_TABLES | {"checkpoint_migrations_v_seq"}
    assert delta.grants_to_revoke == ()
    assert {grant.object_name for grant in delta.grants_to_apply} <= created_objects

    connection = _FakeConnection(_pre_delta_grants(delta))
    apply_migration_grant_delta(
        cast(Connection, connection),
        source_revision=source,
        destination_revision=destination,
        phase=GrantPhase.BASELINE,
        before=_snapshot(source),
    )
    assert connection.mutation_sql
    assert all(any(name in sql for name in created_objects) for sql in connection.mutation_sql)
    assert all("users" not in sql and "task_runs" not in sql for sql in connection.mutation_sql)


def test_new_column_policy_delta_names_only_columns_added_by_the_step() -> None:
    """每日简报 step 对既有 users 只能新增三个 retention-day 列授权。"""
    source = "20260804_0008"
    destination = "20260730_0004"
    delta = _migration_grant_delta(
        source_revision=source,
        destination_revision=destination,
        phase=GrantPhase.BASELINE,
        relation_owner=RELATION_OWNER,
    )
    user_column_applies = {
        grant.column_name
        for grant in delta.grants_to_apply
        if grant.object_kind is ObjectKind.COLUMN and grant.object_name == "users"
    }
    assert user_column_applies == {
        "email_body_retention_days",
        "source_metadata_retention_days",
        "workspace_history_retention_days",
    }


def test_explicit_pre_0019_policy_change_is_not_generic_repair() -> None:
    """0010 只显式增加 email body 三元组 UPDATE，不触碰其他旧对象。"""
    delta = _migration_grant_delta(
        source_revision="20260804_0009",
        destination_revision="20260804_0010",
        phase=GrantPhase.BASELINE,
        relation_owner=RELATION_OWNER,
    )
    assert delta.grants_to_revoke == ()
    assert {
        (grant.object_name, grant.column_name, grant.privilege_type, grant.grantee)
        for grant in delta.grants_to_apply
    } == {
        ("email_messages", "body_ciphertext", "UPDATE", RETENTION_RUNTIME_ROLE_NAME),
        ("email_messages", "body_nonce", "UPDATE", RETENTION_RUNTIME_ROLE_NAME),
        ("email_messages", "body_key_version", "UPDATE", RETENTION_RUNTIME_ROLE_NAME),
    }


def test_0018_to_0019_delta_is_explicit_and_narrows_sequence_usage() -> None:
    """0019 精确应用最终 retention matrix，并只撤销非审计 sequence 的历史 USAGE。"""
    delta = _migration_grant_delta(
        source_revision=HEAD_REVISION,
        destination_revision=DESTINATION_REVISION,
        phase=GrantPhase.BASELINE,
        relation_owner=RELATION_OWNER,
    )
    assert all(
        grant.grantee == RETENTION_RUNTIME_ROLE_NAME
        for grant in (*delta.grants_to_revoke, *delta.grants_to_apply)
    )
    assert {
        (grant.object_name, grant.privilege_type)
        for grant in delta.grants_to_revoke
        if grant.object_kind is ObjectKind.SEQUENCE
    } == {("checkpoint_migrations_v_seq", "USAGE")}
    assert not any(
        grant.object_name == "audit_events_id_seq"
        for grant in delta.grants_to_revoke
        if grant.object_kind is ObjectKind.SEQUENCE
    )

    connection = _FakeConnection(_pre_delta_grants(delta))
    apply_migration_grant_delta(
        cast(Connection, connection),
        source_revision=HEAD_REVISION,
        destination_revision=DESTINATION_REVISION,
        phase=GrantPhase.BASELINE,
        before=_snapshot(HEAD_REVISION),
    )
    rendered = "\n".join(connection.mutation_sql).upper()
    assert "ALL TABLES" not in rendered
    assert "ALL SEQUENCES" not in rendered
    assert "SECURITY DEFINER" not in rendered
    assert "GRANT OPTION" not in rendered
    assert "REVOKE USAGE ON SEQUENCE" in rendered
    assert "CHECKPOINT_MIGRATIONS_V_SEQ" in rendered


def test_historical_adjacent_downgrade_uses_reverse_of_frozen_policy_delta() -> None:
    """历史 downgrade 只能反向执行相邻正向 policy，不得依赖 env waiver 或事后 repair。"""
    delta = _migration_grant_delta(
        source_revision="20260804_0010",
        destination_revision="20260804_0009",
        phase=GrantPhase.BASELINE,
        relation_owner=RELATION_OWNER,
    )

    assert delta.grants_to_apply == ()
    assert {
        (grant.object_name, grant.column_name, grant.privilege_type)
        for grant in delta.grants_to_revoke
    } == {
        ("email_messages", "body_ciphertext", "UPDATE"),
        ("email_messages", "body_key_version", "UPDATE"),
        ("email_messages", "body_nonce", "UPDATE"),
    }


@pytest.mark.parametrize(
    ("mutator", "case_id"),
    (
        pytest.param(lambda grants: list(grants), "container", id="container"),
        pytest.param(lambda grants: (object(),), "member", id="member"),
        pytest.param(
            lambda grants: (*grants, grants[-1]),
            "duplicate",
            id="duplicate",
        ),
    ),
)
def test_apply_rejects_malformed_before_snapshot_before_sql(
    mutator: Callable[[tuple[ObjectGrantTuple, ...]], object],
    case_id: str,
) -> None:
    """非法 grants 容器、成员或 duplicate 必须在 owner 推导和首条 SQL 前稳定失败。"""
    del case_id
    malformed_grants = cast(
        tuple[ObjectGrantTuple, ...],
        mutator(_inventory(HEAD_REVISION)),
    )
    before = CatalogObjectSnapshot(
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
        grants=malformed_grants,
    )
    connection = _FakeConnection(())

    _assert_invariant(
        lambda: apply_migration_grant_delta(
            cast(Connection, connection),
            source_revision=HEAD_REVISION,
            destination_revision=DESTINATION_REVISION,
            phase=GrantPhase.BASELINE,
            before=before,
        )
    )
    assert connection.calls == []
    assert connection.mutation_sql == []


def test_apply_rejects_source_snapshot_drift_before_any_mutation() -> None:
    """传入 snapshot 缺 tuple 时不能先执行 delta 再依赖 destination verifier 发现。"""
    source = "20260803_0003"
    destination = "20260803_0004"
    delta = _migration_grant_delta(
        source_revision=source,
        destination_revision=destination,
        phase=GrantPhase.BASELINE,
        relation_owner=RELATION_OWNER,
    )
    connection = _FakeConnection(_pre_delta_grants(delta))
    bad_before = replace(_snapshot(source), grants=_inventory(source)[:-1])

    _assert_invariant(
        lambda: apply_migration_grant_delta(
            cast(Connection, connection),
            source_revision=source,
            destination_revision=destination,
            phase=GrantPhase.BASELINE,
            before=bad_before,
        )
    )
    assert connection.mutation_sql == []


def test_apply_rejects_unexpected_post_ddl_object_before_any_mutation() -> None:
    """DDL 后出现非本 step 对象时，delta 不能先授予部分权限再失败。"""
    source = "20260803_0003"
    destination = "20260803_0004"
    delta = _migration_grant_delta(
        source_revision=source,
        destination_revision=destination,
        phase=GrantPhase.BASELINE,
        relation_owner=RELATION_OWNER,
    )
    unexpected_owner_grant = ObjectGrantTuple(
        ObjectKind.TABLE,
        "public",
        "unexpected_table",
        None,
        RELATION_OWNER,
        RELATION_OWNER,
        "SELECT",
        False,
    )
    connection = _FakeConnection((*_pre_delta_grants(delta), unexpected_owner_grant))

    _assert_invariant(
        lambda: apply_migration_grant_delta(
            cast(Connection, connection),
            source_revision=source,
            destination_revision=destination,
            phase=GrantPhase.BASELINE,
            before=_snapshot(source),
        )
    )
    assert connection.mutation_sql == []


def test_apply_supports_object_removal_without_emitting_repair_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 planner 必须把 DDL 已删除对象排除在 runtime REVOKE 之外。"""
    source_revision = "synthetic_source"
    destination_revision = "synthetic_destination"
    schema_grants = tuple(
        grant for grant in _inventory(BASE_REVISION) if grant.object_kind is ObjectKind.SCHEMA
    )
    old_table_grants = (
        ObjectGrantTuple(
            ObjectKind.TABLE,
            "public",
            "removed_table",
            None,
            RELATION_OWNER,
            RELATION_OWNER,
            "SELECT",
            False,
        ),
        ObjectGrantTuple(
            ObjectKind.TABLE,
            "public",
            "removed_table",
            None,
            APP_RUNTIME_ROLE_NAME,
            RELATION_OWNER,
            "SELECT",
            False,
        ),
    )
    source_grants = tuple(sorted((*schema_grants, *old_table_grants)))
    destination_grants = tuple(sorted(schema_grants))

    def fake_expected(
        *, revision: str, phase: GrantPhase, relation_owner: str
    ) -> tuple[ObjectGrantTuple, ...]:
        """为一个对象删除 step 提供闭合的合成 registry。"""
        assert phase is GrantPhase.BASELINE
        assert relation_owner == RELATION_OWNER
        if revision == source_revision:
            return source_grants
        if revision == destination_revision:
            return destination_grants
        raise AssertionError("unexpected synthetic revision")

    monkeypatch.setattr(
        database_grants_module,
        "_REVISION_ORDER",
        (source_revision, destination_revision),
    )
    monkeypatch.setattr(database_grants_module, "_expected_object_grants", fake_expected)
    delta = _migration_grant_delta(
        source_revision=source_revision,
        destination_revision=destination_revision,
        phase=GrantPhase.BASELINE,
        relation_owner=RELATION_OWNER,
    )
    assert delta == MigrationGrantDelta(
        source_revision=source_revision,
        destination_revision=destination_revision,
        phase=GrantPhase.BASELINE,
        grants_to_revoke=(),
        grants_to_apply=(),
    )
    connection = _FakeConnection(destination_grants)
    before = CatalogObjectSnapshot(
        revision=source_revision,
        phase=GrantPhase.BASELINE,
        grants=source_grants,
    )

    apply_migration_grant_delta(
        cast(Connection, connection),
        source_revision=source_revision,
        destination_revision=destination_revision,
        phase=GrantPhase.BASELINE,
        before=before,
    )
    assert connection.mutation_sql == []


@pytest.mark.parametrize(
    ("source", "destination", "before_revision"),
    (
        pytest.param(HEAD_REVISION, "20260803_0004", HEAD_REVISION, id="non-adjacent"),
        pytest.param(HEAD_REVISION, DESTINATION_REVISION, "20260808_0017", id="before-mismatch"),
        pytest.param("unknown", DESTINATION_REVISION, "unknown", id="unknown-source"),
    ),
)
def test_apply_rejects_source_destination_revision_mismatch_without_sql(
    source: str,
    destination: str,
    before_revision: str,
) -> None:
    """只允许 registry 中一个精确相邻 source→destination pair。"""
    connection = _FakeConnection(_inventory(HEAD_REVISION))
    before = CatalogObjectSnapshot(
        revision=before_revision,
        phase=GrantPhase.BASELINE,
        grants=_inventory(HEAD_REVISION),
    )
    _assert_invariant(
        lambda: apply_migration_grant_delta(
            cast(Connection, connection),
            source_revision=source,
            destination_revision=destination,
            phase=GrantPhase.BASELINE,
            before=before,
        )
    )
    assert connection.calls == []


def test_apply_contract_cannot_receive_roles_database_acl_or_restore_facts() -> None:
    """对象 helper 的参数面必须与 database ACL/角色/restore authority 保持独立。"""
    parameters = set(inspect.signature(apply_migration_grant_delta).parameters)
    assert parameters == {
        "connection",
        "source_revision",
        "destination_revision",
        "phase",
        "before",
    }


def test_bootstrap_object_grant_verifier_freezes_role_presence_inventory() -> None:
    """角色 absent 接受当前 revision pre-runtime；已存在则要求同 revision 完整 inventory。"""
    from ai_employee.infrastructure.db.database_grants import (
        BootstrapGrantPosture,
        verify_bootstrap_object_grants,
    )

    absent_connection = _FakeConnection(_pre_runtime_inventory(HEAD_REVISION))
    absent_snapshot = verify_bootstrap_object_grants(
        cast(Connection, absent_connection),
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
        posture=BootstrapGrantPosture.PRE_RUNTIME,
    )
    assert absent_snapshot.grants == _pre_runtime_inventory(HEAD_REVISION)
    assert absent_connection.mutation_sql == []

    present_connection = _FakeConnection(_inventory(HEAD_REVISION))
    present_snapshot = verify_bootstrap_object_grants(
        cast(Connection, present_connection),
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
        posture=BootstrapGrantPosture.CURRENT,
    )
    assert present_snapshot.grants == _inventory(HEAD_REVISION)
    assert present_connection.mutation_sql == []

    rejected_connection = _FakeConnection(_pre_runtime_inventory(HEAD_REVISION)[:-1])
    _assert_invariant(
        lambda: verify_bootstrap_object_grants(
            cast(Connection, rejected_connection),
            revision=HEAD_REVISION,
            phase=GrantPhase.BASELINE,
            posture=BootstrapGrantPosture.PRE_RUNTIME,
        )
    )
    assert len(rejected_connection.calls) == 4
    assert rejected_connection.mutation_sql == []


def test_bootstrap_object_grant_mutator_applies_exact_current_revision_runtime_delta() -> None:
    """0018 absent-role bootstrap 只补当前冻结 runtime tuples，不提前使用 0019 policy。"""
    from ai_employee.infrastructure.db.database_grants import (
        BootstrapGrantPosture,
        apply_bootstrap_object_grants,
    )

    connection = _FakeConnection(_pre_runtime_inventory(HEAD_REVISION))
    apply_bootstrap_object_grants(
        cast(Connection, connection),
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
        posture=BootstrapGrantPosture.PRE_RUNTIME,
    )

    expected_runtime_grants = [
        _render_expected_runtime_grant(grant)
        for grant in _inventory(HEAD_REVISION)
        if grant.grantee in {APP_RUNTIME_ROLE_NAME, RETENTION_RUNTIME_ROLE_NAME}
    ]
    assert connection.mutation_sql == expected_runtime_grants
    rendered = "\n".join(connection.mutation_sql).upper()
    for forbidden in (
        "ALL TABLES",
        "ALL SEQUENCES",
        "ALL FUNCTIONS",
        "ALTER DEFAULT PRIVILEGES",
        "DESCRIPTION_AAD_VERSION",
        "LOCATION_AAD_VERSION",
    ):
        assert forbidden not in rendered


def test_bootstrap_object_grant_mutator_reasserts_exact_current_runtime_inventory() -> None:
    """CURRENT 先验证完整 0018 inventory，再逐 tuple 幂等重申既有 runtime grants。"""
    from ai_employee.infrastructure.db.database_grants import (
        BootstrapGrantPosture,
        apply_bootstrap_object_grants,
    )

    connection = _FakeConnection(_inventory(HEAD_REVISION))
    apply_bootstrap_object_grants(
        cast(Connection, connection),
        revision=HEAD_REVISION,
        phase=GrantPhase.BASELINE,
        posture=BootstrapGrantPosture.CURRENT,
    )

    assert connection.mutation_sql == [
        _render_expected_runtime_grant(grant)
        for grant in _inventory(HEAD_REVISION)
        if grant.grantee in {APP_RUNTIME_ROLE_NAME, RETENTION_RUNTIME_ROLE_NAME}
    ]


def test_bootstrap_object_grant_current_reassert_rejects_drift_before_first_mutation() -> None:
    """CURRENT 任一 missing/extra tuple 都必须在第一条幂等 GRANT 前零写拒绝。"""
    from ai_employee.infrastructure.db.database_grants import (
        BootstrapGrantPosture,
        apply_bootstrap_object_grants,
    )

    current_inventory = _inventory(HEAD_REVISION)
    drift_cases = (
        current_inventory[:-1],
        tuple(sorted((*current_inventory, current_inventory[-1]))),
    )
    for grants in drift_cases:
        connection = _FakeConnection(grants)
        _assert_invariant(
            lambda connection=connection: apply_bootstrap_object_grants(
                cast(Connection, connection),
                revision=HEAD_REVISION,
                phase=GrantPhase.BASELINE,
                posture=BootstrapGrantPosture.CURRENT,
            )
        )
        assert connection.mutation_sql == []


def test_bootstrap_object_grant_mutator_rejects_drift_before_first_mutation() -> None:
    """pre-runtime 缺失 owner tuple、partial runtime 或 unknown revision 均不得先 GRANT。"""
    from ai_employee.infrastructure.db.database_grants import (
        BootstrapGrantPosture,
        apply_bootstrap_object_grants,
    )

    base_pre_runtime = _pre_runtime_inventory(BASE_REVISION)
    drift_cases = (
        base_pre_runtime[:-1],
        tuple(sorted((*base_pre_runtime, _inventory(BASE_REVISION)[-1]))),
    )
    for grants in drift_cases:
        connection = _FakeConnection(grants)
        _assert_invariant(
            lambda connection=connection: apply_bootstrap_object_grants(
                cast(Connection, connection),
                revision=BASE_REVISION,
                phase=GrantPhase.BASELINE,
                posture=BootstrapGrantPosture.PRE_RUNTIME,
            )
        )
        assert connection.mutation_sql == []

    unknown_revision_connection = _FakeConnection(_pre_runtime_inventory(HEAD_REVISION))
    _assert_invariant(
        lambda: apply_bootstrap_object_grants(
            cast(Connection, unknown_revision_connection),
            revision="unknown_revision",
            phase=GrantPhase.BASELINE,
            posture=BootstrapGrantPosture.PRE_RUNTIME,
        )
    )
    assert unknown_revision_connection.calls == []


def test_bootstrap_object_grant_public_contract_is_narrow() -> None:
    """bootstrap grant 入口只接收当前 revision/phase 与角色存在事实，不接收任意 tuple。"""
    from ai_employee.infrastructure.db.database_grants import (
        apply_bootstrap_object_grants,
        verify_bootstrap_object_grants,
    )

    expected = {
        "connection",
        "revision",
        "phase",
        "posture",
    }
    assert set(inspect.signature(verify_bootstrap_object_grants).parameters) == expected
    assert set(inspect.signature(apply_bootstrap_object_grants).parameters) == expected


def test_database_grants_public_function_surface_is_closed() -> None:
    """稳定公共函数面只暴露六个 reader/verifier/apply 入口，不泄露 registry helper。"""
    public_functions = {
        name
        for name, value in vars(database_grants_module).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and value.__module__ == database_grants_module.__name__
    }
    assert public_functions == {
        "apply_bootstrap_object_grants",
        "read_object_grants",
        "verify_bootstrap_object_grants",
        "verify_pre_migration_object_grants",
        "apply_migration_grant_delta",
        "verify_object_grants",
    }


def test_destination_verifier_returns_new_snapshot_for_callback_chain() -> None:
    """delta 后完整 destination multiset 验证成功才返回下一步 snapshot。"""
    connection = _FakeConnection(_inventory(DESTINATION_REVISION))
    snapshot = verify_object_grants(
        cast(Connection, connection),
        revision=DESTINATION_REVISION,
        phase=GrantPhase.ACTIVE,
    )
    assert snapshot == CatalogObjectSnapshot(
        revision=DESTINATION_REVISION,
        phase=GrantPhase.ACTIVE,
        grants=_inventory(DESTINATION_REVISION, GrantPhase.ACTIVE),
    )


@pytest.mark.parametrize(
    "mutation",
    (
        pytest.param(lambda grants: grants[:-1], id="missing"),
        pytest.param(lambda grants: (*grants, grants[-1]), id="duplicate"),
        pytest.param(
            lambda grants: (*grants, replace(grants[-1], object_name="extra_object")),
            id="extra",
        ),
        pytest.param(
            lambda grants: (replace(grants[0], grantor=OTHER_GRANTOR), *grants[1:]),
            id="wrong-grantor",
        ),
        pytest.param(
            lambda grants: (replace(grants[0], is_grantable=True), *grants[1:]),
            id="grant-option",
        ),
    ),
)
def test_complete_multiset_comparison_rejects_every_drift(
    mutation: Callable[[tuple[ObjectGrantTuple, ...]], tuple[ObjectGrantTuple, ...]],
) -> None:
    """pure comparator 同样不得以 set/subset 语义吞掉重复或额外 tuple。"""
    expected = _inventory(HEAD_REVISION)
    _assert_invariant(lambda: _verify_exact_grant_multiset(tuple(mutation(expected)), expected))
