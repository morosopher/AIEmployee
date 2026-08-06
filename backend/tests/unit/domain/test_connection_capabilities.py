"""验证连接能力的稳定状态、依赖闭包与 fail-closed 更新规则。"""

from collections.abc import Callable, Iterator, Mapping, MutableMapping, MutableSet
from collections.abc import Set as AbstractSet
from types import MappingProxyType
from typing import cast

import pytest

import ai_employee.domain.connections as connections_module
from ai_employee.domain.connections import (
    CAPABILITY_DEPENDENCIES,
    CapabilityStatus,
    ConnectionCapability,
    validate_capability_disable,
    validate_capability_enable,
)
from ai_employee.domain.errors import StateConflictError

type CapabilityValidator = Callable[
    [ConnectionCapability, AbstractSet[ConnectionCapability]],
    frozenset[ConnectionCapability],
]

# 合成图只用于验证递归闭包和 fail-closed 防御，不改变生产依赖常量。
INDIRECT_DEPENDENCY_GRAPH: Mapping[
    ConnectionCapability, frozenset[ConnectionCapability]
] = MappingProxyType(
    {
        ConnectionCapability.CALENDAR_WRITE: frozenset({ConnectionCapability.MAIL_SEND}),
        ConnectionCapability.MAIL_SEND: frozenset({ConnectionCapability.MAIL_READ}),
    }
)
SELF_DEPENDENCY_GRAPH: Mapping[
    ConnectionCapability, frozenset[ConnectionCapability]
] = MappingProxyType(
    {
        ConnectionCapability.MAIL_SEND: frozenset({ConnectionCapability.MAIL_SEND}),
    }
)
MULTI_NODE_CYCLE_GRAPH: Mapping[
    ConnectionCapability, frozenset[ConnectionCapability]
] = MappingProxyType(
    {
        ConnectionCapability.MAIL_READ: frozenset({ConnectionCapability.MAIL_SEND}),
        ConnectionCapability.MAIL_SEND: frozenset({ConnectionCapability.CALENDAR_WRITE}),
        ConnectionCapability.CALENDAR_WRITE: frozenset({ConnectionCapability.MAIL_READ}),
    }
)
UNKNOWN_KEY_DEPENDENCY_GRAPH = cast(
    Mapping[ConnectionCapability, frozenset[ConnectionCapability]],
    MappingProxyType(
        {
            "mail.archive": frozenset({ConnectionCapability.MAIL_READ}),
        }
    ),
)
UNKNOWN_VALUE_DEPENDENCY_GRAPH = cast(
    Mapping[ConnectionCapability, frozenset[ConnectionCapability]],
    MappingProxyType(
        {
            ConnectionCapability.MAIL_SEND: frozenset({"mail.archive"}),
        }
    ),
)


class SyntheticCapabilitySet(AbstractSet[ConnectionCapability]):
    """可记录遍历次数并在指定轮次抛错的合成只读集合。"""

    def __init__(
        self,
        values: frozenset[ConnectionCapability],
        *,
        fail_on_iteration: int | None = None,
        iteration_error: Exception | None = None,
    ) -> None:
        """保存稳定成员，并配置用于检测重复遍历的可选合成异常。

        Args:
            values: 每次成功遍历应返回的能力成员。
            fail_on_iteration: 从一开始计数、需要抛错的遍历轮次。
            iteration_error: 到达指定轮次时抛出的合成异常。
        """
        self._values = values
        self._fail_on_iteration = fail_on_iteration
        self._iteration_error = iteration_error
        self.iteration_count = 0

    def __contains__(self, value: object) -> bool:
        """返回值是否属于合成集合。"""
        return value in self._values

    def __iter__(self) -> Iterator[ConnectionCapability]:
        """返回成员迭代器，或在配置轮次模拟变化型集合失败。"""
        self.iteration_count += 1
        if self.iteration_count == self._fail_on_iteration:
            if self._iteration_error is None:
                raise AssertionError("synthetic iteration error must be configured")
            raise self._iteration_error
        return iter(self._values)

    def __len__(self) -> int:
        """返回合成集合的稳定成员数量。"""
        return len(self._values)


def _assert_capability_dependency_conflict(
    validator: CapabilityValidator,
    capability: ConnectionCapability,
    enabled_capabilities: AbstractSet[ConnectionCapability],
    *,
    sensitive_fragment: str | None = None,
) -> None:
    """断言 capability 防御路径返回统一且不泄漏输入的稳定冲突。

    Args:
        validator: 待调用的 enable 或 disable 纯领域函数。
        capability: 请求启用或关闭的能力。
        enabled_capabilities: 当前能力集合或合成集合。
        sensitive_fragment: 不得出现在安全错误消息中的可选原始片段。
    """
    with pytest.raises(StateConflictError) as captured:
        validator(capability, enabled_capabilities)

    assert captured.value.error_code == "connection_capability_dependency_conflict"
    assert captured.value.message == "connection capabilities violate dependency requirements"
    if sensitive_fragment is not None:
        assert sensitive_fragment not in captured.value.message


def test_connection_capability_and_status_values_are_stable() -> None:
    """能力名与能力状态会持久化并进入 API，必须使用规格固定英文值。"""
    assert tuple(capability.value for capability in ConnectionCapability) == (
        "mail.read",
        "mail.send",
        "calendar.read",
        "calendar.write",
    )
    assert tuple(status.value for status in CapabilityStatus) == (
        "disabled",
        "authorizing",
        "enabled",
        "degraded",
        "action_required",
        "revoked",
    )


def test_write_capability_dependencies_are_exact_and_immutable() -> None:
    """邮件与日历写能力必须分别绑定对应只读能力，且运行期不能改写依赖。"""
    assert CAPABILITY_DEPENDENCIES == {
        ConnectionCapability.MAIL_SEND: frozenset({ConnectionCapability.MAIL_READ}),
        ConnectionCapability.CALENDAR_WRITE: frozenset(
            {ConnectionCapability.CALENDAR_READ}
        ),
    }

    with pytest.raises(TypeError):
        cast(
            MutableMapping[ConnectionCapability, frozenset[ConnectionCapability]],
            CAPABILITY_DEPENDENCIES,
        )[ConnectionCapability.MAIL_SEND] = frozenset()
    with pytest.raises(AttributeError):
        cast(
            MutableSet[ConnectionCapability],
            CAPABILITY_DEPENDENCIES[ConnectionCapability.MAIL_SEND],
        ).add(ConnectionCapability.CALENDAR_READ)


@pytest.mark.parametrize(
    ("requested", "dependency"),
    (
        (ConnectionCapability.MAIL_SEND, ConnectionCapability.MAIL_READ),
        (ConnectionCapability.CALENDAR_WRITE, ConnectionCapability.CALENDAR_READ),
    ),
)
def test_enabling_write_capability_adds_required_read_dependency(
    requested: ConnectionCapability,
    dependency: ConnectionCapability,
) -> None:
    """渐进启用写能力必须返回包含请求能力和完整依赖的不可变闭包。"""
    enabled = validate_capability_enable(requested, frozenset())

    assert enabled == frozenset({requested, dependency})
    assert type(enabled) is frozenset


def test_enabling_read_capability_preserves_existing_closed_set() -> None:
    """启用独立读能力只能扩展当前闭合集合，不能移除已有能力。"""
    current = frozenset(
        {
            ConnectionCapability.MAIL_READ,
            ConnectionCapability.MAIL_SEND,
        }
    )

    enabled = validate_capability_enable(ConnectionCapability.CALENDAR_READ, current)

    assert enabled == current | {ConnectionCapability.CALENDAR_READ}
    assert current == frozenset(
        {
            ConnectionCapability.MAIL_READ,
            ConnectionCapability.MAIL_SEND,
        }
    )


@pytest.mark.parametrize(
    ("read_capability", "write_capability"),
    (
        (ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND),
        (ConnectionCapability.CALENDAR_READ, ConnectionCapability.CALENDAR_WRITE),
    ),
)
def test_disabling_read_capability_is_rejected_while_write_remains_enabled(
    read_capability: ConnectionCapability,
    write_capability: ConnectionCapability,
) -> None:
    """写能力启用期间不能单独关闭其只读核对依赖。"""
    with pytest.raises(StateConflictError) as captured:
        validate_capability_disable(
            read_capability,
            frozenset({read_capability, write_capability}),
        )

    assert captured.value.error_code == "connection_capability_dependency_conflict"


@pytest.mark.parametrize(
    ("read_capability", "write_capability"),
    (
        (ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND),
        (ConnectionCapability.CALENDAR_READ, ConnectionCapability.CALENDAR_WRITE),
    ),
)
def test_disabling_write_capability_preserves_read_capability(
    read_capability: ConnectionCapability,
    write_capability: ConnectionCapability,
) -> None:
    """用户先关闭写能力后，仍可保留只读同步和结果核对能力。"""
    current = frozenset({read_capability, write_capability})

    assert validate_capability_disable(write_capability, current) == frozenset(
        {read_capability}
    )


def test_disabling_enabled_read_capability_returns_updated_frozenset() -> None:
    """没有写能力依赖时，关闭读能力应确定性移除且不修改输入集合。"""
    current = frozenset(
        {
            ConnectionCapability.MAIL_READ,
            ConnectionCapability.CALENDAR_READ,
        }
    )

    disabled = validate_capability_disable(ConnectionCapability.MAIL_READ, current)

    assert disabled == frozenset({ConnectionCapability.CALENDAR_READ})
    assert current == frozenset(
        {
            ConnectionCapability.MAIL_READ,
            ConnectionCapability.CALENDAR_READ,
        }
    )


def test_disabling_not_enabled_capability_is_idempotent() -> None:
    """关闭未启用能力应返回等值 frozenset，不能制造额外状态变化。"""
    current = frozenset({ConnectionCapability.MAIL_READ})

    disabled = validate_capability_disable(ConnectionCapability.CALENDAR_READ, current)

    assert disabled == current
    assert type(disabled) is frozenset


@pytest.mark.parametrize(
    ("operation", "expected"),
    (
        (
            validate_capability_enable,
            frozenset(
                {
                    ConnectionCapability.MAIL_READ,
                    ConnectionCapability.CALENDAR_READ,
                }
            ),
        ),
        (
            validate_capability_disable,
            frozenset({ConnectionCapability.MAIL_READ}),
        ),
    ),
)
def test_changing_custom_set_is_snapshotted_exactly_once(
    operation: object,
    expected: frozenset[ConnectionCapability],
) -> None:
    """变化型 Set 的首个快照必须成为唯一事实，不能因第二次遍历泄漏异常。"""
    validator = cast(CapabilityValidator, operation)
    current = SyntheticCapabilitySet(
        frozenset({ConnectionCapability.MAIL_READ}),
        fail_on_iteration=2,
        iteration_error=RuntimeError("synthetic second iteration"),
    )

    assert validator(ConnectionCapability.CALENDAR_READ, current) == expected
    assert current.iteration_count == 1


@pytest.mark.parametrize("error_type", (RuntimeError, AttributeError))
@pytest.mark.parametrize("operation", (validate_capability_enable, validate_capability_disable))
def test_snapshot_iteration_errors_are_mapped_to_stable_conflict(
    operation: object,
    error_type: type[Exception],
) -> None:
    """首次快照的可预期迭代错误不能以 RuntimeError 或 AttributeError 泄漏。"""
    validator = cast(CapabilityValidator, operation)
    current = SyntheticCapabilitySet(
        frozenset({ConnectionCapability.MAIL_READ}),
        fail_on_iteration=1,
        iteration_error=error_type("synthetic capability set failure"),
    )

    _assert_capability_dependency_conflict(
        validator,
        ConnectionCapability.CALENDAR_READ,
        current,
        sensitive_fragment="synthetic capability set failure",
    )


@pytest.mark.parametrize(
    ("operation", "requested", "current", "expected"),
    (
        (
            validate_capability_enable,
            ConnectionCapability.CALENDAR_READ,
            {ConnectionCapability.MAIL_READ},
            frozenset(
                {
                    ConnectionCapability.MAIL_READ,
                    ConnectionCapability.CALENDAR_READ,
                }
            ),
        ),
        (
            validate_capability_disable,
            ConnectionCapability.CALENDAR_READ,
            {
                ConnectionCapability.MAIL_READ,
                ConnectionCapability.CALENDAR_READ,
            },
            frozenset({ConnectionCapability.MAIL_READ}),
        ),
    ),
)
def test_builtin_mutable_set_is_copied_into_detached_frozenset(
    operation: object,
    requested: ConnectionCapability,
    current: set[ConnectionCapability],
    expected: frozenset[ConnectionCapability],
) -> None:
    """内建 mutable set 可以作为集合输入，但结果必须解除共享并冻结。"""
    validator = cast(CapabilityValidator, operation)

    result = validator(requested, current)
    current.clear()

    assert result == expected
    assert type(result) is frozenset


@pytest.mark.parametrize(
    "invalid_collection",
    (
        [ConnectionCapability.MAIL_READ],
        (ConnectionCapability.MAIL_READ,),
    ),
)
@pytest.mark.parametrize("operation", (validate_capability_enable, validate_capability_disable))
def test_non_set_enabled_collections_are_rejected(
    operation: object,
    invalid_collection: object,
) -> None:
    """list 或 tuple 即使只含合法枚举也不是集合契约，必须保持 fail closed。"""
    validator = cast(CapabilityValidator, operation)

    _assert_capability_dependency_conflict(
        validator,
        ConnectionCapability.CALENDAR_READ,
        cast(AbstractSet[ConnectionCapability], invalid_collection),
    )


@pytest.mark.parametrize("raw_capability", ("mail.archive", "mail.read"))
@pytest.mark.parametrize("operation", (validate_capability_enable, validate_capability_disable))
def test_raw_capability_strings_never_become_enabled(
    operation: object,
    raw_capability: str,
) -> None:
    """未知或已知同值字符串都必须 fail closed，不能借助 StrEnum 相等混入。"""
    validator = cast(CapabilityValidator, operation)

    _assert_capability_dependency_conflict(
        validator,
        cast(ConnectionCapability, raw_capability),
        frozenset(),
        sensitive_fragment=raw_capability,
    )


@pytest.mark.parametrize("invalid_member", ("mail.read", 1, object()))
@pytest.mark.parametrize("operation", (validate_capability_enable, validate_capability_disable))
def test_non_enum_enabled_set_members_are_rejected(
    operation: object,
    invalid_member: object,
) -> None:
    """当前集合含字符串或其他污染值时必须拒绝，不能静默规范化为已启用能力。"""
    validator = cast(CapabilityValidator, operation)

    _assert_capability_dependency_conflict(
        validator,
        ConnectionCapability.CALENDAR_READ,
        cast(frozenset[ConnectionCapability], frozenset({invalid_member})),
    )


@pytest.mark.parametrize("operation", (validate_capability_enable, validate_capability_disable))
def test_incomplete_current_dependency_closure_is_rejected(operation: object) -> None:
    """缺少读依赖的既有写能力表示状态污染，更新前必须 fail closed。"""
    validator = cast(CapabilityValidator, operation)

    _assert_capability_dependency_conflict(
        validator,
        ConnectionCapability.CALENDAR_READ,
        frozenset({ConnectionCapability.MAIL_SEND}),
    )


def test_indirect_dependency_closure_is_enabled_and_protected_on_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """递归依赖必须随 enable 加入，并在 disable 根依赖时阻止破坏闭包。"""
    original_dependencies = connections_module.CAPABILITY_DEPENDENCIES

    with monkeypatch.context() as patch:
        patch.setattr(
            connections_module,
            "CAPABILITY_DEPENDENCIES",
            INDIRECT_DEPENDENCY_GRAPH,
        )
        enabled = validate_capability_enable(
            ConnectionCapability.CALENDAR_WRITE,
            frozenset(),
        )

        assert enabled == frozenset(
            {
                ConnectionCapability.CALENDAR_WRITE,
                ConnectionCapability.MAIL_SEND,
                ConnectionCapability.MAIL_READ,
            }
        )
        _assert_capability_dependency_conflict(
            validate_capability_disable,
            ConnectionCapability.MAIL_READ,
            enabled,
            sensitive_fragment=ConnectionCapability.MAIL_READ.value,
        )

    assert connections_module.CAPABILITY_DEPENDENCIES is original_dependencies


@pytest.mark.parametrize(
    "cyclic_dependencies",
    (SELF_DEPENDENCY_GRAPH, MULTI_NODE_CYCLE_GRAPH),
    ids=("self-cycle", "multi-node-cycle"),
)
@pytest.mark.parametrize("operation", (validate_capability_enable, validate_capability_disable))
def test_capability_dependency_cycles_fail_closed_without_leaking_input(
    monkeypatch: pytest.MonkeyPatch,
    operation: object,
    cyclic_dependencies: Mapping[
        ConnectionCapability, frozenset[ConnectionCapability]
    ],
) -> None:
    """合法类型的依赖环必须被 enable/disable 同样拒绝，且错误不得回显能力值。"""
    validator = cast(CapabilityValidator, operation)
    original_dependencies = connections_module.CAPABILITY_DEPENDENCIES
    # 合成依赖仍使用与生产常量相同的两层不可变结构，只改变图语义以形成环。
    with pytest.raises(TypeError):
        cast(
            MutableMapping[ConnectionCapability, frozenset[ConnectionCapability]],
            cyclic_dependencies,
        )[ConnectionCapability.MAIL_SEND] = frozenset()
    with pytest.raises(AttributeError):
        cast(
            MutableSet[ConnectionCapability],
            cyclic_dependencies[ConnectionCapability.MAIL_SEND],
        ).add(ConnectionCapability.CALENDAR_READ)

    with monkeypatch.context() as patch:
        patch.setattr(
            connections_module,
            "CAPABILITY_DEPENDENCIES",
            cyclic_dependencies,
        )
        _assert_capability_dependency_conflict(
            validator,
            ConnectionCapability.MAIL_SEND,
            frozenset(
                {
                    ConnectionCapability.MAIL_READ,
                    ConnectionCapability.MAIL_SEND,
                    ConnectionCapability.CALENDAR_WRITE,
                }
            ),
            sensitive_fragment=ConnectionCapability.MAIL_SEND.value,
        )

    assert connections_module.CAPABILITY_DEPENDENCIES is original_dependencies


@pytest.mark.parametrize(
    ("invalid_dependencies", "sensitive_fragment"),
    (
        (UNKNOWN_KEY_DEPENDENCY_GRAPH, "mail.archive"),
        (UNKNOWN_VALUE_DEPENDENCY_GRAPH, "mail.archive"),
    ),
    ids=("unknown-key", "unknown-value"),
)
@pytest.mark.parametrize("operation", (validate_capability_enable, validate_capability_disable))
def test_unknown_dependency_graph_members_fail_closed_without_leaking_input(
    monkeypatch: pytest.MonkeyPatch,
    operation: object,
    invalid_dependencies: Mapping[
        ConnectionCapability, frozenset[ConnectionCapability]
    ],
    sensitive_fragment: str,
) -> None:
    """依赖图的未知 key/value 必须由 enable/disable 同样映射为安全稳定冲突。"""
    validator = cast(CapabilityValidator, operation)
    original_dependencies = connections_module.CAPABILITY_DEPENDENCIES

    with monkeypatch.context() as patch:
        patch.setattr(
            connections_module,
            "CAPABILITY_DEPENDENCIES",
            invalid_dependencies,
        )
        _assert_capability_dependency_conflict(
            validator,
            ConnectionCapability.MAIL_READ,
            frozenset(),
            sensitive_fragment=sensitive_fragment,
        )

    assert connections_module.CAPABILITY_DEPENDENCIES is original_dependencies
