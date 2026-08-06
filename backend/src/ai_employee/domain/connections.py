"""定义外部连接、渐进能力状态与不可绕过的依赖更新规则。"""

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ai_employee.domain.errors import StateConflictError


class ConnectionStatus(StrEnum):
    """描述 OAuth 数据连接对后续同步的可用性。

    状态值会持久化并被 API 返回，因此使用稳定英文字符串；只有 ``CONNECTED``
    允许正常增量同步，断开或授权失效必须阻止未来同步任务继续读取供应商数据。
    """

    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    EXPIRED = "expired"
    DISCONNECTED = "disconnected"


class ConnectionCapability(StrEnum):
    """连接可独立授权和验证的四种 M2 最小能力。"""

    MAIL_READ = "mail.read"
    MAIL_SEND = "mail.send"
    CALENDAR_READ = "calendar.read"
    CALENDAR_WRITE = "calendar.write"


class CapabilityStatus(StrEnum):
    """单项连接能力从本地关闭到供应商撤销的稳定状态。"""

    DISABLED = "disabled"
    AUTHORIZING = "authorizing"
    ENABLED = "enabled"
    DEGRADED = "degraded"
    ACTION_REQUIRED = "action_required"
    REVOKED = "revoked"


# 写后核对、回复线程和 ETag 保护都依赖读取能力；两层不可变结构防止运行期放宽。
CAPABILITY_DEPENDENCIES: Final[
    Mapping[ConnectionCapability, frozenset[ConnectionCapability]]
] = MappingProxyType(
    {
        ConnectionCapability.MAIL_SEND: frozenset({ConnectionCapability.MAIL_READ}),
        ConnectionCapability.CALENDAR_WRITE: frozenset(
            {ConnectionCapability.CALENDAR_READ}
        ),
    }
)


class ConnectionCapabilityDependencyConflict(StateConflictError):
    """表示能力请求、当前集合或依赖图不能形成可信闭包。"""

    def __init__(self) -> None:
        """构造不回显未知字符串或 OAuth scope 的稳定安全冲突。"""
        super().__init__(
            error_code="connection_capability_dependency_conflict",
            message="connection capabilities violate dependency requirements",
        )


def _validate_requested_capability(capability: object) -> ConnectionCapability:
    """收窄动态调用边界上的请求能力为精确领域枚举。

    Args:
        capability: 调用方传入的待启用或关闭能力。

    Returns:
        精确的 ``ConnectionCapability`` 枚举成员。

    Raises:
        ConnectionCapabilityDependencyConflict: 输入是原始字符串或其他未知对象。
    """
    # StrEnum 与同值字符串相等，不能使用 ``in ConnectionCapability`` 等宽松判断。
    if type(capability) is not ConnectionCapability:
        raise ConnectionCapabilityDependencyConflict
    return capability


def _validate_dependency_graph() -> None:
    """验证依赖表类型、成员与全图无环不变量。

    正常运行时常量不会变化；这里仍每次 fail closed 校验，是为了避免测试注入、
    热补丁或未来维护错误让未知 capability 悄悄进入启用闭包。

    Raises:
        ConnectionCapabilityDependencyConflict: 图含非枚举值、可变集合或依赖环。
    """
    for capability, dependencies in CAPABILITY_DEPENDENCIES.items():
        if type(capability) is not ConnectionCapability or type(dependencies) is not frozenset:
            raise ConnectionCapabilityDependencyConflict
        if any(type(dependency) is not ConnectionCapability for dependency in dependencies):
            raise ConnectionCapabilityDependencyConflict

    for capability in ConnectionCapability:
        _dependency_closure(capability, visiting=frozenset())


def _dependency_closure(
    capability: ConnectionCapability,
    *,
    visiting: frozenset[ConnectionCapability],
) -> frozenset[ConnectionCapability]:
    """递归计算单项能力的全部直接和间接依赖。

    Args:
        capability: 需要解析依赖的精确能力枚举。
        visiting: 当前递归路径，用于在未来依赖表扩展时检测环。

    Returns:
        不包含能力自身的不可变递归依赖闭包。

    Raises:
        ConnectionCapabilityDependencyConflict: 当前递归路径形成依赖环。
    """
    if capability in visiting:
        raise ConnectionCapabilityDependencyConflict

    dependencies = CAPABILITY_DEPENDENCIES.get(capability, frozenset())
    path = visiting | {capability}
    closure: set[ConnectionCapability] = set()
    for dependency in sorted(dependencies, key=lambda item: item.value):
        closure.add(dependency)
        closure.update(_dependency_closure(dependency, visiting=path))
    return frozenset(closure)


def _validate_enabled_capabilities(
    enabled_capabilities: AbstractSet[ConnectionCapability],
) -> frozenset[ConnectionCapability]:
    """复制并验证当前 enabled 集合已经形成完整依赖闭包。

    Args:
        enabled_capabilities: 持久状态中当前标记 enabled 的能力集合。

    Returns:
        与输入解除共享的不可变、完整能力集合。

    Raises:
        ConnectionCapabilityDependencyConflict: 输入不是集合、含非枚举成员，或已有
            写能力缺少其直接或间接读依赖，或集合无法稳定完成一次快照。
    """
    if not isinstance(enabled_capabilities, AbstractSet):
        raise ConnectionCapabilityDependencyConflict

    # 自定义或并发变化的 Set 可能在两次遍历间改变内容；先冻结唯一快照，后续
    # 类型和闭包判断只读取该快照，避免同一调用观察到互相矛盾的能力事实。
    try:
        normalized = frozenset(enabled_capabilities)
    except (AttributeError, RuntimeError, TypeError):
        # 集合实现的内部消息可能包含未经验证输入，统一收敛且不向上显示原始上下文。
        raise ConnectionCapabilityDependencyConflict from None

    if any(type(capability) is not ConnectionCapability for capability in normalized):
        raise ConnectionCapabilityDependencyConflict

    for capability in sorted(normalized, key=lambda item: item.value):
        if not _dependency_closure(capability, visiting=frozenset()).issubset(normalized):
            raise ConnectionCapabilityDependencyConflict
    return normalized


def validate_capability_enable(
    capability: ConnectionCapability,
    enabled_capabilities: AbstractSet[ConnectionCapability],
) -> frozenset[ConnectionCapability]:
    """校验启用请求并返回包含递归依赖的确定性能力闭包。

    Args:
        capability: 请求启用的精确连接能力枚举。
        enabled_capabilities: 当前已启用且依赖完整的能力集合。

    Returns:
        当前集合、请求能力及其全部依赖组成的不可变更新集合。

    Raises:
        ConnectionCapabilityDependencyConflict: 请求值、当前集合或依赖图不可信。
    """
    requested = _validate_requested_capability(capability)
    _validate_dependency_graph()
    current = _validate_enabled_capabilities(enabled_capabilities)
    dependencies = _dependency_closure(requested, visiting=frozenset())
    return frozenset(current | dependencies | {requested})


def validate_capability_disable(
    capability: ConnectionCapability,
    enabled_capabilities: AbstractSet[ConnectionCapability],
) -> frozenset[ConnectionCapability]:
    """校验关闭请求并返回不会破坏其他已启用能力的更新集合。

    Args:
        capability: 请求关闭的精确连接能力枚举。
        enabled_capabilities: 当前已启用且依赖完整的能力集合。

    Returns:
        移除请求能力后的不可变集合；能力原本未启用时幂等返回等值副本。

    Raises:
        ConnectionCapabilityDependencyConflict: 请求值、当前集合或依赖图不可信，或
            仍启用的其他能力直接或间接依赖待关闭能力。
    """
    requested = _validate_requested_capability(capability)
    _validate_dependency_graph()
    current = _validate_enabled_capabilities(enabled_capabilities)
    remaining = current - {requested}

    # 关闭写能力会保留读能力；关闭读能力前必须先清除所有依赖它的写能力。
    for enabled in sorted(remaining, key=lambda item: item.value):
        if requested in _dependency_closure(enabled, visiting=frozenset()):
            raise ConnectionCapabilityDependencyConflict
    return frozenset(remaining)
