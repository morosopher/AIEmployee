"""冻结 append-only OAuth 双通道结果联合的严格匹配及永久闭合规则。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from ai_employee.application.calendar_aad_digests import connection_digest_v1
from ai_employee.application.ports.credential_rotation import (
    ConfirmedV1,
    CredentialReplacedV1,
    OAuthRefreshAuditRecord,
    RecoveryUnsatisfiedV1,
    parse_oauth_refresh_result,
)
from ai_employee.infrastructure.db.repositories.oauth_lifecycle import _closed_groups

USER_ID = UUID("00000000-0000-0000-0000-000000000101")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000201")
ATTEMPT_ID = "00000000-0000-0000-0000-000000000601"
RECOVERY_ID = "00000000-0000-0000-0000-000000000602"
NOW = datetime(2030, 1, 1, tzinfo=UTC)


def started() -> OAuthRefreshAuditRecord:
    """使用完整关闭字段集合创建合成 automatic started 输入。"""
    return OAuthRefreshAuditRecord(
        event_id=1,
        user_id=USER_ID,
        event_type="oauth.refresh_started",
        created_at=NOW,
        metadata={
            "fence_schema_version": "oauth_refresh_fence.v1",
            "source": "provider_refresh",
            "refresh_attempt_id": ATTEMPT_ID,
            "connection_digest": connection_digest_v1(CONNECTION_ID),
            "source_revision": None,
            "target_revision": None,
            "rollout_digest_v1": None,
            "fence_generation": 2,
            "pre_credential_snapshot_digest_v1": "1" * 64,
            "pre_refresh_credential_snapshot_digest_v1": "2" * 64,
            "old_refresh_token_identity_v1": "a" * 64,
            "refresh_token_identity_key_version": 7,
            "result_code": "oauth_refresh_started",
        },
    )


def recovery_started() -> OAuthRefreshAuditRecord:
    """恢复 started 引用唯一原 fence，并冻结合法 F/S/T 关系。"""
    return OAuthRefreshAuditRecord(
        event_id=2,
        user_id=USER_ID,
        event_type="oauth.refresh_recovery_authorization_started",
        created_at=NOW + timedelta(seconds=1),
        metadata={
            "recovery_schema_version": "oauth_refresh_recovery_authorization.v1",
            "source": "progressive_recovery",
            "oauth_attempt_id": RECOVERY_ID,
            "connection_digest": connection_digest_v1(CONNECTION_ID),
            "refresh_attempt_id": ATTEMPT_ID,
            "started_source": "provider_refresh",
            "fence_generation": 2,
            "source_generation": 3,
            "target_generation": 4,
            "pre_credential_snapshot_digest_v1": "1" * 64,
            "pre_refresh_credential_snapshot_digest_v1": "2" * 64,
            "old_refresh_token_identity_v1": "a" * 64,
            "refresh_token_identity_key_version": 7,
            "result_code": "oauth_refresh_recovery_authorization_started",
        },
    )


def result_event(
    kind: str = "confirmed", *, disposition: str = "different"
) -> OAuthRefreshAuditRecord:
    """显式构造三种结果输入，不以生产 matcher 或当前凭据推导预期结果。"""
    common = {
        "source": "provider_refresh",
        "started_source": "provider_refresh",
        "refresh_attempt_id": ATTEMPT_ID,
        "connection_digest": connection_digest_v1(CONNECTION_ID),
        "fence_generation": 2,
        "source_generation": 2,
        "pre_credential_snapshot_digest_v1": "1" * 64,
        "pre_refresh_credential_snapshot_digest_v1": "2" * 64,
        "old_refresh_token_identity_v1": "a" * 64,
        "refresh_token_identity_key_version": 7,
    }
    if kind == "unsatisfied":
        metadata = {
            **common,
            "source": "progressive_recovery",
            "source_generation": 3,
            "target_generation": 4,
            "result_schema_version": "oauth_refresh_recovery_unsatisfied.v1",
            "recovery_oauth_attempt_id": RECOVERY_ID,
            "recovery_authorization_started_event_id": "2",
            "requested_capabilities": ["calendar.read", "mail.read"],
            "capability_transition": "action_required",
            "error_code": "oauth_authorization_failed",
            "result_code": "oauth_refresh_recovery_unsatisfied",
        }
        event_type = "oauth.refresh_recovery_unsatisfied"
    else:
        metadata = {
            **common,
            "pre_generation": 2,
            "post_generation": 2,
            "post_credential_snapshot_digest_v1": "3" * 64,
            "post_refresh_credential_snapshot_digest_v1": "4" * 64,
            "new_refresh_token_identity_v1": "b" * 64 if disposition == "different" else "a" * 64,
            "new_refresh_token_identity_key_version": 7,
            "token_expires_at": "2030-01-01T01:00:00.000000Z",
            "refresh_identity_changed": disposition == "different",
        }
        if kind == "replacement":
            metadata.update(
                {
                    "proof_schema_version": "oauth_refresh_credential_replaced.v1",
                    "source": "progressive_recovery",
                    "source_generation": 3,
                    "target_generation": 4,
                    "pre_generation": 4,
                    "post_generation": 4,
                    "recovery_oauth_attempt_id": RECOVERY_ID,
                    "recovery_authorization_started_event_id": "2",
                    "result_code": "oauth_refresh_credential_replaced",
                }
            )
            event_type = "oauth.refresh_credential_replaced"
        else:
            metadata.update(
                {
                    "result_schema_version": "oauth_refresh_confirmed.v1",
                    "source_revision": None,
                    "target_revision": None,
                    "rollout_digest_v1": None,
                    "refresh_token_disposition": disposition,
                    "rollout_deadline_candidate": None,
                    "result_code": "oauth_refresh_confirmed",
                }
            )
            event_type = "oauth.refresh_confirmed"
    return OAuthRefreshAuditRecord(
        event_id=3,
        user_id=USER_ID,
        event_type=event_type,
        created_at=NOW + timedelta(seconds=2),
        metadata=metadata,
    )


def parse(event: OAuthRefreshAuditRecord, **kwargs: object):
    """只提供 immutable 历史事件；接口刻意没有 current credential 比较参数。"""
    return parse_oauth_refresh_result(
        event,
        automatic=kwargs.get("automatic", started()),
        recovery=kwargs.get("recovery", recovery_started()),
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        key_version=7,
    )


@pytest.mark.parametrize("disposition", ["missing", "same", "different"])
def test_confirmed_matrix_accepts_three_known_valid_results(disposition: str) -> None:
    """missing/same 只证明 access 更新，different 只关闭自己的 automatic started。"""
    result = parse(result_event(disposition=disposition))
    assert isinstance(result, ConfirmedV1)
    assert result.refresh_identity_changed is (disposition == "different")


@pytest.mark.parametrize(
    "kind,expected", [("unsatisfied", RecoveryUnsatisfiedV1), ("replacement", CredentialReplacedV1)]
)
def test_recovery_union_is_parsed_without_current_state(kind: str, expected: type) -> None:
    """历史闭合不依赖后来合法 generation、scope、capability 或 ciphertext 值。"""
    assert isinstance(parse(result_event(kind)), expected)


@pytest.mark.parametrize(
    "field,value",
    [
        ("refresh_identity_changed", False),
        ("new_refresh_token_identity_v1", "a" * 64),
        ("source_generation", 3),
        ("pre_generation", 3),
        ("post_generation", 3),
        ("new_refresh_token_identity_key_version", 8),
        ("refresh_token_identity_key_version", 8),
        ("connection_digest", "0" * 64),
        ("started_source", "calendar_aad_preflight"),
        ("refresh_attempt_id", "00000000000000000000000000000601"),
        ("rollout_deadline_candidate", "2030-01-01T00:45:00.000000Z"),
        ("token_expires_at", "2030-01-01T01:00:00Z"),
        ("unexpected_query", "synthetic-canary"),
    ],
)
def test_confirmed_mismatch_cannot_close_automatic(field: str, value: object) -> None:
    """任何配对、类型、schema 或 disposition/identity 矛盾都保持 fence 未决。"""
    event = result_event()
    assert parse(replace(event, metadata={**event.metadata, field: value})) is None


@pytest.mark.parametrize("kind", ["confirmed", "replacement", "unsatisfied"])
def test_result_requires_exact_user_outer_fields_and_strict_timestamp(kind: str) -> None:
    """别人的事件、错误 actor/task 或同一时刻结果都不能变成匹配证明。"""
    event = result_event(kind)
    assert parse(replace(event, user_id=UUID("00000000-0000-0000-0000-000000000102"))) is None
    assert parse(replace(event, task_id=UUID(int=1))) is None
    assert parse(replace(event, created_at=NOW)) is None
    assert parse(replace(event, actor_type="user")) is None


@pytest.mark.parametrize("kind", ["replacement", "unsatisfied"])
def test_recovery_result_requires_exact_original_and_recovery_links(kind: str) -> None:
    """recovery event ID、OAuthAttempt 和 F/S/T 必须同时绑定，不能只按旧 token 配对。"""
    event = result_event(kind)
    for field, value in (
        ("recovery_oauth_attempt_id", ATTEMPT_ID),
        ("recovery_authorization_started_event_id", "1"),
        ("target_generation", 5),
        ("pre_credential_snapshot_digest_v1", "9" * 64),
    ):
        assert parse(replace(event, metadata={**event.metadata, field: value})) is None
    assert parse(event, recovery=None) is None


@pytest.mark.parametrize(
    "field,value",
    [("refresh_identity_changed", False), ("new_refresh_token_identity_v1", "a" * 64)],
)
def test_replacement_cannot_claim_same_plaintext(field: str, value: object) -> None:
    """重加密或布尔标记不能伪造唯一允许消费旧 fence 的不同 token proof。"""
    event = result_event("replacement")
    assert parse(replace(event, metadata={**event.metadata, field: value})) is None


def test_unsatisfied_has_no_credential_post_or_expiry_and_capabilities_are_canonical() -> None:
    """unsatisfied 只关闭恢复授权；不得夹带能伪装成凭据替换的字段。"""
    event = result_event("unsatisfied")
    for field, value in (
        ("post_credential_snapshot_digest_v1", "3" * 64),
        ("new_refresh_token_identity_v1", "b" * 64),
        ("token_expires_at", "2030-01-01T01:00:00.000000Z"),
        ("requested_capabilities", ["mail.read", "calendar.read"]),
        ("error_code", "raw-provider-error"),
    ):
        assert parse(replace(event, metadata={**event.metadata, field: value})) is None


@pytest.mark.parametrize("kind", ["confirmed", "replacement", "unsatisfied"])
def test_naive_result_time_is_rejected_without_comparing_to_aware_started(kind: str) -> None:
    """不可信审计时间先验证 UTC，再比较顺序；不能抛出 TypeError 阻断安全匹配。"""
    assert parse(replace(result_event(kind), created_at=NOW.replace(tzinfo=None))) is None


def test_replacement_flag_requires_json_boolean_not_integer() -> None:
    """JSON true 与数字 1 不同；Pydantic Literal 不得把整数默认为 replacement proof。"""
    event = result_event("replacement")
    assert parse(replace(event, metadata={**event.metadata, "refresh_identity_changed": 1})) is None


@pytest.mark.parametrize("kind", ["confirmed", "replacement", "unsatisfied"])
@pytest.mark.parametrize("duplicate_timing", ["none", "expired", "at_cutoff", "after_cutoff"])
def test_closing_uniqueness_uses_complete_history_before_cutoff(
    kind: str, duplicate_timing: str
) -> None:
    """较新的重复 closing 仍使旧组歧义，不能先筛过期项再选出看似唯一的结果。"""
    automatic = started()
    recovery = recovery_started() if kind != "confirmed" else None
    result = result_event(kind)
    records = (automatic, result) if recovery is None else (automatic, recovery, result)
    cutoff = NOW + timedelta(days=1)
    if duplicate_timing != "none":
        created_at = {
            "expired": result.created_at + timedelta(microseconds=1),
            "at_cutoff": cutoff,
            "after_cutoff": cutoff + timedelta(microseconds=1),
        }[duplicate_timing]
        duplicate = replace(result, event_id=4, created_at=created_at)
        # 先证明输入本身是合法 matching result；失败必须来自组判断而不是错误 fixture。
        assert parse_oauth_refresh_result(
            duplicate,
            automatic=automatic,
            recovery=recovery,
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            key_version=7,
        ) is not None
        records += (duplicate,)
    groups = _closed_groups(
        records,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        key_version=7,
        cutoff=cutoff,
    )
    assert len(groups) == (1 if duplicate_timing == "none" else 0)
