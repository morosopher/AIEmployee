"""约束真实SIGKILL演练的采样比较：允许追加，禁止旧checkpoint丢失/改写或业务事实变化。"""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tests.integration.faults.m2_process_drill import (
    CheckpointDigest,
    ProcessFacts,
    _committed_fact_differences,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
FIRST = CheckpointDigest('a' * 64, '1' * 64)
SECOND = CheckpointDigest('b' * 64, '2' * 64)
ADDITIONAL = CheckpointDigest('c' * 64, '3' * 64)


def _facts() -> ProcessFacts:
    """使用手工固定摘要与合成标识，不从待测比较器反推预期。"""
    return ProcessFacts(
        task_status='running', execution_status='executing', execution_id=UUID(int=1),
        executions=1, write_attempts=1, request_started=True, request_started_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=5), scheduled_for=None,
        checkpoints=2, checkpoint_records=(FIRST, SECOND),
    )


def test_unchanged_committed_facts_are_preserved() -> None:
    """完全相同快照可以继续演练。"""
    assert _committed_fact_differences(_facts(), _facts()) == ()


def test_last_lease_renewal_and_real_checkpoint_append_are_allowed() -> None:
    """kill前续租/追加不表示旧事实消失；旧身份与摘要仍必须逐项保留。"""
    before = _facts()
    after = replace(before, lease_expires_at=NOW + timedelta(seconds=6), checkpoints=3,
                    checkpoint_records=(FIRST, SECOND, ADDITIONAL))
    assert _committed_fact_differences(before, after) == ()


@pytest.mark.parametrize(('records', 'expected'), [
    ((FIRST,), 'checkpoint_missing'),
    ((FIRST, ADDITIONAL), 'checkpoint_missing'),
    ((FIRST, CheckpointDigest(SECOND.identity_sha256, '9' * 64)), 'checkpoint_changed'),
    ((FIRST, ADDITIONAL, CheckpointDigest('d' * 64, '4' * 64)), 'checkpoint_missing'),
])
def test_old_checkpoint_loss_replacement_or_same_identity_change_is_rejected(
    records: tuple[CheckpointDigest, ...], expected: str,
) -> None:
    """数量相同或更大也不能掩盖旧记录丢失、替换或同ID改写。"""
    after = replace(_facts(), checkpoints=len(records), checkpoint_records=records)
    assert _committed_fact_differences(_facts(), after) == (expected,)


@pytest.mark.parametrize('side', ['before', 'after'])
def test_inconsistent_checkpoint_count_is_rejected(side: str) -> None:
    """摘要清单与计数必须来自一致的实际读取，不能仅靠较大count放行。"""
    invalid = replace(_facts(), checkpoints=3)
    before, after = (invalid, _facts()) if side == 'before' else (_facts(), invalid)
    assert _committed_fact_differences(before, after) == (f'{side}_checkpoint_count',)


@pytest.mark.parametrize('side', ['before', 'after'])
def test_duplicate_checkpoint_identity_is_rejected(side: str) -> None:
    """重复身份不能被字典覆盖而隐藏不完整读取。"""
    invalid = replace(_facts(), checkpoint_records=(FIRST, SECOND, SECOND), checkpoints=3)
    before, after = (invalid, _facts()) if side == 'before' else (_facts(), invalid)
    assert _committed_fact_differences(before, after) == (f'{side}_checkpoint_identity_duplicate',)


@pytest.mark.parametrize(('field', 'value'), [
    ('task_status', 'succeeded'), ('execution_status', 'succeeded'),
    ('execution_id', UUID(int=2)), ('executions', 2), ('write_attempts', 2),
    ('request_started', False), ('request_started_at', NOW + timedelta(seconds=1)),
    ('scheduled_for', NOW),
])
def test_nonlease_business_fact_changes_are_rejected(field: str, value: object) -> None:
    """第三方改变任何业务事实仍失败，checkpoint追加例外不能扩散到状态或写入事实。"""
    assert _committed_fact_differences(_facts(), replace(_facts(), **{field: value})) == (field,)
