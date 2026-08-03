"""验证审批提案使用确定性哈希并冻结精确 JSON 载荷。"""

import hashlib
from collections.abc import Callable
from typing import cast

import pytest

from ai_employee.domain.tasks import ApprovalProposal, ApprovalStatus, JsonValue


def test_public_constructor_cannot_forge_approval_proposal() -> None:
    """普通构造调用不得绕过工厂注入批准状态、伪哈希或非对象载荷。"""
    constructor = cast(Callable[..., ApprovalProposal], ApprovalProposal)

    with pytest.raises(TypeError):
        constructor(
            action="synthetic.action",
            payload_hash="0" * 64,
            status=ApprovalStatus.APPROVED,
            _canonical_payload="[]",
        )


def test_approval_factory_rejects_non_object_root() -> None:
    """动态调用边界也必须拒绝 list 等非普通字典根节点。"""
    factory = cast(Callable[[str, object], ApprovalProposal], ApprovalProposal.create)

    with pytest.raises(TypeError, match="JSON object"):
        factory("synthetic.action", ["synthetic-item"])


def test_approval_hash_changes_when_payload_changes() -> None:
    """同一动作的载荷值变化必须生成新的哈希并保持初始待审批状态。"""
    first = ApprovalProposal.create("calendar.create", {"title": "Synthetic A"})
    second = ApprovalProposal.create("calendar.create", {"title": "Synthetic B"})

    assert first.payload_hash != second.payload_hash
    assert first.action == "calendar.create"
    assert first.status is ApprovalStatus.PENDING


def test_payload_key_order_does_not_change_hash() -> None:
    """同一动作与同一 JSON 数据不应因字典插入顺序产生不同审批哈希。"""
    first = ApprovalProposal.create(
        "email.send",
        {"recipient_alias": "synthetic-recipient", "subject": "Synthetic subject"},
    )
    second = ApprovalProposal.create(
        "email.send",
        {"subject": "Synthetic subject", "recipient_alias": "synthetic-recipient"},
    )

    assert first.payload_hash == second.payload_hash


def test_payload_hash_uses_canonical_utf8_json() -> None:
    """哈希输入必须使用未转义 Unicode、排序键和紧凑分隔符的 UTF-8 JSON。"""
    payload: dict[str, JsonValue] = {
        "说明": "合成内容",
        "details": {"enabled": True, "items": [1, None]},
    }
    canonical_json = '{"action":"synthetic.action","payload":{"details":{"enabled":true,"items":[1,null]},"说明":"合成内容"}}'

    proposal = ApprovalProposal.create("synthetic.action", payload)

    assert proposal.payload_hash == hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    assert len(proposal.payload_hash) == 64


@pytest.mark.parametrize(
    "invalid_number",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_payload_rejects_non_finite_numbers(invalid_number: float) -> None:
    """NaN 与无穷值不是标准 JSON 数字，不得进入不稳定审批哈希。"""
    with pytest.raises(ValueError, match="finite"):
        ApprovalProposal.create("synthetic.action", {"value": invalid_number})


@pytest.mark.parametrize(
    "invalid_value",
    [
        pytest.param(b"synthetic-bytes", id="bytes"),
        pytest.param(("synthetic",), id="tuple"),
        pytest.param({"synthetic"}, id="set"),
        pytest.param(object(), id="object"),
    ],
)
def test_payload_rejects_non_json_values(invalid_value: object) -> None:
    """审批载荷只接受递归 JSON 类型，不依赖编码器的非标准隐式转换。"""
    payload = cast(dict[str, JsonValue], {"value": invalid_value})

    with pytest.raises(TypeError, match="JSON"):
        ApprovalProposal.create("synthetic.action", payload)


def test_payload_rejects_non_string_object_keys() -> None:
    """JSON 对象键必须是字符串，避免排序与跨语言持久化语义不一致。"""
    payload = cast(dict[str, JsonValue], {1: "synthetic-value"})

    with pytest.raises(TypeError, match="JSON object keys"):
        ApprovalProposal.create("synthetic.action", payload)


def test_payload_rejects_cyclic_containers() -> None:
    """循环容器不是 JSON，必须稳定拒绝而不是递归到解释器上限。"""
    payload: dict[str, JsonValue] = {}
    payload["self"] = payload

    with pytest.raises(ValueError, match="cyclic"):
        ApprovalProposal.create("synthetic.action", payload)


def test_payload_is_deeply_frozen_from_input_and_public_copies() -> None:
    """原始容器或公开副本的深层修改都不得改变已冻结载荷及其哈希。"""
    source_payload: dict[str, JsonValue] = {
        "title": "Synthetic event",
        "details": {
            "labels": ["initial"],
            "options": {"notify": False},
        },
    }
    proposal = ApprovalProposal.create("calendar.create", source_payload)
    frozen_payload = proposal.payload
    frozen_hash = proposal.payload_hash

    source_details = cast(dict[str, JsonValue], source_payload["details"])
    cast(list[JsonValue], source_details["labels"]).append("tampered-input")
    cast(dict[str, JsonValue], source_details["options"])["notify"] = True
    source_payload["title"] = "Tampered input"

    assert proposal.payload == frozen_payload
    assert proposal.payload_hash == frozen_hash

    exposed_payload = proposal.payload
    exposed_details = cast(dict[str, JsonValue], exposed_payload["details"])
    cast(list[JsonValue], exposed_details["labels"]).append("tampered-copy")
    cast(dict[str, JsonValue], exposed_details["options"])["notify"] = True
    exposed_payload["title"] = "Tampered copy"

    assert proposal.payload == frozen_payload
    assert proposal.payload_hash == frozen_hash
