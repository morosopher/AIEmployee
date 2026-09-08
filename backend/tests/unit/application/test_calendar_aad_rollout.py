"""验证 rollout 文件的闭合 JSON 协议，避免缺键、重复键和 bool 冒充零计数。"""

import json
from uuid import UUID

import pytest

from ai_employee.application.calendar_aad_digests import calendar_pair_digest_v1
from ai_employee.application.use_cases.calendar_aad_rollout import (
    CalendarAadBinding,
    CalendarAadFacts,
    CalendarAadRolloutError,
    create_rollout_artifact,
    parse_rollout_artifact,
    serialize_rollout_artifact,
)

BINDING = CalendarAadBinding("calendar-aad-0019-synthetic", "sha256:" + "0123456789abcdef" * 4)


def zero_bytes() -> bytes:
    """使用完整的规范零状态 fixture，不绕过闭合模型构造。"""
    return serialize_rollout_artifact(
        create_rollout_artifact(BINDING, CalendarAadFacts("20260809_0018", (), ()))
    )


@pytest.mark.parametrize(
    "key",
    [
        "schema_version",
        "source_revision",
        "target_revision",
        "safety_margin_seconds",
        "affected_connection_count",
        "connection_digests",
        "earliest_token_expires_at",
        "rollout_deadline",
    ],
)
def test_calendar_aad_artifact_requires_every_fixed_key(key):
    """所有固定字段必须在文件中显式存在，模型默认值不能替运维工件补齐事实。"""
    payload = json.loads(zero_bytes())
    del payload[key]
    with pytest.raises(CalendarAadRolloutError, match="requires attention"):
        parse_rollout_artifact(json.dumps(payload).encode(), BINDING)


@pytest.mark.parametrize("value", [False, 0.0])
def test_calendar_aad_zero_count_is_exact_integer(value):
    """JSON bool/浮点值不能因 Python 与 0 相等而通过零分支。"""
    payload = json.loads(zero_bytes())
    payload["affected_pair_count"] = value
    with pytest.raises(CalendarAadRolloutError):
        parse_rollout_artifact(json.dumps(payload).encode(), BINDING)


def test_calendar_aad_artifact_rejects_duplicate_json_keys():
    """相同值的重复键也不属于唯一规范工件，拒绝 last-key-wins 解析。"""
    raw = zero_bytes().replace(b"{", b'{"safety_margin_seconds":900,', 1)
    with pytest.raises(CalendarAadRolloutError):
        parse_rollout_artifact(raw, BINDING)


def test_calendar_aad_pair_digest_matches_frozen_vector():
    """固定 NUL 分帧向量，不调用另一个复制公式来生成 expected。"""
    assert (
        calendar_pair_digest_v1(
            UUID("00000000-0000-0000-0000-000000000201"), "synthetic-calendar-opaque"
        )
        == "d10d3615cb36d57dff1053e4028f375d7d6af0d5f8fc81244f2b61efe9196e61"
    )
