"""验证可信命令严格 JSON 边界、领域映射与跨进程稳定哈希。"""

import ast
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import get_args
from uuid import UUID

import pytest

from ai_employee.application.commands import (
    TrustedCommand,
    TrustedCommandValidationError,
    canonical_command_json,
    parse_trusted_command,
    trusted_command_hash,
)
from ai_employee.domain.calendar_actions import (
    CalendarCommand,
    CalendarCreateCommand,
    CalendarRestoreCommand,
    CalendarUpdateCommand,
    NotificationPolicy,
    calendar_client_event_id,
)
from ai_employee.domain.mail_actions import MailMode, MailSendCommand, ReplyThreadHeaders

OPERATION_ID = UUID("00000000-0000-0000-0000-000000000021")
CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000022")
DRAFT_ID = UUID("00000000-0000-0000-0000-000000000023")
SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000024")
BACKEND_SOURCE = Path(__file__).resolve().parents[3] / "src" / "ai_employee"
ADAPTER_BOUNDARY_DIRECTORIES = (
    BACKEND_SOURCE / "api",
    BACKEND_SOURCE / "agents",
    BACKEND_SOURCE / "application" / "ports",
    BACKEND_SOURCE / "infrastructure",
    BACKEND_SOURCE / "integrations",
    BACKEND_SOURCE / "workers",
)
COMMAND_TYPE_NAMES = frozenset(
    {
        "MailSendCommand",
        "CalendarCreateCommand",
        "CalendarUpdateCommand",
        "CalendarRestoreCommand",
    }
)
COMMAND_MODULE_NAMES = frozenset(
    {
        "ai_employee.application.commands",
        "ai_employee.domain.calendar_actions",
        "ai_employee.domain.mail_actions",
    }
)
_COMMAND_ANALYSIS_PATH_BUDGET = 256
_COMMAND_ANALYSIS_PATH_BUDGET_MESSAGE = "command type analysis path budget exceeded"


class _CommandPathBudgetExceeded(RuntimeError):
    """表示 AST 架构门禁无法在固定路径预算内保持精确分析。"""


@dataclass(frozen=True, slots=True)
class _AliasTypeExpression:
    """保存 alias 值、形参元数据及 default 的定义点解析上下文。"""

    expression: ast.expr
    parameter_names: tuple[str, ...] = ()
    parameter_kinds: tuple[str, ...] = ()
    parameter_defaults: tuple[ast.expr | None, ...] = ()
    parameter_default_states: tuple["_CommandTypeState | None", ...] = ()


@dataclass(frozen=True, slots=True)
class _TypeVariableDefinition:
    """保存 legacy/PEP 695 类型参数种类及 TypeVar/default 声明语义。"""

    bound: ast.expr | None = None
    constraints: tuple[ast.expr, ...] = ()
    default: ast.expr | None = None
    default_state: "_CommandTypeState | None" = None
    kind: str = "type_var"


@dataclass(frozen=True, slots=True)
class _ClassTypeDefinition:
    """保存一条可达路径完成后的 class 局部命名空间。

    命名空间冻结时不携带父指针；限定属性在最终模块路径上解析时再接回真实词法
    父级。这样既允许类内 alias 读取后置模块绑定，也不会让嵌套类裸名错误捕获
    外层 class 局部名称。
    """

    namespace: "_CommandTypeState"
    parameter_names: tuple[str, ...] = ()
    parameter_kinds: tuple[str, ...] = ()
    parameter_defaults: tuple[ast.expr | None, ...] = ()
    parameter_default_states: tuple["_CommandTypeState | None", ...] = ()
    member_type_expressions: tuple["_DeferredTypeExpression", ...] = ()
    base_type_expressions: tuple["_DeferredTypeExpression", ...] = ()


@dataclass(slots=True)
class _CommandTypeState:
    """保存一条静态可达路径上的命令类型符号状态。

    Module/Class 使用父链表达词法作用域；未知条件分支复制同一 scope 的本地状态
    后独立保留，不再按名称汇合，从而避免把互斥路径上的命令叶错误组合。
    """

    parent: "_CommandTypeState | None"
    is_class_scope: bool = False
    local_defined: set[str] = field(default_factory=set)
    module_aliases: dict[str, frozenset[str]] = field(default_factory=dict)
    typing_aliases: dict[str, frozenset[str]] = field(default_factory=dict)
    alias_expressions: dict[str, _AliasTypeExpression] = field(default_factory=dict)
    class_definitions: dict[str, _ClassTypeDefinition] = field(default_factory=dict)
    type_variable_definitions: dict[str, _TypeVariableDefinition] = field(
        default_factory=dict
    )
    direct_command_leaves: dict[str, frozenset[str]] = field(default_factory=dict)

    def reset_local_name(self, name: str) -> None:
        """登记本路径的一次定义，并清除该名称此前的所有静态含义。"""
        self.local_defined.add(name)
        self.module_aliases.pop(name, None)
        self.typing_aliases.pop(name, None)
        self.alias_expressions.pop(name, None)
        self.class_definitions.pop(name, None)
        self.type_variable_definitions.pop(name, None)
        self.direct_command_leaves.pop(name, None)

    def branch_copy(self) -> "_CommandTypeState":
        """复制当前 scope 状态，创建不会反向污染 sibling 的未知分支路径。"""
        return _CommandTypeState(
            parent=self.parent,
            is_class_scope=self.is_class_scope,
            local_defined=set(self.local_defined),
            module_aliases=dict(self.module_aliases),
            typing_aliases=dict(self.typing_aliases),
            alias_expressions=dict(self.alias_expressions),
            class_definitions=dict(self.class_definitions),
            type_variable_definitions=dict(self.type_variable_definitions),
            direct_command_leaves=dict(self.direct_command_leaves),
        )

    def reparented_copy(
        self,
        parent: "_CommandTypeState | None",
    ) -> "_CommandTypeState":
        """复制已完成 scope 的局部 frame，并显式连接指定的最终父路径状态。"""
        return _CommandTypeState(
            parent=parent,
            is_class_scope=self.is_class_scope,
            local_defined=set(self.local_defined),
            module_aliases=dict(self.module_aliases),
            typing_aliases=dict(self.typing_aliases),
            alias_expressions=dict(self.alias_expressions),
            class_definitions=dict(self.class_definitions),
            type_variable_definitions=dict(self.type_variable_definitions),
            direct_command_leaves=dict(self.direct_command_leaves),
        )

    def definition_snapshot(self) -> "_CommandTypeState":
        """递归复制当前词法父链，冻结类型表达式声明时可见的名称含义。

        快照只复制后续绑定会修改的 scope 容器；已冻结的 alias、TypeVar 与 class
        定义值可以安全共享。这样 default 在稍后展开时不会读取 alias owner 的完成态，
        也不会被同一 class 内声明点之后的同名绑定反向改写。
        """
        parent_snapshot = (
            self.parent.definition_snapshot() if self.parent is not None else None
        )
        return self.reparented_copy(parent_snapshot)


@dataclass(slots=True)
class _TypeCheckingVisibility:
    """跟踪静态条件与受限 TypeVar 工厂识别所需的最小名称含义。

    这里故意不复用完整命令类型符号表：分支选择只允许识别 ``typing``、``sys``
    与 ``TYPE_CHECKING``，旧式泛型只额外识别两种受信模块的 TypeVar、ParamSpec
    与 TypeVarTuple 工厂。其余绑定统一视为普通名称；子环境通过父链继承，
    unknown 分支则各自记录局部变化。
    """

    parent: "_TypeCheckingVisibility | None"
    local_bindings: dict[str, str] = field(default_factory=dict)

    def lookup(self, name: str) -> str | None:
        """返回当前静态可见含义；本地普通绑定会明确阻止父链命中。"""
        current: _TypeCheckingVisibility | None = self
        while current is not None:
            if name in current.local_bindings:
                return current.local_bindings[name]
            current = current.parent
        return None

    def bind(self, name: str, meaning: str) -> None:
        """在当前 Python scope 登记名称含义，覆盖此前同 scope 的导入别名。"""
        self.local_bindings[name] = meaning

    def branch_copy(self) -> "_TypeCheckingVisibility":
        """复制同一 scope 的条件可见性，供 unknown-if 的互斥路径独立演进。"""
        return _TypeCheckingVisibility(
            parent=self.parent,
            local_bindings=dict(self.local_bindings),
        )


@dataclass(frozen=True, slots=True)
class _DeferredTypeExpression:
    """保存延迟类型表达式、constraints 报告行及待接回父路径的局部 frame。"""

    expressions: tuple[ast.expr, ...]
    local_frames: tuple[_CommandTypeState, ...] = ()
    constraint_report_line: int | None = None
    skip_enclosing_class_frames: bool = False
    generic_base_report_line: int | None = None


@dataclass(slots=True)
class _AnalysisPath:
    """绑定一条可达路径上的命令状态与受限静态名称可见性。"""

    command_state: _CommandTypeState
    type_checking_visibility: _TypeCheckingVisibility
    pending_expressions: list[_DeferredTypeExpression] = field(default_factory=list)
    inheritable_type_expressions: list[_DeferredTypeExpression] = field(
        default_factory=list
    )

    def branch_copy(self) -> "_AnalysisPath":
        """复制 unknown-if 的一条互斥出路径，不共享可变的同 scope 绑定。"""
        return _AnalysisPath(
            command_state=self.command_state.branch_copy(),
            type_checking_visibility=self.type_checking_visibility.branch_copy(),
            pending_expressions=list(self.pending_expressions),
            inheritable_type_expressions=list(self.inheritable_type_expressions),
        )


@dataclass(frozen=True, slots=True)
class _TypeArgument:
    """保存 generic alias 实参及其调用点解析上下文；None 表示裸泛型 opaque 形参。"""

    expression: ast.expr | None
    state: _CommandTypeState
    substitutions: "_TypeSubstitutionFrame | None"


@dataclass(frozen=True, slots=True)
class _TypeSubstitutionFrame:
    """保存一次 generic alias 展开的局部形参到实参绑定。"""

    bindings: dict[str, _TypeArgument]


def _wide_command_union_annotation_lines(
    source: str,
    *,
    module_name: str | None = None,
) -> tuple[int, ...]:
    """返回类型上下文中直接重建两个以上具体可信命令联合的行号。

    检查函数参数/返回值、类基类、PEP 695 类型参数与 alias、受信 ``typing``/
    ``typing_extensions`` TypeVar、模块或类级传统 alias 与字段注解；普通运行时
    Call 和函数实现体不属于架构边界。导入符号会先解析为具体命令叶集合，本地
    generic alias 则在应用点按位置替换实参，因此前向 alias、字符串注解、模块
    属性、PEP 604 与 ``typing.Union`` 都遵守同一规则。官方 alias 保持不透明，
    单命令 Optional 或单命令加普通类型不计为宽集。

    Args:
        source: 待检查 Python 模块的 UTF-8 源码。
        module_name: 源码的完整模块名；解析相对导入时必填，省略时固定忽略相对导入。

    Returns:
        按升序去重后的违规联合起始行号。
    """
    tree = ast.parse(source, type_comments=True)
    root_state = _CommandTypeState(parent=None)
    violations: set[int] = set()
    trusted_typing_form_modules = frozenset({"typing", "typing_extensions"})
    typing_form_names = frozenset(
        {"Annotated", "Generic", "Literal", "Optional", "TypeAlias", "Union"}
    )

    def resolved_import_module(node: ast.ImportFrom) -> str | None:
        """按当前模块包上下文解析 ``from`` 导入的绝对模块名。

        ``ImportFrom.level`` 的一层表示当前包，两层表示当前包的父包。扫描器没有
        模块上下文时不能可靠猜测相对路径，因此固定返回 ``None``，避免把同名第三方
        模块误判为可信命令来源。
        """
        if node.level == 0:
            return node.module
        if module_name is None:
            return None

        package_parts = module_name.split(".")[:-1]
        kept_part_count = len(package_parts) - node.level + 1
        if kept_part_count <= 0:
            return None
        module_parts = node.module.split(".") if node.module is not None else []
        return ".".join((*package_parts[:kept_part_count], *module_parts))

    def binding_state(
        state: _CommandTypeState,
        name: str,
    ) -> _CommandTypeState | None:
        """返回名称最近的词法绑定状态；普通本地定义同样会中止父链查询。"""
        current: _CommandTypeState | None = state
        while current is not None:
            if name in current.local_defined:
                return current
            current = current.parent
        return None

    def lookup_command_leaves(
        state: _CommandTypeState,
        name: str,
        resolving: frozenset[tuple[int, str]] = frozenset(),
    ) -> frozenset[str]:
        """递归解析一个名称在当前路径可能代表的具体可信命令叶集合。

        TypeVar 的 bound/constraints 在其所属状态中解析，避免子类同名 shadow
        反向改变父声明。``resolving`` 只用于截断循环声明，不执行表达式，也不把
        可能叶集合本身视为源码显式 union；alias 应用由上层直接命令解析负责。
        """
        owner = binding_state(state, name)
        if owner is None:
            return frozenset({name}) if name in COMMAND_TYPE_NAMES else frozenset()

        leaves = owner.direct_command_leaves.get(name, frozenset())
        resolution_key = (id(owner), name)
        if resolution_key in resolving:
            return leaves
        type_variable = owner.type_variable_definitions.get(name)
        if type_variable is not None:
            next_resolving = resolving | {resolution_key}
            if type_variable.bound is not None:
                return leaves | direct_command_meaning(
                    type_variable.bound,
                    owner,
                    next_resolving,
                )
            return leaves | frozenset(
                leaf
                for constraint in type_variable.constraints
                for leaf in direct_command_meaning(
                    constraint,
                    owner,
                    next_resolving,
                )
            )

        return leaves

    def lookup_module_aliases(
        state: _CommandTypeState,
        name: str,
    ) -> frozenset[str]:
        """返回路径上所有可能的模块 import 展开；opaque 本地绑定返回空集合。"""
        owner = binding_state(state, name)
        if owner is None:
            return frozenset({name})
        return owner.module_aliases.get(name, frozenset())

    def lookup_typing_aliases(
        state: _CommandTypeState,
        name: str,
    ) -> frozenset[str]:
        """返回路径上可能的 typing 特殊形式，并尊重普通绑定 shadow。"""
        owner = binding_state(state, name)
        if owner is None:
            return frozenset({name}) if name in typing_form_names else frozenset()
        return owner.typing_aliases.get(name, frozenset())

    def dotted_name(node: ast.expr) -> str | None:
        """返回 Name/Attribute 的点分名称，其他表达式没有稳定符号名。"""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = dotted_name(node.value)
            return f"{prefix}.{node.attr}" if prefix is not None else None
        return None

    def expanded_names(
        node: ast.expr,
        state: _CommandTypeState,
    ) -> frozenset[str]:
        """按当前路径可能的 import alias 展开点分名称首段。"""
        dotted = dotted_name(node)
        if dotted is None:
            return frozenset()
        first, separator, remainder = dotted.partition(".")
        return frozenset(
            f"{expanded_first}.{remainder}" if separator else expanded_first
            for expanded_first in lookup_module_aliases(state, first)
        )

    def typing_forms(
        node: ast.expr,
        state: _CommandTypeState,
    ) -> frozenset[str]:
        """把裸名或两种受信 typing 模块属性解析为可能特殊形式集合。"""
        if isinstance(node, ast.Name):
            return lookup_typing_aliases(state, node.id)
        return frozenset(
            candidate
            for expanded in expanded_names(node, state)
            for trusted_module in trusted_typing_form_modules
            for prefix in (f"{trusted_module}.",)
            if expanded.startswith(prefix)
            for candidate in (expanded.removeprefix(prefix),)
            if candidate in typing_form_names
        )

    def command_leaves_for_node(
        node: ast.expr,
        state: _CommandTypeState,
        resolving: frozenset[tuple[int, str]],
    ) -> frozenset[str]:
        """在指定路径把名称或模块属性解析为可能的具体命令叶集合。"""
        if isinstance(node, ast.Name):
            return lookup_command_leaves(state, node.id, resolving)
        return frozenset(
            command_name
            for expanded in expanded_names(node, state)
            if "." in expanded
            for command_module, command_name in (expanded.rsplit(".", maxsplit=1),)
            if command_module in COMMAND_MODULE_NAMES
            and command_name in COMMAND_TYPE_NAMES
        )

    def is_union_node(node: ast.AST, state: _CommandTypeState) -> bool:
        """识别 PEP 604 或当前路径可能可见的 ``typing.Union[...]``。"""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return True
        return isinstance(node, ast.Subscript) and "Union" in typing_forms(
            node.value,
            state,
        )

    def is_type_alias_marker(node: ast.expr, state: _CommandTypeState) -> bool:
        """判断 AnnAssign 注解是否在当前路径可能是 TypeAlias。"""
        return "TypeAlias" in typing_forms(node, state)

    def looks_like_type_expression(node: ast.expr) -> bool:
        """传统无注解 alias 只接受不会执行函数调用的类型表达式形状。"""
        return isinstance(
            node,
            ast.Name | ast.Attribute | ast.Subscript | ast.BinOp | ast.Tuple,
        )

    def parsed_forward_expression(node: ast.expr) -> ast.expr | None:
        """安全解析字符串 forward ref，并把报告行平移回原源码位置。"""
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            return None
        try:
            parsed = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
        ast.increment_lineno(parsed, node.lineno - 1)
        return parsed

    def parsed_function_type_comment(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> tuple[ast.expr, ...]:
        """安全解析函数 type comment 的参数与返回类型，并对齐源码行号。"""
        if node.type_comment is None:
            return ()
        try:
            parsed = ast.parse(node.type_comment, mode="func_type")
        except SyntaxError:
            return ()
        assert isinstance(parsed, ast.FunctionType)
        ast.increment_lineno(parsed, node.lineno - 1)
        return (*parsed.argtypes, parsed.returns)

    def parsed_argument_type_comment(node: ast.arg) -> ast.expr | None:
        """把参数 comment 作为字符串类型表达式安全解析，并保留参数源码行号。"""
        if node.type_comment is None:
            return None
        comment_node = ast.copy_location(
            ast.Constant(value=node.type_comment),
            node,
        )
        return parsed_forward_expression(comment_node)

    def type_expression_children(
        node: ast.expr,
        state: _CommandTypeState,
    ) -> tuple[ast.expr, ...]:
        """返回真实类型位置子表达式；Literal metadata 与普通 Call 固定跳过。"""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return (node.left, node.right)
        if isinstance(node, ast.Subscript):
            form_names = typing_forms(node.value, state)
            if form_names == frozenset({"Literal"}):
                return ()
            if form_names == frozenset({"Annotated"}) and isinstance(
                node.slice,
                ast.Tuple,
            ):
                return tuple(node.slice.elts[:1])
            return (node.slice,)
        if isinstance(node, ast.Tuple | ast.List):
            return tuple(node.elts)
        if isinstance(node, ast.Starred):
            return (node.value,)
        parsed = parsed_forward_expression(node)
        return () if parsed is None else (parsed,)

    def inferred_legacy_type_parameter_names(
        expression: ast.expr,
        state: _CommandTypeState,
    ) -> tuple[str, ...]:
        """按 RHS 类型位置首次出现顺序返回可证明的 legacy TypeVar 名。

        仅当前词法路径中由受信 ``typing``/``typing_extensions`` 工厂建立的
        TypeVar 可以成为隐式形参；普通同名值、官方命令 alias、Literal 数据与
        Annotated metadata 都不会被猜测为泛型参数。
        """
        parameter_names: list[str] = []
        seen_names: set[str] = set()

        def collect(node: ast.expr) -> None:
            if isinstance(node, ast.Name):
                owner = binding_state(state, node.id)
                if (
                    owner is not None
                    and node.id in owner.type_variable_definitions
                    and node.id not in seen_names
                ):
                    seen_names.add(node.id)
                    parameter_names.append(node.id)
                return
            for child in type_expression_children(node, state):
                collect(child)

        collect(expression)
        return tuple(parameter_names)

    def alias_parameter_metadata(
        parameter_names: tuple[str, ...],
        state: _CommandTypeState,
    ) -> tuple[
        tuple[str, ...],
        tuple[ast.expr | None, ...],
        tuple[_CommandTypeState | None, ...],
    ]:
        """冻结 alias 形参的 kind、default 及其定义点解析上下文。"""
        definitions: list[_TypeVariableDefinition] = []
        for parameter_name in parameter_names:
            owner = binding_state(state, parameter_name)
            definition = (
                owner.type_variable_definitions.get(parameter_name)
                if owner is not None
                else None
            )
            definitions.append(definition or _TypeVariableDefinition())
        return (
            tuple(definition.kind for definition in definitions),
            tuple(definition.default for definition in definitions),
            tuple(definition.default_state for definition in definitions),
        )

    def union_member_expressions(
        node: ast.expr,
        state: _CommandTypeState,
    ) -> tuple[ast.expr, ...]:
        """返回真实 Union/PEP 604 的直接成员；普通容器固定返回空集合。"""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return (node.left, node.right)
        if not (
            isinstance(node, ast.Subscript)
            and "Union" in typing_forms(node.value, state)
        ):
            return ()
        if isinstance(node.slice, ast.Tuple):
            return tuple(node.slice.elts)
        return (node.slice,)

    def alias_application(
        node: ast.expr,
        state: _CommandTypeState,
        substitutions: _TypeSubstitutionFrame | None,
    ) -> tuple[
        _CommandTypeState,
        str,
        _AliasTypeExpression,
        _TypeSubstitutionFrame,
        tuple[ast.expr, ...],
    ] | None:
        """解析裸名或 class 限定 alias 应用，并捕获实参调用点上下文。"""
        arguments: tuple[ast.expr, ...]
        alias_node: ast.expr
        if isinstance(node, ast.Name | ast.Attribute):
            alias_node = node
            arguments = ()
            parameterized = False
        elif isinstance(node, ast.Subscript) and isinstance(
            node.value,
            ast.Name | ast.Attribute,
        ):
            alias_node = node.value
            arguments = (
                tuple(node.slice.elts)
                if isinstance(node.slice, ast.Tuple)
                else (node.slice,)
            )
            parameterized = True
        else:
            return None

        if isinstance(alias_node, ast.Name):
            alias_name = alias_node.id
            owner = binding_state(state, alias_name)
        else:
            alias_name = alias_node.attr
            owner = resolved_class_namespace(alias_node.value, state)
        if owner is None:
            return None
        alias_expression = owner.alias_expressions.get(alias_name)
        if alias_expression is None:
            return None

        parameter_names = alias_expression.parameter_names
        parameter_kinds = alias_expression.parameter_kinds or tuple(
            "type_var" for _ in parameter_names
        )
        parameter_defaults = alias_expression.parameter_defaults or tuple(
            None for _ in parameter_names
        )
        parameter_default_states = alias_expression.parameter_default_states or tuple(
            None for _ in parameter_names
        )
        if parameterized:
            variadic_indexes = tuple(
                index
                for index, kind in enumerate(parameter_kinds)
                if kind in {"param_spec", "type_var_tuple"}
            )
            if len(variadic_indexes) > 1:
                return None
            if not variadic_indexes:
                if len(arguments) > len(parameter_names):
                    return None
                missing_defaults = parameter_defaults[len(arguments) :]
                if any(default is None for default in missing_defaults):
                    return None
                bound_arguments = arguments + tuple(
                    default
                    for default in missing_defaults
                    if default is not None
                )
                # 显式实参保持调用点解析；仅缺省尾参数切换到各自 TypeVar
                # 声明点快照。PEP 695 暂无独立快照时沿用 alias owner 语义。
                bound_argument_states = (state,) * len(arguments) + tuple(
                    parameter_default_states[index] or owner
                    for index in range(len(arguments), len(parameter_names))
                )
            else:
                variadic_index = variadic_indexes[0]
                leading_count = variadic_index
                if len(arguments) < leading_count:
                    return None
                leading = arguments[:leading_count]
                trailing_count = len(parameter_names) - variadic_index - 1
                remaining_arguments = arguments[leading_count:]
                if parameter_kinds[variadic_index] == "param_spec":
                    # ParamSpec 在 alias 应用中占一个参数列表槽，例如 ``[[int]]``
                    # 或 ``...``；其后的显式实参依次覆盖固定尾参数。不能像
                    # TypeVarTuple 一样从末尾预留全部固定参数，否则唯一的参数列表
                    # 会被误绑到尾 TypeVar，进而跳过其 default。
                    if not remaining_arguments:
                        variadic_default = parameter_defaults[variadic_index]
                        if variadic_default is None:
                            return None
                        variadic_expression = variadic_default
                        variadic_state = parameter_default_states[variadic_index] or owner
                        explicit_trailing = ()
                    else:
                        variadic_expression = remaining_arguments[0]
                        variadic_state = state
                        explicit_trailing = remaining_arguments[1:]
                    if len(explicit_trailing) > trailing_count:
                        return None
                else:
                    # TypeVarTuple 可为空并吸收任意数量的中间实参。固定尾参数优先
                    # 从调用点获得位置实参；只有实际缺省的连续尾部才读取 default。
                    explicit_trailing_count = min(
                        len(remaining_arguments),
                        trailing_count,
                    )
                    variadic_end = len(remaining_arguments) - explicit_trailing_count
                    variadic_arguments = remaining_arguments[:variadic_end]
                    explicit_trailing = remaining_arguments[variadic_end:]
                    if len(variadic_arguments) == 1:
                        variadic_expression = variadic_arguments[0]
                    else:
                        variadic_expression = ast.copy_location(
                            ast.Tuple(
                                elts=list(variadic_arguments),
                                ctx=ast.Load(),
                            ),
                            node,
                        )
                    variadic_state = state

                missing_trailing_start = variadic_index + 1 + len(explicit_trailing)
                missing_trailing_defaults = parameter_defaults[missing_trailing_start:]
                if any(default is None for default in missing_trailing_defaults):
                    return None
                default_trailing = tuple(
                    default for default in missing_trailing_defaults if default is not None
                )
                bound_arguments = (
                    *leading,
                    variadic_expression,
                    *explicit_trailing,
                    *default_trailing,
                )
                bound_argument_states = (
                    *((state,) * len(leading)),
                    variadic_state,
                    *((state,) * len(explicit_trailing)),
                    *(
                        parameter_default_states[index] or owner
                        for index in range(
                            missing_trailing_start,
                            len(parameter_names),
                        )
                    ),
                )
        if not parameterized and not parameter_names:
            bindings: dict[str, _TypeArgument] = {}
        elif not parameterized:
            bindings = {
                parameter_name: _TypeArgument(
                    expression=parameter_default,
                    state=parameter_default_state or owner,
                    substitutions=substitutions,
                )
                for (
                    parameter_name,
                    parameter_default,
                    parameter_default_state,
                ) in zip(
                    parameter_names,
                    parameter_defaults,
                    parameter_default_states,
                    strict=True,
                )
            }
        else:
            bindings = {
                parameter_name: _TypeArgument(
                    expression=argument,
                    state=argument_state,
                    substitutions=substitutions,
                )
                for parameter_name, argument, argument_state in zip(
                    parameter_names,
                    bound_arguments,
                    bound_argument_states,
                    strict=True,
                )
            }
        return (
            owner,
            alias_name,
            alias_expression,
            _TypeSubstitutionFrame(bindings=bindings),
            arguments,
        )

    def direct_command_meaning(
        node: ast.expr,
        state: _CommandTypeState,
        resolving: frozenset[tuple[int, str]] = frozenset(),
        substitutions: _TypeSubstitutionFrame | None = None,
    ) -> frozenset[str]:
        """解析表达式的直接命令含义，不把普通容器内部类型提升到外层。

        Name/Attribute、真实 Union、Optional 唯一类型、Annotated 首类型、
        forward string 与已登记 alias/TypeVar 可以贡献命令；tuple、list、Callable
        和其他普通 Subscript 只作为容器，内部参数不会成为该容器表达式的直接
        命令含义。
        """
        if isinstance(node, ast.Name) and substitutions is not None:
            argument = substitutions.bindings.get(node.id)
            if argument is not None:
                if argument.expression is None:
                    return frozenset()
                return direct_command_meaning(
                    argument.expression,
                    argument.state,
                    resolving,
                    argument.substitutions,
                )

        resolved_alias = alias_application(node, state, substitutions)
        if resolved_alias is not None:
            owner, alias_name, alias_expression, alias_substitutions, _ = (
                resolved_alias
            )
            resolution_key = (id(alias_expression), alias_name)
            if resolution_key in resolving:
                return frozenset()
            return direct_command_meaning(
                alias_expression.expression,
                owner,
                resolving | {resolution_key},
                alias_substitutions,
            )

        if isinstance(node, ast.Name | ast.Attribute):
            return command_leaves_for_node(node, state, resolving)

        union_members = union_member_expressions(node, state)
        if union_members:
            return frozenset(
                leaf
                for member in union_members
                for leaf in direct_command_meaning(
                    member,
                    state,
                    resolving,
                    substitutions,
                )
            )

        if isinstance(node, ast.Subscript) and "Annotated" in typing_forms(
            node.value,
            state,
        ):
            first_type = (
                node.slice.elts[0]
                if isinstance(node.slice, ast.Tuple) and node.slice.elts
                else node.slice
            )
            return direct_command_meaning(
                first_type,
                state,
                resolving,
                substitutions,
            )

        if isinstance(node, ast.Subscript) and "Optional" in typing_forms(
            node.value,
            state,
        ):
            return direct_command_meaning(
                node.slice,
                state,
                resolving,
                substitutions,
            )

        parsed = parsed_forward_expression(node)
        if parsed is not None:
            return direct_command_meaning(
                parsed,
                state,
                resolving,
                substitutions,
            )

        return frozenset()

    def wide_union_report_lines(
        node: ast.expr,
        state: _CommandTypeState,
        resolving: frozenset[tuple[int, str]] = frozenset(),
        substitutions: _TypeSubstitutionFrame | None = None,
        alias_report_line: int | None = None,
    ) -> frozenset[int]:
        """遍历真实类型位置与 alias 展开，返回每个真实宽命令 union 的报告行。"""
        if isinstance(node, ast.Name) and substitutions is not None:
            argument = substitutions.bindings.get(node.id)
            if argument is not None:
                if argument.expression is None:
                    return frozenset()
                return wide_union_report_lines(
                    argument.expression,
                    argument.state,
                    resolving,
                    argument.substitutions,
                    alias_report_line,
                )

        resolved_alias = alias_application(node, state, substitutions)
        if resolved_alias is not None:
            owner, alias_name, alias_expression, alias_substitutions, arguments = (
                resolved_alias
            )
            resolution_key = (id(alias_expression), alias_name)
            lines = {
                line
                for argument in arguments
                for line in wide_union_report_lines(
                    argument,
                    state,
                    resolving,
                    substitutions,
                    alias_report_line,
                )
            }
            if resolution_key in resolving:
                return frozenset(lines)
            report_line = alias_report_line or node.lineno
            lines.update(
                wide_union_report_lines(
                    alias_expression.expression,
                    owner,
                    resolving | {resolution_key},
                    alias_substitutions,
                    report_line,
                )
            )
            return frozenset(lines)

        lines: set[int] = set()
        if is_union_node(node, state) and len(
            direct_command_meaning(node, state, resolving, substitutions)
        ) >= 2:
            lines.add(alias_report_line or node.lineno)
        for child in type_expression_children(node, state):
            lines.update(
                wide_union_report_lines(
                    child,
                    state,
                    resolving,
                    substitutions,
                    alias_report_line,
                )
            )
        return frozenset(lines)

    def bound_names(target: ast.expr) -> tuple[str, ...]:
        """返回赋值目标会在当前 scope 绑定的简单名称。"""
        if isinstance(target, ast.Name):
            return (target.id,)
        if isinstance(target, ast.Tuple | ast.List):
            return tuple(name for element in target.elts for name in bound_names(element))
        if isinstance(target, ast.Starred):
            return bound_names(target.value)
        return ()

    type_checking_binding = "type_checking"
    typing_module_binding = "typing_module"
    typing_extensions_module_binding = "typing_extensions_module"
    type_var_binding = "type_var"
    param_spec_binding = "param_spec"
    type_var_tuple_binding = "type_var_tuple"
    legacy_type_parameter_bindings = {
        "ParamSpec": param_spec_binding,
        "TypeVar": type_var_binding,
        "TypeVarTuple": type_var_tuple_binding,
    }
    sys_module_binding = "sys_module"
    ordinary_binding = "other"

    def static_sys_attribute_value(
        expression: ast.expr,
        visibility: _TypeCheckingVisibility,
    ) -> tuple[object, ...] | str | None:
        """读取受导入与 shadow 约束的稳定 sys 属性，不解析任意 Attribute。"""
        if not (
            isinstance(expression, ast.Attribute)
            and isinstance(expression.value, ast.Name)
            and visibility.lookup(expression.value.id) == sys_module_binding
        ):
            return None
        if expression.attr == "version_info":
            return tuple(sys.version_info)
        if expression.attr == "platform":
            return sys.platform
        return None

    def static_comparison_literal(
        expression: ast.expr,
    ) -> tuple[int, ...] | str | None:
        """只接受版本整数 tuple 或平台字符串，拒绝名称、调用与可变容器。"""
        if isinstance(expression, ast.Tuple) and all(
            isinstance(element, ast.Constant)
            and isinstance(element.value, int)
            and not isinstance(element.value, bool)
            for element in expression.elts
        ):
            return tuple(element.value for element in expression.elts)
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            return expression.value
        return None

    def apply_static_comparison(
        left: tuple[object, ...] | tuple[int, ...] | str,
        operator: ast.cmpop,
        right: tuple[object, ...] | tuple[int, ...] | str,
    ) -> bool | None:
        """显式执行六种无副作用比较；类型不可比较时固定退回 unknown。"""
        try:
            if isinstance(operator, ast.Lt):
                return left < right
            if isinstance(operator, ast.LtE):
                return left <= right
            if isinstance(operator, ast.Eq):
                return left == right
            if isinstance(operator, ast.NotEq):
                return left != right
            if isinstance(operator, ast.GtE):
                return left >= right
            if isinstance(operator, ast.Gt):
                return left > right
        except TypeError:
            return None
        return None

    def static_sys_comparison_truth(
        expression: ast.expr,
        visibility: _TypeCheckingVisibility,
    ) -> bool | None:
        """解析单个 sys.version_info/sys.platform 与受限字面量的比较。"""
        if not (
            isinstance(expression, ast.Compare)
            and len(expression.ops) == 1
            and len(expression.comparators) == 1
        ):
            return None

        comparator = expression.comparators[0]
        left_sys = static_sys_attribute_value(expression.left, visibility)
        right_sys = static_sys_attribute_value(comparator, visibility)
        left_literal = static_comparison_literal(expression.left)
        right_literal = static_comparison_literal(comparator)
        operator = expression.ops[0]
        if left_sys is not None and right_literal is not None:
            return apply_static_comparison(left_sys, operator, right_literal)
        if left_literal is not None and right_sys is not None:
            return apply_static_comparison(left_literal, operator, right_sys)
        return None

    def static_condition_truth(
        expression: ast.expr,
        visibility: _TypeCheckingVisibility,
    ) -> bool | None:
        """只解析受支持的 TYPE_CHECKING 与稳定 sys 条件，不执行任意 AST。

        裸名必须来自 ``from typing import TYPE_CHECKING``，属性形式的首段必须来自
        ``import typing``。sys 条件必须由未被 shadow 的模块 import、受限属性和
        字面量组成。一层 ``not`` 仅反转已知结果；其余表达式固定返回未知。
        """
        if isinstance(expression, ast.Name):
            if visibility.lookup(expression.id) == type_checking_binding:
                return True
            return None
        if (
            isinstance(expression, ast.Attribute)
            and expression.attr == "TYPE_CHECKING"
            and isinstance(expression.value, ast.Name)
            and visibility.lookup(expression.value.id) == typing_module_binding
        ):
            return True
        if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
            operand_truth = static_condition_truth(expression.operand, visibility)
            return None if operand_truth is None else not operand_truth
        return static_sys_comparison_truth(expression, visibility)

    def record_type_expression(
        expression: ast.expr,
        path: _AnalysisPath,
        *,
        local_frames: tuple[_CommandTypeState, ...] = (),
        inheritable_member: bool = False,
        generic_base_report_line: int | None = None,
    ) -> None:
        """把真实表达式挂到当前路径，延迟到所属 scope 完成后解析。"""
        deferred = _DeferredTypeExpression(
            expressions=(expression,),
            local_frames=local_frames,
            generic_base_report_line=generic_base_report_line,
        )
        path.pending_expressions.append(deferred)
        if inheritable_member:
            path.inheritable_type_expressions.append(deferred)

    def record_constraint_group(
        expressions: tuple[ast.expr, ...],
        path: _AnalysisPath,
        *,
        report_line: int,
        local_frames: tuple[_CommandTypeState, ...] = (),
        inheritable_member: bool = False,
    ) -> None:
        """延迟检查一组顶层 constraints 的直接命令含义。"""
        if not expressions:
            return
        deferred = _DeferredTypeExpression(
            expressions=expressions,
            local_frames=local_frames,
            constraint_report_line=report_line,
        )
        path.pending_expressions.append(deferred)
        if inheritable_member:
            path.inheritable_type_expressions.append(deferred)

    def type_parameter_names(
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.TypeAlias,
    ) -> tuple[str, ...]:
        """返回 PEP 695 声明引入的局部类型参数名，供命令名称 shadow 使用。"""
        return tuple(
            type_parameter.name
            for type_parameter in node.type_params
            if isinstance(
                type_parameter,
                ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple,
            )
        )

    def type_parameter_frame(
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.TypeAlias,
    ) -> _CommandTypeState | None:
        """创建 PEP 695 类型参数 frame，并保存种类、default 与 TypeVar 约束。"""
        names = type_parameter_names(node)
        if not names:
            return None
        frame = _CommandTypeState(parent=None)
        for type_parameter in node.type_params:
            if not isinstance(
                type_parameter,
                ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple,
            ):
                continue
            frame.reset_local_name(type_parameter.name)
            default = getattr(type_parameter, "default_value", None)
            if isinstance(type_parameter, ast.ParamSpec):
                definition = _TypeVariableDefinition(
                    default=default,
                    kind="param_spec",
                )
            elif isinstance(type_parameter, ast.TypeVarTuple):
                definition = _TypeVariableDefinition(
                    default=default,
                    kind="type_var_tuple",
                )
            elif type_parameter.bound is None:
                definition = _TypeVariableDefinition(default=default)
            elif isinstance(type_parameter.bound, ast.Tuple):
                definition = _TypeVariableDefinition(
                    constraints=tuple(type_parameter.bound.elts),
                    default=default,
                )
            else:
                definition = _TypeVariableDefinition(
                    bound=type_parameter.bound,
                    default=default,
                )
            frame.type_variable_definitions[type_parameter.name] = definition
        return frame

    def record_type_parameter_bounds(
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.TypeAlias,
        path: _AnalysisPath,
        frame: _CommandTypeState | None,
        *,
        inheritable_member: bool = False,
    ) -> None:
        """记录 PEP 695 bound 内真实 union 或顶层 constraints 聚合。"""
        local_frames = () if frame is None else (frame,)
        for type_parameter in node.type_params:
            if not isinstance(type_parameter, ast.TypeVar):
                continue
            if type_parameter.bound is None:
                continue
            if isinstance(type_parameter.bound, ast.Tuple):
                constraints = tuple(type_parameter.bound.elts)
                if constraints:
                    record_constraint_group(
                        constraints,
                        path,
                        report_line=constraints[0].lineno,
                        local_frames=local_frames,
                        inheritable_member=inheritable_member,
                    )
                continue
            record_type_expression(
                type_parameter.bound,
                path,
                local_frames=local_frames,
                inheritable_member=inheritable_member,
            )

    def legacy_type_parameter_kind(
        expression: ast.expr,
        visibility: _TypeCheckingVisibility,
    ) -> str | None:
        """返回受信 legacy 类型参数工厂种类；普通 Call 固定返回 None。"""
        if not isinstance(expression, ast.Call):
            return None
        if isinstance(expression.func, ast.Name):
            meaning = visibility.lookup(expression.func.id)
            return (
                meaning
                if meaning
                in {type_var_binding, param_spec_binding, type_var_tuple_binding}
                else None
            )
        if not (
            isinstance(expression.func, ast.Attribute)
            and isinstance(expression.func.value, ast.Name)
            and visibility.lookup(expression.func.value.id)
            in {typing_module_binding, typing_extensions_module_binding}
        ):
            return None
        return legacy_type_parameter_bindings.get(expression.func.attr)

    def record_legacy_type_var_call(
        expression: ast.expr,
        path: _AnalysisPath,
    ) -> _TypeVariableDefinition | None:
        """记录受信 legacy 类型参数声明，并返回可绑定到赋值名称的定义。"""
        kind = legacy_type_parameter_kind(
            expression,
            path.type_checking_visibility,
        )
        if kind is None:
            return None
        assert isinstance(expression, ast.Call)
        bound: ast.expr | None = None
        default: ast.expr | None = None
        for keyword in expression.keywords:
            if keyword.arg == "bound":
                bound = keyword.value
                record_type_expression(keyword.value, path)
            elif keyword.arg == "default":
                default = keyword.value
                record_type_expression(keyword.value, path)
        constraints = (
            tuple(expression.args[1:]) if kind == type_var_binding else ()
        )
        if constraints:
            record_constraint_group(
                constraints,
                path,
                report_line=constraints[0].lineno,
            )
        return _TypeVariableDefinition(
            bound=bound,
            constraints=constraints,
            default=default,
            default_state=(
                path.command_state.definition_snapshot()
                if default is not None
                else None
            ),
            kind=kind,
        )

    def collect_function_signature(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        path: _AnalysisPath,
    ) -> None:
        """记录函数类型参数、注解与 type comment；实现体不属于类型边界。"""
        parameter_frame = type_parameter_frame(node)
        local_frames = () if parameter_frame is None else (parameter_frame,)
        inheritable_member = path.command_state.is_class_scope
        record_type_parameter_bounds(
            node,
            path,
            parameter_frame,
            inheritable_member=inheritable_member,
        )
        arguments = (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
            *((node.args.vararg,) if node.args.vararg is not None else ()),
            *((node.args.kwarg,) if node.args.kwarg is not None else ()),
        )
        for argument in arguments:
            if argument.annotation is not None:
                record_type_expression(
                    argument.annotation,
                    path,
                    local_frames=local_frames,
                    inheritable_member=inheritable_member,
                )
            comment_expression = parsed_argument_type_comment(argument)
            if comment_expression is not None:
                record_type_expression(
                    comment_expression,
                    path,
                    local_frames=local_frames,
                    inheritable_member=inheritable_member,
                )
        if node.returns is not None:
            record_type_expression(
                node.returns,
                path,
                local_frames=local_frames,
                inheritable_member=inheritable_member,
            )
        for expression in parsed_function_type_comment(node):
            record_type_expression(
                expression,
                path,
                local_frames=local_frames,
                inheritable_member=inheritable_member,
            )

    def bind_command_definition(node: ast.stmt, path: _AnalysisPath) -> None:
        """把一个可见定义应用到当前控制流路径的命令类型状态。"""
        state = path.command_state
        if isinstance(node, ast.Import):
            for imported in node.names:
                bound_name = imported.asname or imported.name.split(".", maxsplit=1)[0]
                state.reset_local_name(bound_name)
                state.module_aliases[bound_name] = frozenset(
                    {imported.name if imported.asname else bound_name}
                )
            return

        if isinstance(node, ast.ImportFrom):
            imported_module = resolved_import_module(node)
            if imported_module is None:
                return
            for imported in node.names:
                if imported.name == "*":
                    continue
                bound_name = imported.asname or imported.name
                imported_path = f"{imported_module}.{imported.name}"
                state.reset_local_name(bound_name)
                if (
                    imported_module in COMMAND_MODULE_NAMES
                    and imported.name in COMMAND_TYPE_NAMES
                ):
                    state.direct_command_leaves[bound_name] = frozenset(
                        {imported.name}
                    )
                elif imported_path in COMMAND_MODULE_NAMES:
                    state.module_aliases[bound_name] = frozenset({imported_path})
                elif (
                    imported_module in trusted_typing_form_modules
                    and imported.name in typing_form_names
                ):
                    state.typing_aliases[bound_name] = frozenset({imported.name})
            return

        if isinstance(node, ast.TypeAlias):
            if isinstance(node.name, ast.Name):
                parameter_frame = type_parameter_frame(node)
                parameter_names = type_parameter_names(node)
                (
                    parameter_kinds,
                    parameter_defaults,
                    parameter_default_states,
                ) = alias_parameter_metadata(parameter_names, parameter_frame or state)
                local_frames = (
                    () if parameter_frame is None else (parameter_frame,)
                )
                record_type_parameter_bounds(node, path, parameter_frame)
                state.reset_local_name(node.name.id)
                # 泛型 alias 仍须保留固定命令值的展开能力；有参应用按这里保存的
                # 有序形参替换调用点实参，裸应用则把形参视为 opaque，避免外层
                # 同名命令穿透合法 shadow。
                state.alias_expressions[node.name.id] = _AliasTypeExpression(
                    expression=node.value,
                    parameter_names=parameter_names,
                    parameter_kinds=parameter_kinds,
                    parameter_defaults=parameter_defaults,
                    parameter_default_states=parameter_default_states,
                )
                record_type_expression(
                    node.value,
                    path,
                    local_frames=local_frames,
                )
            return

        if isinstance(node, ast.Assign):
            type_variable_definition = record_legacy_type_var_call(node.value, path)
            names = tuple(name for target in node.targets for name in bound_names(target))
            is_alias_expression = looks_like_type_expression(node.value)
            for name in names:
                state.reset_local_name(name)
            if type_variable_definition is not None:
                for name in names:
                    state.type_variable_definitions[name] = type_variable_definition
            elif is_alias_expression:
                parameter_names = inferred_legacy_type_parameter_names(
                    node.value,
                    state,
                )
                (
                    parameter_kinds,
                    parameter_defaults,
                    parameter_default_states,
                ) = alias_parameter_metadata(parameter_names, state)
                for name in names:
                    state.alias_expressions[name] = _AliasTypeExpression(
                        expression=node.value,
                        parameter_names=parameter_names,
                        parameter_kinds=parameter_kinds,
                        parameter_defaults=parameter_defaults,
                        parameter_default_states=parameter_default_states,
                    )
            if names and type_variable_definition is None and is_alias_expression:
                record_type_expression(node.value, path)
            return

        if isinstance(node, ast.AnnAssign):
            is_alias = node.value is not None and is_type_alias_marker(
                node.annotation,
                state,
            )
            record_type_expression(
                node.annotation,
                path,
                inheritable_member=state.is_class_scope,
            )
            if isinstance(node.target, ast.Name):
                state.reset_local_name(node.target.id)
                if is_alias and node.value is not None:
                    parameter_names = inferred_legacy_type_parameter_names(
                        node.value,
                        state,
                    )
                    (
                        parameter_kinds,
                        parameter_defaults,
                        parameter_default_states,
                    ) = alias_parameter_metadata(parameter_names, state)
                    state.alias_expressions[node.target.id] = _AliasTypeExpression(
                        expression=node.value,
                        parameter_names=parameter_names,
                        parameter_kinds=parameter_kinds,
                        parameter_defaults=parameter_defaults,
                        parameter_default_states=parameter_default_states,
                    )
                    record_type_expression(node.value, path)
            return

        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            state.reset_local_name(node.name)

    def bind_visibility_names(
        node: ast.stmt,
        visibility: _TypeCheckingVisibility,
    ) -> None:
        """按执行顺序更新静态条件/TypeVar 识别环境，并尊重普通定义 shadow。"""
        if isinstance(node, ast.Import):
            for imported in node.names:
                bound_name = imported.asname or imported.name.split(".", maxsplit=1)[0]
                if imported.name == "typing":
                    meaning = typing_module_binding
                elif imported.name == "typing_extensions":
                    meaning = typing_extensions_module_binding
                elif imported.name == "sys":
                    meaning = sys_module_binding
                else:
                    meaning = ordinary_binding
                visibility.bind(bound_name, meaning)
            return
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name == "*":
                    continue
                bound_name = imported.asname or imported.name
                if node.level == 0 and node.module in {"typing", "typing_extensions"}:
                    if imported.name == "TYPE_CHECKING":
                        meaning = (
                            type_checking_binding
                            if node.module == "typing"
                            else ordinary_binding
                        )
                    elif imported.name in legacy_type_parameter_bindings:
                        meaning = legacy_type_parameter_bindings[imported.name]
                    else:
                        meaning = ordinary_binding
                else:
                    meaning = ordinary_binding
                visibility.bind(bound_name, meaning)
            return
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name in bound_names(target):
                    visibility.bind(name, ordinary_binding)
            return
        if isinstance(node, ast.AnnAssign):
            for name in bound_names(node.target):
                visibility.bind(name, ordinary_binding)
            return
        if isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
            visibility.bind(node.name.id, ordinary_binding)
            return
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            visibility.bind(node.name, ordinary_binding)

    def command_state_signature(state: _CommandTypeState) -> tuple[object, ...]:
        """返回可比较的结构签名；只合并后续解析完全等价的命令状态。"""
        return (
            command_state_signature(state.parent) if state.parent is not None else None,
            state.is_class_scope,
            tuple(sorted(state.local_defined)),
            tuple(
                sorted(
                    (name, tuple(sorted(values)))
                    for name, values in state.module_aliases.items()
                )
            ),
            tuple(
                sorted(
                    (name, tuple(sorted(values)))
                    for name, values in state.typing_aliases.items()
                )
            ),
            tuple(
                sorted(
                    (
                        name,
                        ast.dump(alias_expression.expression, include_attributes=False),
                        alias_expression.parameter_names,
                        alias_expression.parameter_kinds,
                        tuple(
                            ast.dump(default, include_attributes=False)
                            if default is not None
                            else None
                            for default in alias_expression.parameter_defaults
                        ),
                        tuple(
                            command_state_signature(default_state)
                            if default_state is not None
                            else None
                            for default_state in alias_expression.parameter_default_states
                        ),
                    )
                    for name, alias_expression in state.alias_expressions.items()
                )
            ),
            tuple(
                sorted(
                    (
                        name,
                        command_state_signature(class_definition.namespace),
                        class_definition.parameter_names,
                        class_definition.parameter_kinds,
                        tuple(
                            ast.dump(default, include_attributes=False)
                            if default is not None
                            else None
                            for default in class_definition.parameter_defaults
                        ),
                        tuple(
                            command_state_signature(default_state)
                            if default_state is not None
                            else None
                            for default_state in class_definition.parameter_default_states
                        ),
                        tuple(
                            deferred_expression_signature(deferred)
                            for deferred in class_definition.member_type_expressions
                        ),
                        tuple(
                            deferred_expression_signature(deferred)
                            for deferred in class_definition.base_type_expressions
                        ),
                    )
                    for name, class_definition in state.class_definitions.items()
                )
            ),
            tuple(
                sorted(
                    (
                        name,
                        (
                            ast.dump(definition.bound, include_attributes=False)
                            if definition.bound is not None
                            else None
                        ),
                        tuple(
                            ast.dump(constraint, include_attributes=False)
                            for constraint in definition.constraints
                        ),
                        (
                            ast.dump(definition.default, include_attributes=False)
                            if definition.default is not None
                            else None
                        ),
                        (
                            command_state_signature(definition.default_state)
                            if definition.default_state is not None
                            else None
                        ),
                        definition.kind,
                    )
                    for name, definition in state.type_variable_definitions.items()
                )
            ),
            tuple(
                sorted(
                    (name, tuple(sorted(values)))
                    for name, values in state.direct_command_leaves.items()
                )
            ),
        )

    def visibility_signature(
        visibility: _TypeCheckingVisibility,
    ) -> tuple[object, ...]:
        """返回受限静态名称可见性的结构签名，并包含完整父 scope。"""
        return (
            visibility_signature(visibility.parent)
            if visibility.parent is not None
            else None,
            tuple(sorted(visibility.local_bindings.items())),
        )

    def deferred_expression_signature(
        deferred: _DeferredTypeExpression,
    ) -> tuple[object, ...]:
        """区分源码表达式身份及其完整局部 frame 链，避免错误合并 sibling。"""
        return (
            tuple(id(expression) for expression in deferred.expressions),
            tuple(command_state_signature(frame) for frame in deferred.local_frames),
            deferred.constraint_report_line,
            deferred.skip_enclosing_class_frames,
            deferred.generic_base_report_line,
        )

    def deduplicate_paths(paths: tuple[_AnalysisPath, ...]) -> tuple[_AnalysisPath, ...]:
        """去重结构等价路径，保留名称间相关性同时限制无意义的路径增长。"""
        unique_paths: dict[tuple[object, ...], _AnalysisPath] = {}
        for path in paths:
            signature = (
                command_state_signature(path.command_state),
                visibility_signature(path.type_checking_visibility),
            )
            existing_path = unique_paths.get(signature)
            if existing_path is None:
                unique_paths[signature] = path
                continue

            known_expression_signatures = {
                deferred_expression_signature(deferred)
                for deferred in existing_path.pending_expressions
            }
            for deferred in path.pending_expressions:
                deferred_signature = deferred_expression_signature(deferred)
                if deferred_signature in known_expression_signatures:
                    continue
                existing_path.pending_expressions.append(deferred)
                known_expression_signatures.add(deferred_signature)

            known_member_signatures = {
                deferred_expression_signature(deferred)
                for deferred in existing_path.inheritable_type_expressions
            }
            for deferred in path.inheritable_type_expressions:
                deferred_signature = deferred_expression_signature(deferred)
                if deferred_signature in known_member_signatures:
                    continue
                existing_path.inheritable_type_expressions.append(deferred)
                known_member_signatures.add(deferred_signature)

        deduplicated = tuple(unique_paths.values())
        if len(deduplicated) > _COMMAND_ANALYSIS_PATH_BUDGET:
            raise _CommandPathBudgetExceeded(_COMMAND_ANALYSIS_PATH_BUDGET_MESSAGE)
        return deduplicated

    def evaluate_completed_paths(paths: tuple[_AnalysisPath, ...]) -> None:
        """在 scope 完成态逐路径解析待检查表达式，保留前向绑定与相关性。"""
        for path in paths:
            for deferred in path.pending_expressions:
                state = path.command_state
                for local_frame in deferred.local_frames:
                    state = local_frame.reparented_copy(state)
                if deferred.constraint_report_line is not None:
                    command_leaves = frozenset(
                        leaf
                        for expression in deferred.expressions
                        for leaf in direct_command_meaning(expression, state)
                    )
                    if len(command_leaves) >= 2:
                        violations.add(deferred.constraint_report_line)
                    continue
                for expression in deferred.expressions:
                    violations.update(wide_union_report_lines(expression, state))
                    if deferred.generic_base_report_line is not None:
                        violations.update(
                            specialized_generic_base_report_lines(
                                expression,
                                state,
                                report_line=deferred.generic_base_report_line,
                            )
                        )

    def promote_completed_child_scope(
        node: ast.ClassDef,
        child_paths: tuple[_AnalysisPath, ...],
        parent_path: _AnalysisPath,
        parameter_frame: _CommandTypeState | None,
        legacy_parameter_names: tuple[str, ...] = (),
    ) -> tuple[_AnalysisPath, ...]:
        """逐路径导出完成类 frame，并保持 class 内 unknown 分支相关性。

        每个类体出路径都复制成独立父路径并绑定一个冻结命名空间。限定属性因此可
        沿同一路径解析，互斥分支上的多个类成员不会被扁平合并成虚假的宽联合；
        nested class 的延迟表达式仍跳过所有外层 class 命名空间。
        """
        promoted_paths: list[_AnalysisPath] = []
        parameter_names = type_parameter_names(node) or legacy_parameter_names
        (
            parameter_kinds,
            parameter_defaults,
            parameter_default_states,
        ) = alias_parameter_metadata(
            parameter_names,
            parameter_frame or parent_path.command_state,
        )
        base_local_frames = () if parameter_frame is None else (parameter_frame,)
        base_type_expressions = tuple(
            _DeferredTypeExpression(
                expressions=(base,),
                local_frames=base_local_frames,
                generic_base_report_line=base.lineno,
            )
            for base in node.bases
        )
        for child_path in child_paths:
            local_frame = child_path.command_state.reparented_copy(None)
            promoted_path = parent_path.branch_copy()
            promoted_path.pending_expressions.extend(
                _DeferredTypeExpression(
                    expressions=deferred.expressions,
                    local_frames=(
                        deferred.local_frames
                        if deferred.skip_enclosing_class_frames
                        else (local_frame, *deferred.local_frames)
                    ),
                    constraint_report_line=deferred.constraint_report_line,
                    skip_enclosing_class_frames=(
                        deferred.skip_enclosing_class_frames
                        or promoted_path.command_state.is_class_scope
                    ),
                    generic_base_report_line=deferred.generic_base_report_line,
                )
                for deferred in child_path.pending_expressions
            )
            bind_command_definition(node, promoted_path)
            promoted_path.command_state.class_definitions[node.name] = (
                _ClassTypeDefinition(
                    namespace=local_frame,
                    parameter_names=parameter_names,
                    parameter_kinds=parameter_kinds,
                    parameter_defaults=parameter_defaults,
                    parameter_default_states=parameter_default_states,
                    member_type_expressions=tuple(
                        child_path.inheritable_type_expressions
                    ),
                    base_type_expressions=base_type_expressions,
                )
            )
            bind_visibility_names(node, promoted_path.type_checking_visibility)
            promoted_paths.append(promoted_path)
        return tuple(promoted_paths)

    def class_lexical_parent(state: _CommandTypeState) -> _CommandTypeState | None:
        """返回 class body 的真实词法父级；Python 名称解析会跳过外层 class。"""
        current: _CommandTypeState | None = state
        while current is not None and current.is_class_scope:
            current = current.parent
        return current

    def resolved_class_definition(
        node: ast.expr,
        state: _CommandTypeState,
    ) -> tuple[_ClassTypeDefinition, _CommandTypeState] | None:
        """沿 Name/Attribute 链定位 class 定义及接回词法父级的命名空间。"""
        if isinstance(node, ast.Name):
            owner = binding_state(state, node.id)
            if owner is None:
                return None
            class_definition = owner.class_definitions.get(node.id)
        elif isinstance(node, ast.Attribute):
            owner = resolved_class_namespace(node.value, state)
            if owner is None:
                return None
            class_definition = owner.class_definitions.get(node.attr)
        else:
            return None
        if class_definition is None:
            return None
        namespace = class_definition.namespace.reparented_copy(
            class_lexical_parent(owner)
        )
        return class_definition, namespace

    def resolved_class_namespace(
        node: ast.expr,
        state: _CommandTypeState,
    ) -> _CommandTypeState | None:
        """返回限定 class 属性链最终节点的路径化命名空间。"""
        resolved = resolved_class_definition(node, state)
        return None if resolved is None else resolved[1]

    def legacy_generic_base_arguments(
        node: ast.expr,
        state: _CommandTypeState,
        resolving: frozenset[tuple[int, str]] = frozenset(),
    ) -> tuple[ast.expr, ...] | None:
        """解包受信 ``Generic[...]`` 或其传统 alias，返回直接类型参数。

        这里只识别 class 声明直接使用的 Generic 标记，不追踪普通基类、更深层
        MRO 或任意运行时调用；alias 循环和非 Generic 表达式固定返回 ``None``。
        """
        if isinstance(node, ast.Subscript) and "Generic" in typing_forms(
            node.value,
            state,
        ):
            if isinstance(node.slice, ast.Tuple):
                return tuple(node.slice.elts)
            return (node.slice,)

        if isinstance(node, ast.Name):
            alias_name = node.id
            owner = binding_state(state, alias_name)
        elif isinstance(node, ast.Attribute):
            owner = resolved_class_namespace(node.value, state)
            alias_name = node.attr
        else:
            return None
        if owner is None:
            return None
        alias_expression = owner.alias_expressions.get(alias_name)
        if alias_expression is None:
            return None
        resolution_key = (id(alias_expression), alias_name)
        if resolution_key in resolving:
            return None
        return legacy_generic_base_arguments(
            alias_expression.expression,
            owner,
            resolving | {resolution_key},
        )

    def outer_legacy_typevar_name(
        argument: _TypeArgument,
        resolving: frozenset[tuple[int, str]] = frozenset(),
    ) -> str | None:
        """沿一次 alias substitution 找回外层 legacy 类型参数名。"""
        expression = argument.expression
        if not isinstance(expression, ast.Name):
            return None
        if argument.substitutions is not None:
            resolution_key = (id(argument.substitutions), expression.id)
            if resolution_key in resolving:
                return None
            nested = argument.substitutions.bindings.get(expression.id)
            if nested is not None:
                return outer_legacy_typevar_name(
                    nested,
                    resolving | {resolution_key},
                )
        owner = binding_state(argument.state, expression.id)
        definition = (
            owner.type_variable_definitions.get(expression.id)
            if owner is not None
            else None
        )
        if definition is None or definition.kind not in {
            "type_var",
            "param_spec",
        }:
            return None
        return expression.id

    def legacy_generic_parameter_names(
        bases: tuple[ast.expr, ...],
        state: _CommandTypeState,
    ) -> tuple[str, ...]:
        """从直接 Generic 或泛型基类应用提取 legacy TypeVar 顺序。

        ``Generic[U]`` 与 ``Base[U]`` 是同一组形参的两种声明，不应因重复出现
        就丢弃中间类；只有参数数量、顺序或外层映射真正冲突时才 fail closed。
        """
        parameter_names: tuple[str, ...] | None = None
        for base in bases:
            arguments = legacy_generic_base_arguments(base, state)
            candidate_names: list[str] = []
            if arguments is not None:
                for expression in arguments:
                    name = outer_legacy_typevar_name(
                        _TypeArgument(expression=expression, state=state, substitutions=None)
                    )
                    if name is None or name in candidate_names:
                        candidate_names = []
                        break
                    candidate_names.append(name)
            else:
                application = generic_class_application(base, state)
                if application is None:
                    continue
                class_definition, _, substitutions = application
                for parameter_name in class_definition.parameter_names:
                    argument = substitutions.bindings.get(parameter_name)
                    name = (
                        None
                        if argument is None
                        else outer_legacy_typevar_name(argument)
                    )
                    if name is None or name in candidate_names:
                        candidate_names = []
                        break
                    candidate_names.append(name)
            if not candidate_names:
                continue
            candidate = tuple(candidate_names)
            if parameter_names is not None:
                if candidate != parameter_names:
                    return ()
                continue
            parameter_names = candidate
        return () if parameter_names is None else parameter_names

    def legacy_generic_parameter_frame(
        parameter_names: tuple[str, ...],
        state: _CommandTypeState,
    ) -> _CommandTypeState | None:
        """复制 legacy TypeVar 定义到 class 局部 frame，供成员签名替换。"""
        if not parameter_names:
            return None
        frame = _CommandTypeState(parent=None)
        for parameter_name in parameter_names:
            owner = binding_state(state, parameter_name)
            definition = (
                owner.type_variable_definitions.get(parameter_name)
                if owner is not None
                else None
            )
            if definition is None:
                return None
            frame.reset_local_name(parameter_name)
            frame.type_variable_definitions[parameter_name] = definition
        return frame

    def generic_class_application(
        node: ast.expr,
        state: _CommandTypeState,
        substitutions: _TypeSubstitutionFrame | None = None,
        resolving: frozenset[tuple[int, str]] = frozenset(),
    ) -> tuple[
        _ClassTypeDefinition,
        _CommandTypeState,
        _TypeSubstitutionFrame,
    ] | None:
        """解析本地泛型 class 应用，并把基类实参绑定到定义点形参。"""
        resolved_alias = alias_application(node, state, substitutions)
        if resolved_alias is not None:
            owner, alias_name, alias_expression, alias_substitutions, _ = (
                resolved_alias
            )
            resolution_key = (id(alias_expression), alias_name)
            if resolution_key in resolving:
                return None
            return generic_class_application(
                alias_expression.expression,
                owner,
                alias_substitutions,
                resolving | {resolution_key},
            )

        # ``BaseAlias: TypeAlias = Base`` 没有可由 alias_application 推断的
        # TypeVar；仅在底层值仍是 Name/Attribute 时把显式实参转发给真实 class。
        if isinstance(node, ast.Subscript) and isinstance(
            node.value,
            ast.Name | ast.Attribute,
        ):
            alias_node = node.value
            if isinstance(alias_node, ast.Name):
                alias_name = alias_node.id
                alias_owner = binding_state(state, alias_name)
            else:
                alias_name = alias_node.attr
                alias_owner = resolved_class_namespace(alias_node.value, state)
            alias_expression = (
                alias_owner.alias_expressions.get(alias_name)
                if alias_owner is not None
                else None
            )
            if (
                alias_owner is not None
                and alias_expression is not None
                and not alias_expression.parameter_names
                and not isinstance(alias_expression.expression, ast.Subscript)
            ):
                resolution_key = (id(alias_expression), alias_name)
                if resolution_key in resolving:
                    return None
                forwarded = ast.copy_location(
                    ast.Subscript(
                        value=alias_expression.expression,
                        slice=node.slice,
                        ctx=ast.Load(),
                    ),
                    node,
                )
                return generic_class_application(
                    forwarded,
                    alias_owner,
                    substitutions,
                    resolving | {resolution_key},
                )

        class_node: ast.expr
        arguments: tuple[ast.expr, ...]
        if isinstance(node, ast.Name | ast.Attribute):
            class_node = node
            arguments = ()
            parameterized = False
        elif isinstance(node, ast.Subscript) and isinstance(
            node.value,
            ast.Name | ast.Attribute,
        ):
            class_node = node.value
            arguments = (
                tuple(node.slice.elts)
                if isinstance(node.slice, ast.Tuple)
                else (node.slice,)
            )
            parameterized = True
        else:
            return None

        resolved = resolved_class_definition(class_node, state)
        if resolved is None:
            return None
        class_definition, namespace = resolved
        parameter_names = class_definition.parameter_names
        if not parameter_names:
            return None
        parameter_kinds = class_definition.parameter_kinds or tuple(
            "type_var" for _ in parameter_names
        )
        parameter_defaults = class_definition.parameter_defaults or tuple(
            None for _ in parameter_names
        )
        parameter_default_states = (
            class_definition.parameter_default_states
            or tuple(None for _ in parameter_names)
        )

        if parameterized:
            variadic_indexes = tuple(
                index
                for index, kind in enumerate(parameter_kinds)
                if kind in {"param_spec", "type_var_tuple"}
            )
            if len(variadic_indexes) > 1:
                return None
            if not variadic_indexes:
                if len(arguments) > len(parameter_names):
                    return None
                missing_defaults = parameter_defaults[len(arguments) :]
                if any(default is None for default in missing_defaults):
                    return None
                bound_arguments = arguments + tuple(
                    default for default in missing_defaults if default is not None
                )
                bound_argument_states = (state,) * len(arguments) + tuple(
                    parameter_default_states[index] or namespace
                    for index in range(len(arguments), len(parameter_names))
                )
            else:
                variadic_index = variadic_indexes[0]
                trailing_count = len(parameter_names) - variadic_index - 1
                if parameter_kinds[variadic_index] == "param_spec":
                    # ParamSpec 在 class 应用中同样只占一个参数列表槽；尾部固定
                    # TypeVar 从其后按位置绑定，未提供的连续尾部读取声明点 default。
                    leading_count = variadic_index
                    if len(arguments) < leading_count:
                        return None
                    leading = arguments[:leading_count]
                    remaining_arguments = arguments[leading_count:]
                    if not remaining_arguments:
                        variadic_default = parameter_defaults[variadic_index]
                        if variadic_default is None:
                            return None
                        variadic_expression = variadic_default
                        variadic_state = (
                            parameter_default_states[variadic_index] or namespace
                        )
                        explicit_trailing = ()
                    else:
                        variadic_expression = remaining_arguments[0]
                        variadic_state = state
                        explicit_trailing = remaining_arguments[1:]
                    if len(explicit_trailing) > trailing_count:
                        return None
                    missing_trailing_start = variadic_index + 1 + len(
                        explicit_trailing
                    )
                    missing_trailing_defaults = parameter_defaults[
                        missing_trailing_start:
                    ]
                    if any(default is None for default in missing_trailing_defaults):
                        return None
                    bound_arguments = (
                        *leading,
                        variadic_expression,
                        *explicit_trailing,
                        *(
                            default
                            for default in missing_trailing_defaults
                            if default is not None
                        ),
                    )
                    bound_argument_states = (
                        *((state,) * len(leading)),
                        variadic_state,
                        *((state,) * len(explicit_trailing)),
                        *(
                            parameter_default_states[index] or namespace
                            for index in range(
                                missing_trailing_start,
                                len(parameter_names),
                            )
                        ),
                    )
                else:
                    # TypeVarTuple 保留既有的中间参数折叠与 fail-closed 规则；本
                    # residual 只补 ParamSpec 与尾部 TypeVar default 的混合应用。
                    fixed_count = len(parameter_names) - 1
                    if len(arguments) < fixed_count:
                        return None
                    leading = arguments[:variadic_index]
                    trailing = (
                        arguments[len(arguments) - trailing_count :]
                        if trailing_count
                        else ()
                    )
                    variadic_end = (
                        len(arguments) - trailing_count if trailing_count else None
                    )
                    variadic_arguments = arguments[variadic_index:variadic_end]
                    if len(variadic_arguments) == 1:
                        variadic_expression = variadic_arguments[0]
                    else:
                        variadic_expression = ast.copy_location(
                            ast.Tuple(
                                elts=list(variadic_arguments),
                                ctx=ast.Load(),
                            ),
                            node,
                        )
                    bound_arguments = (*leading, variadic_expression, *trailing)
                    bound_argument_states = (state,) * len(parameter_names)

        if not parameterized:
            bindings = {
                parameter_name: _TypeArgument(
                    expression=parameter_default,
                    state=parameter_default_state or namespace,
                    substitutions=substitutions,
                )
                for (
                    parameter_name,
                    parameter_default,
                    parameter_default_state,
                ) in zip(
                    parameter_names,
                    parameter_defaults,
                    parameter_default_states,
                    strict=True,
                )
            }
        else:
            bindings = {
                parameter_name: _TypeArgument(
                    expression=argument,
                    state=argument_state,
                    substitutions=substitutions,
                )
                for parameter_name, argument, argument_state in zip(
                    parameter_names,
                    bound_arguments,
                    bound_argument_states,
                    strict=True,
                )
            }
        return (
            class_definition,
            namespace,
            _TypeSubstitutionFrame(bindings=bindings),
        )

    def member_type_expression_is_wide(
        deferred: _DeferredTypeExpression,
        namespace: _CommandTypeState,
        substitutions: _TypeSubstitutionFrame | None,
    ) -> bool:
        """判断一个直接成员模板在给定 class 实参下是否形成宽命令 union。"""
        state = namespace
        shadowed_names: set[str] = set()
        for local_frame in deferred.local_frames:
            shadowed_names.update(local_frame.local_defined)
            state = local_frame.reparented_copy(state)
        effective_substitutions = substitutions
        if substitutions is not None and shadowed_names:
            effective_substitutions = _TypeSubstitutionFrame(
                bindings={
                    name: argument
                    for name, argument in substitutions.bindings.items()
                    if name not in shadowed_names
                }
            )
        if deferred.constraint_report_line is not None:
            command_leaves = frozenset(
                leaf
                for expression in deferred.expressions
                for leaf in direct_command_meaning(
                    expression,
                    state,
                    substitutions=effective_substitutions,
                )
            )
            return len(command_leaves) >= 2
        return any(
            wide_union_report_lines(
                expression,
                state,
                substitutions=effective_substitutions,
            )
            for expression in deferred.expressions
        )

    def generic_base_application(
        deferred: _DeferredTypeExpression,
        namespace: _CommandTypeState,
        substitutions: _TypeSubstitutionFrame | None,
    ) -> tuple[
        _ClassTypeDefinition,
        _CommandTypeState,
        _TypeSubstitutionFrame,
    ] | None:
        """在 class 定义上下文中解析一个直接基类模板的泛型应用。"""
        state = namespace
        for local_frame in deferred.local_frames:
            state = local_frame.reparented_copy(state)
        for expression in deferred.expressions:
            application = generic_class_application(
                expression,
                state,
                substitutions,
            )
            if application is not None:
                return application
        return None

    def class_member_specialization_is_wide(
        class_definition: _ClassTypeDefinition,
        namespace: _CommandTypeState,
        baseline_substitutions: _TypeSubstitutionFrame | None,
        specialized_substitutions: _TypeSubstitutionFrame | None,
        visited: frozenset[tuple[int, tuple[int | None, ...]]] = frozenset(),
    ) -> bool:
        """递归比较成员特化，并按 class 与 default 定义点上下文截断环。"""
        class_key = (
            id(class_definition),
            tuple(
                id(default_state) if default_state is not None else None
                for default_state in class_definition.parameter_default_states
            ),
        )
        if class_key in visited:
            return False
        next_visited = visited | {class_key}
        for member in class_definition.member_type_expressions:
            if member_type_expression_is_wide(
                member,
                namespace,
                baseline_substitutions,
            ):
                continue
            if member_type_expression_is_wide(
                member,
                namespace,
                specialized_substitutions,
            ):
                return True

        for base in class_definition.base_type_expressions:
            baseline_application = generic_base_application(
                base,
                namespace,
                baseline_substitutions,
            )
            specialized_application = generic_base_application(
                base,
                namespace,
                specialized_substitutions,
            )
            if specialized_application is None:
                continue
            if baseline_application is None:
                baseline_class_definition = specialized_application[0]
                baseline_nested_substitutions = None
            else:
                baseline_class_definition = baseline_application[0]
                baseline_nested_substitutions = baseline_application[2]
            specialized_class_definition, specialized_namespace, nested_substitutions = (
                specialized_application
            )
            if baseline_class_definition is not specialized_class_definition:
                baseline_nested_substitutions = None
            if class_member_specialization_is_wide(
                specialized_class_definition,
                specialized_namespace,
                baseline_nested_substitutions,
                nested_substitutions,
                next_visited,
            ):
                return True
        return False

    def specialized_generic_base_report_lines(
        node: ast.expr,
        state: _CommandTypeState,
        *,
        report_line: int,
    ) -> frozenset[int]:
        """仅在基类实参让直接继承成员新形成宽 union 时报告继承行。"""
        application = generic_class_application(node, state)
        if application is None:
            return frozenset()
        class_definition, namespace, substitutions = application
        return (
            frozenset({report_line})
            if class_member_specialization_is_wide(
                class_definition,
                namespace,
                None,
                substitutions,
            )
            else frozenset()
        )

    def auxiliary_statements(node: ast.AST) -> tuple[ast.stmt, ...]:
        """按 AST 字段顺序提取 try handler 与 match case 等容器中的语句。

        本守卫仅对 unknown ``if`` 建立精确析取；循环、try 与 match 仍沿旧行为按
        语法顺序保守扫描，不声称这些结构之间具有精确的控制流相关性。
        """
        statements: list[ast.stmt] = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                statements.append(child)
            elif isinstance(child, ast.ExceptHandler | ast.match_case):
                statements.extend(auxiliary_statements(child))
        return tuple(statements)

    def collect_statements(
        statements: list[ast.stmt] | tuple[ast.stmt, ...],
        paths: tuple[_AnalysisPath, ...],
    ) -> tuple[_AnalysisPath, ...]:
        """让语句序列分别作用于每条可达路径，并在每一步安全去重。"""
        current_paths = paths
        for statement in statements:
            current_paths = deduplicate_paths(
                tuple(
                    output_path
                    for path in current_paths
                    for output_path in collect_statement(statement, path)
                )
            )
        return current_paths

    def collect_statement(
        node: ast.stmt,
        path: _AnalysisPath,
    ) -> tuple[_AnalysisPath, ...]:
        """分析一条路径上的语句，并返回该语句全部可达出路径。"""
        state = path.command_state
        visibility = path.type_checking_visibility
        if isinstance(node, ast.If):
            condition_truth = static_condition_truth(node.test, visibility)
            if condition_truth is not None:
                selected_branch = node.body if condition_truth else node.orelse
                return collect_statements(selected_branch, (path,))

            body_paths = collect_statements(node.body, (path.branch_copy(),))
            else_paths = collect_statements(node.orelse, (path.branch_copy(),))
            return deduplicate_paths((*body_paths, *else_paths))

        if isinstance(node, ast.ClassDef):
            pep_parameter_frame = type_parameter_frame(node)
            record_type_parameter_bounds(node, path, pep_parameter_frame)
            legacy_parameter_names = (
                ()
                if pep_parameter_frame is not None
                else legacy_generic_parameter_names(node.bases, state)
            )
            parameter_frame = (
                pep_parameter_frame
                or legacy_generic_parameter_frame(legacy_parameter_names, state)
            )
            # PEP 695 frame 优先；legacy Generic[T] 只登记当前 class 的直接参数，
            # 不把普通基类或更深层 MRO 传播成隐式参数。
            if pep_parameter_frame is None and parameter_frame is None:
                legacy_parameter_names = ()
            local_frames = () if parameter_frame is None else (parameter_frame,)
            for base in node.bases:
                record_type_expression(
                    base,
                    path,
                    local_frames=local_frames,
                    generic_base_report_line=base.lineno,
                )
            lexical_parent = class_lexical_parent(state)
            class_state = (
                parameter_frame.reparented_copy(lexical_parent)
                if parameter_frame is not None
                else _CommandTypeState(parent=lexical_parent)
            )
            class_state.is_class_scope = True
            class_path = _AnalysisPath(
                command_state=class_state,
                type_checking_visibility=_TypeCheckingVisibility(parent=visibility),
            )
            # 类体 unknown-if 的完成命名空间逐路径导出，避免限定属性交叉污染。
            class_paths = collect_statements(node.body, (class_path,))
            return promote_completed_child_scope(
                node,
                class_paths,
                path,
                parameter_frame,
                legacy_parameter_names,
            )

        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            collect_function_signature(node, path)
            bind_command_definition(node, path)
            bind_visibility_names(node, visibility)
            return (path,)

        if isinstance(
            node,
            ast.AnnAssign | ast.Assign | ast.Import | ast.ImportFrom | ast.TypeAlias,
        ):
            bind_command_definition(node, path)
            bind_visibility_names(node, visibility)
            return (path,)

        nested_statements = auxiliary_statements(node)
        if nested_statements:
            return collect_statements(nested_statements, (path,))
        return (path,)

    root_path = _AnalysisPath(
        command_state=root_state,
        type_checking_visibility=_TypeCheckingVisibility(parent=None),
    )
    module_paths = collect_statements(tree.body, (root_path,))
    evaluate_completed_paths(module_paths)
    return tuple(sorted(violations))


def _mail_payload(**overrides: object) -> dict[str, object]:
    """返回只包含标准 JSON 值的有效新邮件命令。"""
    payload: dict[str, object] = {
        "schema_version": "mail_send.v1",
        "action": "mail.send",
        "operation_id": str(OPERATION_ID),
        "connection_id": str(CONNECTION_ID),
        "draft_id": str(DRAFT_ID),
        "draft_version": 1,
        "message_date": "2026-08-06T09:30:00+08:00",
        "mode": "new",
        "source_thread_id": None,
        "source_message_id": None,
        "to": ["owner@Example.test"],
        "cc": [],
        "bcc": [],
        "subject": "Synthetic 会议",
        "body_text": "Synthetic body",
        "thread_headers": None,
    }
    payload.update(overrides)
    return payload


def _reply_payload(*, mode: str = "reply", **overrides: object) -> dict[str, object]:
    """返回完整绑定源线程和规范引用头的有效回复命令。"""
    payload = _mail_payload(
        mode=mode,
        source_thread_id="thread-synthetic",
        source_message_id="message-synthetic",
        thread_headers={
            "in_reply_to": "<source-message@example.test>",
            "references": [
                "<root-message@example.test>",
                "<source-message@example.test>",
            ],
        },
    )
    payload.update(overrides)
    return payload


def _calendar_payload(action: str, **overrides: object) -> dict[str, object]:
    """按动作返回显式完整期望状态的有效日历命令。"""
    schema_versions = {
        "calendar.create": "calendar_create.v1",
        "calendar.update": "calendar_update.v1",
        "calendar.restore": "calendar_restore.v1",
    }
    payload: dict[str, object] = {
        "schema_version": schema_versions[action],
        "action": action,
        "operation_id": str(OPERATION_ID),
        "connection_id": str(CONNECTION_ID),
        "calendar_id": "primary-synthetic",
        "title": "Synthetic meeting",
        "description": "Synthetic description",
        "location": "Synthetic room",
        "starts_at": "2026-08-06T01:00:00Z",
        "ends_at": "2026-08-06T02:00:00Z",
        "timezone": "Asia/Shanghai",
        "all_day": False,
        "attendees": ["owner@Example.test"],
        "notification_policy": "all",
    }
    if action == "calendar.create":
        payload["client_event_id"] = calendar_client_event_id(OPERATION_ID)
    else:
        payload.update(
            {
                "provider_event_id": "provider-event-synthetic",
                "base_etag": '"etag-synthetic"',
                "before_snapshot_id": str(SNAPSHOT_ID),
                "changed_fields": ["description", "title"],
            }
        )
    payload.update(overrides)
    return payload


def test_parse_mail_command_maps_json_values_to_frozen_domain_types() -> None:
    """UUID、时间、枚举、tuple 和严格回复头应在 Pydantic 后显式映射为领域值。"""
    command = parse_trusted_command(
        _reply_payload(
            mode="reply_all",
            to=["owner@Example.test"],
            cc=["owner@example.test", "Owner@Example.test"],
        )
    )

    assert isinstance(command, MailSendCommand)
    assert command.operation_id == OPERATION_ID
    assert command.connection_id == CONNECTION_ID
    assert command.draft_id == DRAFT_ID
    assert command.message_date.utcoffset() is not None
    assert command.mode is MailMode.REPLY_ALL
    assert command.to == ("owner@example.test",)
    assert command.cc == ("Owner@example.test",)
    assert isinstance(command.thread_headers, ReplyThreadHeaders)


@pytest.mark.parametrize(
    ("action", "expected_type"),
    (
        ("calendar.create", CalendarCreateCommand),
        ("calendar.update", CalendarUpdateCommand),
        ("calendar.restore", CalendarRestoreCommand),
    ),
)
def test_parse_calendar_union_maps_each_action_to_explicit_domain_type(
    action: str,
    expected_type: type[object],
) -> None:
    """判别联合必须只产生三种显式日历领域命令。"""
    command = parse_trusted_command(_calendar_payload(action))

    assert isinstance(command, expected_type)
    assert command.operation_id == OPERATION_ID
    assert command.connection_id == CONNECTION_ID
    assert command.attendees == ("owner@example.test",)
    assert command.notification_policy is NotificationPolicy.ALL


def test_public_command_aliases_expand_to_exact_m2_command_types() -> None:
    """可信命令顶层必须复用日历 alias，递归叶类型仍精确覆盖四种命令。"""
    calendar_members = get_args(CalendarCommand.__value__)
    trusted_members = get_args(TrustedCommand.__value__)

    assert calendar_members == (
        CalendarCreateCommand,
        CalendarUpdateCommand,
        CalendarRestoreCommand,
    )
    assert trusted_members == (MailSendCommand, CalendarCommand)
    assert (trusted_members[0], *get_args(trusted_members[1].__value__)) == (
        MailSendCommand,
        CalendarCreateCommand,
        CalendarUpdateCommand,
        CalendarRestoreCommand,
    )


def test_wide_command_union_detector_rejects_rebuilt_union_but_allows_aliases() -> None:
    """静态检测器应拒绝宽具体联合，同时允许官方 alias、单命令和 Optional。"""
    violating = """
def execute(command: CalendarCreateCommand | CalendarUpdateCommand | CalendarRestoreCommand) -> None:
    pass
"""
    allowed = """
def execute(command: CalendarCommand) -> None:
    pass

def send(command: MailSendCommand | None) -> None:
    pass
"""

    assert _wide_command_union_annotation_lines(violating) == (2,)
    assert _wide_command_union_annotation_lines(allowed) == ()


def test_wide_command_union_detector_rejects_module_level_alias_bypasses() -> None:
    """模块级 PEP 695 alias 与 ``typing.Union`` 赋值也不能重建具体命令联合。"""
    violating = (
        "type ProviderCommand = CalendarCreateCommand | CalendarUpdateCommand\n"
        "LegacyCommand = typing.Union[MailSendCommand, CalendarCreateCommand]\n"
    )
    allowed = (
        "type ProviderCommand = CalendarCommand\n"
        "OptionalMail = MailSendCommand | None\n"
        "LegacyOptional = typing.Union[MailSendCommand, None]\n"
    )

    assert _wide_command_union_annotation_lines(violating) == (1, 2)
    assert _wide_command_union_annotation_lines(allowed) == ()


def test_wide_command_union_detector_resolves_imported_command_aliases() -> None:
    """from-import 重命名不能让函数、PEP 695 或传统类型上下文绕过检测。"""
    source = (
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand as Create, CalendarUpdateCommand as Update\n"
        "type ProviderCommand = Create | Update\n"
        "LegacyCommand = Create | Update\n"
        "class Adapter:\n"
        "    command: Create | Update\n"
        "def execute(command: Create | Update) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2, 3, 5, 6)


def test_wide_command_union_detector_resolves_module_and_typing_aliases() -> None:
    """模块属性命令与重命名 ``typing.Union`` 在传统 TypeAlias 中仍应识别。"""
    source = (
        "import ai_employee.domain.calendar_actions as calendar_actions\n"
        "from typing import TypeAlias, Union as TypeUnion\n"
        "LegacyCommand: TypeAlias = TypeUnion[calendar_actions.CalendarCreateCommand, calendar_actions.CalendarUpdateCommand]\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_typing_forms_optional_direct_import_has_single_type_meaning() -> None:
    """重命名 Optional 应展开唯一类型参数，单独使用仍保持窄命令语义。"""
    source = (
        "from typing import Optional as Maybe\n"
        "def violating(command: Maybe[CalendarCreateCommand] | CalendarUpdateCommand) -> None: ...\n"
        "def allowed(command: Maybe[CalendarCreateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_typing_forms_optional_module_alias_has_single_type_meaning() -> None:
    """typing 模块 alias 的 Optional 与直接导入遵守相同命令含义。"""
    source = (
        "import typing as typing_module\n"
        "def violating(command: typing_module.Optional[CalendarCreateCommand] | CalendarUpdateCommand) -> None: ...\n"
        "def allowed(command: typing_module.Optional[CalendarCreateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_typing_forms_optional_shadow_is_opaque() -> None:
    """普通赋值 shadow Optional 后不得继续套用受信 typing 特殊语义。"""
    source = (
        "from typing import Optional\n"
        "Optional = runtime_optional\n"
        "def allowed(command: Optional[CalendarCreateCommand] | CalendarUpdateCommand) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_typing_forms_typing_extensions_type_alias_direct_import() -> None:
    """typing_extensions.TypeAlias 直接导入必须标记传统 alias 值为类型边界。"""
    source = (
        "from typing_extensions import TypeAlias\n"
        "Provider: TypeAlias = CalendarCreateCommand | CalendarUpdateCommand\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_typing_forms_typing_extensions_type_alias_module_import() -> None:
    """typing_extensions 模块 alias 的 TypeAlias 标记同样受信。"""
    source = (
        "import typing_extensions as typing_ext\n"
        "Provider: typing_ext.TypeAlias = CalendarCreateCommand | CalendarUpdateCommand\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_typing_forms_typing_extensions_type_alias_preserves_generic_inference() -> None:
    """扩展模块 TypeAlias 必须复用 legacy TypeVar 的隐式形参推断。"""
    source = (
        "from typing_extensions import TypeAlias, TypeVar\n"
        'T = TypeVar("T")\n'
        "Provider: TypeAlias = CalendarCreateCommand | T\n"
        "def execute(command: Provider[CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (4,)


def test_typing_forms_typing_extensions_direct_aliases_respect_type_positions() -> None:
    """扩展模块特殊形式应识别 union/Optional，并排除 Literal 与 metadata 数据。"""
    source = (
        "from typing_extensions import Annotated as A, Literal as L, Optional as O, Union as U\n"
        'AnnotatedWide: A[CalendarCreateCommand, "CalendarCreateCommand | CalendarUpdateCommand"] | CalendarUpdateCommand\n'
        "UnionWide: U[CalendarCreateCommand, CalendarUpdateCommand]\n"
        "OptionalWide: O[CalendarCreateCommand] | CalendarUpdateCommand\n"
        'LiteralSafe: L["CalendarCreateCommand | CalendarUpdateCommand"]\n'
        'MetadataSafe: A[str, "CalendarCreateCommand | CalendarUpdateCommand"]\n'
    )

    assert _wide_command_union_annotation_lines(source) == (2, 3, 4)


def test_typing_forms_typing_extensions_module_alias_respects_type_positions() -> None:
    """扩展模块属性形式与 direct import 使用同一特殊类型位置规则。"""
    source = (
        "import typing_extensions as typing_ext\n"
        'AnnotatedWide: typing_ext.Annotated[CalendarCreateCommand, "CalendarCreateCommand | CalendarUpdateCommand"] | CalendarUpdateCommand\n'
        "UnionWide: typing_ext.Union[CalendarCreateCommand, CalendarUpdateCommand]\n"
        "OptionalWide: typing_ext.Optional[CalendarCreateCommand] | CalendarUpdateCommand\n"
        'LiteralSafe: typing_ext.Literal["CalendarCreateCommand | CalendarUpdateCommand"]\n'
        'MetadataSafe: typing_ext.Annotated[str, "CalendarCreateCommand | CalendarUpdateCommand"]\n'
    )

    assert _wide_command_union_annotation_lines(source) == (2, 3, 4)


def test_typing_forms_typing_extensions_shadowed_names_are_opaque() -> None:
    """direct 与 module 名被普通值覆盖后，都不得保留扩展模块特殊语义。"""
    source = (
        "from typing_extensions import Optional, TypeAlias, Union\n"
        "import typing_extensions as typing_ext\n"
        "Optional = runtime_optional\n"
        "TypeAlias = runtime_type_alias\n"
        "Union = runtime_union\n"
        "typing_ext = runtime_typing_extensions\n"
        "DirectOptional: Optional[CalendarCreateCommand] | CalendarUpdateCommand\n"
        "DirectUnion: Union[CalendarCreateCommand, CalendarUpdateCommand]\n"
        "DirectAlias: TypeAlias = CalendarCreateCommand | CalendarUpdateCommand\n"
        "ModuleOptional: typing_ext.Optional[CalendarCreateCommand] | CalendarUpdateCommand\n"
        "ModuleUnion: typing_ext.Union[CalendarCreateCommand, CalendarUpdateCommand]\n"
        "ModuleAlias: typing_ext.TypeAlias = CalendarCreateCommand | CalendarUpdateCommand\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_ignores_runtime_union_expressions() -> None:
    """运行时 isinstance 的 PEP 604 参数不是边界类型声明，不得产生误报。"""
    source = (
        "def accepts(command: object) -> bool:\n"
        "    return isinstance(command, CalendarCreateCommand | CalendarUpdateCommand)\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_resolves_relative_import_aliases() -> None:
    """适配器模块的相对 from-import 重命名必须解析到具体领域命令。"""
    source = (
        "from ..domain.calendar_actions import CalendarCreateCommand as Create, CalendarUpdateCommand as Update\n"
        "type ProviderCommand = Create | Update\n"
    )

    assert _wide_command_union_annotation_lines(
        source,
        module_name="ai_employee.integrations.synthetic_adapter",
    ) == (2,)


def test_wide_command_union_detector_parses_string_annotations_without_execution() -> None:
    """显式字符串注解应按 eval AST 安全解析，无效字符串则固定忽略。"""
    violating = (
        'def execute(command: "CalendarCreateCommand | CalendarUpdateCommand") -> None:\n'
        "    pass\n"
    )
    invalid = 'def execute(command: "not valid [") -> None:\n    pass\n'

    assert _wide_command_union_annotation_lines(violating) == (1,)
    assert _wide_command_union_annotation_lines(invalid) == ()


def test_wide_command_union_detector_propagates_indirect_alias_leaf_types() -> None:
    """PEP695、Assign、AnnAssign 的单命令 alias 组合后仍必须识别宽联合。"""
    violating = (
        "from typing import TypeAlias\n"
        "type CreateOnly = CalendarCreateCommand\n"
        "UpdateOnly = CalendarUpdateCommand\n"
        "RestoreOnly: TypeAlias = CalendarRestoreCommand\n"
        "type PepWide = CreateOnly | UpdateOnly\n"
        "AssignedWide = CreateOnly | RestoreOnly\n"
        "AnnotatedWide: TypeAlias = UpdateOnly | RestoreOnly\n"
    )
    allowed = (
        "from ai_employee.application.commands import CalendarCommand as CalendarAlias, TrustedCommand as TrustedAlias\n"
        "from typing import TypeAlias\n"
        "type MaybeCalendar = CalendarAlias | None\n"
        "MaybeTrusted: TypeAlias = TrustedAlias | None\n"
    )

    assert _wide_command_union_annotation_lines(violating) == (5, 6, 7)
    assert _wide_command_union_annotation_lines(allowed) == ()


def test_wide_command_union_detector_isolates_annotated_aliases_between_classes() -> None:
    """Sibling class 的同名传统 alias 不得互相覆盖或污染方法签名。"""
    violating = (
        "from typing import TypeAlias\n"
        "class Violating:\n"
        "    Alias: TypeAlias = CalendarCreateCommand\n"
        "    def execute(self, command: Alias | CalendarUpdateCommand) -> None:\n"
        "        pass\n"
        "class Harmless:\n"
        "    Alias: TypeAlias = str\n"
    )
    allowed = (
        "from typing import TypeAlias\n"
        "class Allowed:\n"
        "    Alias: TypeAlias = str\n"
        "    def execute(self, command: Alias | CalendarUpdateCommand) -> None:\n"
        "        pass\n"
        "class Other:\n"
        "    Alias: TypeAlias = CalendarCreateCommand\n"
    )

    assert _wide_command_union_annotation_lines(violating) == (4,)
    assert _wide_command_union_annotation_lines(allowed) == ()


def test_wide_command_union_detector_scopes_pep695_aliases_and_nested_shadowing() -> None:
    """类级 alias 隔离 sibling；嵌套类裸名须跳过外层 class 并回到模块。"""
    violating = (
        "class Violating:\n"
        "    type Alias = CalendarCreateCommand\n"
        "    def execute(self, command: Alias | CalendarUpdateCommand) -> None:\n"
        "        pass\n"
        "class Harmless:\n"
        "    type Alias = str\n"
    )
    shadowed = (
        "type Alias = CalendarCreateCommand\n"
        "class Outer:\n"
        "    type Alias = str\n"
        "    class Nested:\n"
        "        def execute(self, command: Alias | CalendarUpdateCommand) -> None:\n"
        "            pass\n"
    )

    assert _wide_command_union_annotation_lines(violating) == (3,)
    assert _wide_command_union_annotation_lines(shadowed) == (5,)


def test_class_model_nested_class_skips_legacy_outer_class_namespace() -> None:
    """嵌套 class 方法的裸名不闭包捕获外层 class 的传统 TypeAlias。"""
    source = (
        "from typing import TypeAlias\n"
        "Alias: TypeAlias = CalendarCreateCommand\n"
        "class Outer:\n"
        "    Alias: TypeAlias = str\n"
        "    class Nested:\n"
        "        def execute(self, command: Alias | CalendarUpdateCommand) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (6,)


def test_wide_command_union_detector_keeps_function_body_names_out_of_signatures() -> None:
    """同步或异步函数体的局部 Alias 都不得污染外围 Module/Class 签名 scope。"""
    source = (
        "type Alias = CalendarCreateCommand\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    Alias = str\n"
        "class Adapter:\n"
        "    type Alias = CalendarCreateCommand\n"
        "    async def execute(self, command: Alias | CalendarRestoreCommand) -> None:\n"
        "        Alias = str\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2, 6)


def test_wide_command_union_detector_selects_type_checking_body_over_else() -> None:
    """静态类型检查视角必须选择 TYPE_CHECKING body，不能被运行时 else 覆盖。"""
    source = (
        "from typing import TYPE_CHECKING, TypeAlias\n"
        "if TYPE_CHECKING:\n"
        "    Alias: TypeAlias = CalendarCreateCommand\n"
        "else:\n"
        "    Alias: TypeAlias = str\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (6,)


def test_wide_command_union_detector_does_not_collect_type_checking_else_alias() -> None:
    """TYPE_CHECKING body 的合法 alias 不得被互斥 else 中的具体命令 alias 污染。"""
    source = (
        "from typing import TYPE_CHECKING, TypeAlias\n"
        "if TYPE_CHECKING:\n"
        "    Alias: TypeAlias = str\n"
        "else:\n"
        "    Alias: TypeAlias = CalendarCreateCommand\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_selects_inverse_not_type_checking_branch() -> None:
    """一层 ``not TYPE_CHECKING`` 必须按 False 处理并跳过其运行时覆盖。"""
    violating = (
        "from typing import TYPE_CHECKING\n"
        "type Alias = CalendarCreateCommand\n"
        "if not TYPE_CHECKING:\n"
        "    type Alias = str\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )
    allowed = (
        "from typing import TYPE_CHECKING\n"
        "type Alias = str\n"
        "if not TYPE_CHECKING:\n"
        "    type Alias = CalendarCreateCommand\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(violating) == (5,)
    assert _wide_command_union_annotation_lines(allowed) == ()


def test_wide_command_union_detector_resolves_type_checking_module_aliases() -> None:
    """``typing.TYPE_CHECKING`` 与重命名模块属性必须共享同一静态分支语义。"""
    violating = (
        "import typing\n"
        "if typing.TYPE_CHECKING:\n"
        "    type Alias = CalendarCreateCommand\n"
        "else:\n"
        "    type Alias = str\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )
    allowed = (
        "import typing as t\n"
        "if t.TYPE_CHECKING:\n"
        "    type Alias = str\n"
        "else:\n"
        "    type Alias = CalendarCreateCommand\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(violating) == (6,)
    assert _wide_command_union_annotation_lines(allowed) == ()


def test_wide_command_union_detector_handles_aliased_nested_type_checking_branches() -> None:
    """from-import alias 在 class 嵌套 if/elif 中仍须选择唯一静态可见分支。"""
    violating = (
        "from typing import TYPE_CHECKING as TC\n"
        "class Adapter:\n"
        "    if TC:\n"
        "        if not TC:\n"
        "            type Alias = str\n"
        "        else:\n"
        "            type Alias = CalendarCreateCommand\n"
        "    else:\n"
        "        type Alias = str\n"
        "    def execute(self, command: Alias | CalendarUpdateCommand) -> None:\n"
        "        pass\n"
    )
    allowed = (
        "from typing import TYPE_CHECKING as TC\n"
        "class Adapter:\n"
        "    if not TC:\n"
        "        type Alias = CalendarCreateCommand\n"
        "    elif TC:\n"
        "        type Alias = str\n"
        "    else:\n"
        "        type Alias = CalendarCreateCommand\n"
        "    def execute(self, command: Alias | CalendarUpdateCommand) -> None:\n"
        "        pass\n"
    )

    assert _wide_command_union_annotation_lines(violating) == (10,)
    assert _wide_command_union_annotation_lines(allowed) == ()


def test_wide_command_union_detector_treats_shadowed_type_checking_as_unknown() -> None:
    """被普通赋值 shadow 的导入 alias 不再是特殊常量，未知 if 两侧都应保守扫描。"""
    source = (
        "from typing import TYPE_CHECKING as TC\n"
        "TC = runtime_flag\n"
        "if TC:\n"
        "    def allowed(command: CalendarCommand) -> None:\n"
        "        pass\n"
        "else:\n"
        "    def violating(command: CalendarCreateCommand | CalendarUpdateCommand) -> None:\n"
        "        pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (7,)


@pytest.mark.parametrize(
    ("body_alias", "else_alias"),
    (
        ("CalendarCreateCommand", "str"),
        ("str", "CalendarCreateCommand"),
    ),
)
def test_wide_command_union_detector_merges_unknown_branch_aliases_without_order_bias(
    body_alias: str,
    else_alias: str,
) -> None:
    """未知分支任一路径保留具体命令时，分支后的签名都必须保守报告。"""
    source = (
        "if runtime_flag:\n"
        f"    type Alias = {body_alias}\n"
        "else:\n"
        f"    type Alias = {else_alias}\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_wide_command_union_detector_clears_unknown_alias_when_all_paths_are_opaque() -> None:
    """未知分支所有路径都把 alias 变为普通类型时，不得保留虚假的命令叶。"""
    source = (
        "type Alias = CalendarCreateCommand\n"
        "if runtime_flag:\n"
        "    type Alias = str\n"
        "else:\n"
        "    type Alias = bytes\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_only_reports_explicit_union_after_branch_merge() -> None:
    """分支汇合的可能叶集合本身不是源码 union，只有后续显式 union 才违规。"""
    source = (
        "if runtime_flag:\n"
        "    type Alias = CalendarCreateCommand\n"
        "else:\n"
        "    type Alias = CalendarUpdateCommand\n"
        "alias_only: Alias\n"
        "def execute(command: Alias | CalendarRestoreCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (6,)


@pytest.mark.parametrize(
    ("preexisting_alias", "body_alias"),
    (
        ("CalendarCreateCommand", "str"),
        ("str", "CalendarCreateCommand"),
    ),
)
def test_wide_command_union_detector_merges_missing_else_with_prebranch_state(
    preexisting_alias: str,
    body_alias: str,
) -> None:
    """无 else 的未知分支必须合并未进入 body 时仍可见的分支前绑定。"""
    source = (
        f"type Alias = {preexisting_alias}\n"
        "if runtime_flag:\n"
        f"    type Alias = {body_alias}\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (4,)


def test_wide_command_union_detector_keeps_unknown_branch_signatures_path_local() -> None:
    """未知分支内部签名必须使用本路径 alias，不能被 sibling 分支定义覆盖。"""
    source = (
        "if runtime_flag:\n"
        "    type Alias = CalendarCreateCommand\n"
        "    def violating(command: Alias | CalendarUpdateCommand) -> None:\n"
        "        pass\n"
        "else:\n"
        "    type Alias = str\n"
        "    def allowed(command: Alias | CalendarUpdateCommand) -> None:\n"
        "        pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_wide_command_union_detector_combines_unknown_and_type_checking_branches() -> None:
    """未知外层分支汇合时仍须保留内层 TYPE_CHECKING 唯一可见路径的命令叶。"""
    source = (
        "from typing import TYPE_CHECKING\n"
        "if runtime_flag:\n"
        "    if TYPE_CHECKING:\n"
        "        type Alias = CalendarCreateCommand\n"
        "    else:\n"
        "        type Alias = str\n"
        "else:\n"
        "    type Alias = str\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (9,)


def test_wide_command_union_detector_preserves_single_alias_path_correlation() -> None:
    """互斥路径各自只有一个命令 alias 时，Optional 不得拼成虚假的宽联合。"""
    source = (
        "if runtime_flag:\n"
        "    type Alias = CalendarCreateCommand\n"
        "else:\n"
        "    type Alias = CalendarUpdateCommand\n"
        "def execute(command: Alias | None) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_preserves_cross_name_branch_correlation() -> None:
    """不同名称的互斥命令绑定不能在汇合后被组合成同一条虚假宽路径。"""
    source = (
        "if runtime_flag:\n"
        "    type Left = CalendarCreateCommand\n"
        "    type Right = str\n"
        "else:\n"
        "    type Left = str\n"
        "    type Right = CalendarUpdateCommand\n"
        "def execute(command: Left | Right) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_reports_union_wide_on_every_alias_path() -> None:
    """互斥单命令 alias 与第三命令显式联合时，每条可达路径都应报告。"""
    source = (
        "if runtime_flag:\n"
        "    type Alias = CalendarCreateCommand\n"
        "else:\n"
        "    type Alias = CalendarUpdateCommand\n"
        "def execute(command: Alias | CalendarRestoreCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_wide_command_union_detector_reports_union_wide_on_any_alias_path() -> None:
    """只要一个未知分支保留具体命令，后续与另一命令联合就必须报告。"""
    source = (
        "if runtime_flag:\n"
        "    type Alias = CalendarCreateCommand\n"
        "else:\n"
        "    type Alias = str\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_wide_command_union_detector_preserves_three_correlated_unknown_paths() -> None:
    """嵌套 unknown 与缺失 else 形成三条路径时仍须逐路径判断真实 union。"""
    source = (
        "type Alias = str\n"
        "if outer_flag:\n"
        "    if inner_flag:\n"
        "        type Alias = CalendarCreateCommand\n"
        "    else:\n"
        "        type Alias = CalendarUpdateCommand\n"
        "def optional(command: Alias | None) -> None:\n"
        "    pass\n"
        "def violating(command: Alias | CalendarRestoreCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (9,)


def test_wide_command_union_detector_prunes_repeated_static_sys_version_condition() -> None:
    """同一 sys.version_info 静态条件必须稳定选支，不能产生不可达交叉积。"""
    source = (
        "from __future__ import annotations\n"
        "import sys\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand, CalendarUpdateCommand\n"
        "if sys.version_info >= (3, 13):\n"
        "    type Left = CalendarCreateCommand\n"
        "else:\n"
        "    type Left = str\n"
        "if sys.version_info >= (3, 13):\n"
        "    type Right = str\n"
        "else:\n"
        "    type Right = CalendarUpdateCommand\n"
        "def execute(command: Left | Right) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_prunes_static_sys_version_module_alias() -> None:
    """重命名 sys 模块仍应裁剪静态版本条件，并跳过不可达的命令 alias。"""
    source = (
        "import sys as runtime_sys\n"
        "if runtime_sys.version_info < (3, 13):\n"
        "    type Alias = str\n"
        "else:\n"
        "    type Alias = CalendarCreateCommand\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


@pytest.mark.parametrize(
    ("condition", "condition_truth"),
    (
        ("sys.version_info < (3, 13)", True),
        ("sys.version_info <= (3, 12, 999)", True),
        ("sys.version_info == (99, 0)", False),
        ("sys.version_info != (3, 13)", True),
        ("sys.version_info >= (3, 13)", False),
        ("sys.version_info > (3, 11)", True),
        ("not sys.version_info >= (3, 13)", True),
    ),
)
def test_wide_command_union_detector_selects_static_sys_version_boundaries(
    condition: str,
    condition_truth: bool,
) -> None:
    """六种版本比较与一层 not 都须选择真实分支，而不是保守扫描两侧。"""
    body_alias = "str" if condition_truth else "CalendarCreateCommand"
    else_alias = "CalendarCreateCommand" if condition_truth else "str"
    source = (
        "import sys\n"
        f"if {condition}:\n"
        f"    type Alias = {body_alias}\n"
        "else:\n"
        f"    type Alias = {else_alias}\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_treats_shadowed_sys_condition_as_unknown() -> None:
    """普通绑定 shadow sys 后条件不再可证明，必须保留两次判断的精确析取。"""
    source = (
        "import sys\n"
        "sys = runtime_sys\n"
        "if sys.version_info >= (3, 13):\n"
        "    type Left = CalendarCreateCommand\n"
        "else:\n"
        "    type Left = str\n"
        "if sys.version_info >= (3, 13):\n"
        "    type Right = str\n"
        "else:\n"
        "    type Right = CalendarUpdateCommand\n"
        "def execute(command: Left | Right) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (11,)


def test_wide_command_union_detector_prunes_static_sys_platform_condition() -> None:
    """sys.platform 的稳定字符串比较也不得形成不可达的分支交叉积。"""
    source = (
        "import sys as runtime_sys\n"
        'if runtime_sys.platform == "__ai_employee_unsupported_platform__":\n'
        "    type Left = CalendarCreateCommand\n"
        "else:\n"
        "    type Left = str\n"
        'if runtime_sys.platform == "__ai_employee_unsupported_platform__":\n'
        "    type Right = str\n"
        "else:\n"
        "    type Right = CalendarUpdateCommand\n"
        "def execute(command: Left | Right) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_resolves_alias_bound_later_in_same_path() -> None:
    """PEP 695 alias 的前向名称必须在同一路径 scope 完成后再统一解析。"""
    source = (
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand, CalendarUpdateCommand\n"
        "type Wide = Later | CalendarUpdateCommand\n"
        "type Later = CalendarCreateCommand\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_wide_command_union_detector_resolves_future_signature_bindings_later() -> None:
    """future annotations 的签名可引用同一路径后文才定义的 alias 与 import。"""
    source = (
        "from __future__ import annotations\n"
        "def execute(command: Later | Update) -> None:\n"
        "    pass\n"
        "type Later = CalendarCreateCommand\n"
        "from ai_employee.domain.calendar_actions import CalendarUpdateCommand as Update\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_wide_command_union_detector_resolves_class_signature_from_later_module_import() -> None:
    """类方法 future 签名须在模块路径完成后解析其后置父 scope import。"""
    source = (
        "from __future__ import annotations\n"
        "class Adapter:\n"
        "    def execute(self, command: Later | Update) -> None:\n"
        "        pass\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand as Later, CalendarUpdateCommand as Update\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_wide_command_union_detector_resolves_class_alias_from_later_module_import() -> None:
    """类级 PEP 695 alias 须保留完成的类局部状态并读取后置模块 import。"""
    source = (
        "class Adapter:\n"
        "    type Wide = Later | Update\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand as Later, CalendarUpdateCommand as Update\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_wide_command_union_detector_resolves_nested_class_from_later_module_import() -> None:
    """嵌套类须保留完整局部 frame 链，最终接回后置模块 import。"""
    source = (
        "class Outer:\n"
        "    class Inner:\n"
        "        type Wide = Later | Update\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand as Later, CalendarUpdateCommand as Update\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_wide_command_union_detector_carries_class_context_into_outer_unknown_paths() -> None:
    """类延迟表达式须随后续模块 unknown 路径复制，并在任一宽路径上报告。"""
    source = (
        "from __future__ import annotations\n"
        "class Adapter:\n"
        "    def execute(self, command: Later | Update) -> None:\n"
        "        pass\n"
        "if runtime_flag:\n"
        "    from ai_employee.domain.calendar_actions import CalendarCreateCommand as Later, CalendarUpdateCommand as Update\n"
        "else:\n"
        "    Later = str\n"
        "    Update = str\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_wide_command_union_detector_uses_final_same_path_shadow_for_future_signature() -> None:
    """future annotations 应使用 scope 完成时的同路径绑定，后续 opaque shadow 生效。"""
    source = (
        "from __future__ import annotations\n"
        "type Alias = CalendarCreateCommand\n"
        "def execute(command: Alias | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
        "type Alias = str\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_keeps_correlation_at_exact_path_budget() -> None:
    """预算边界内的 256 条互斥路径仍须精确区分 Optional 与真实宽联合。"""
    source_lines = [
        "if flag_0:",
        "    type Alias = CalendarCreateCommand",
        "else:",
        "    type Alias = CalendarUpdateCommand",
    ]
    for index in range(1, 8):
        source_lines.extend(
            (
                f"if flag_{index}:",
                f"    type Noise{index} = str",
                "else:",
                f"    type Noise{index} = bytes",
            )
        )
    source_lines.extend(
        (
            "def optional(command: Alias | None) -> None:",
            "    pass",
            "def violating(command: Alias | CalendarRestoreCommand) -> None:",
            "    pass",
        )
    )

    assert _wide_command_union_annotation_lines("\n".join(source_lines)) == (35,)


def test_wide_command_union_detector_fails_closed_above_path_budget() -> None:
    """连续 16 个独立 unknown-if 超出预算时必须稳定失败，禁止静默丢路径。"""
    source = "\n".join(
        line
        for index in range(16)
        for line in (
            f"if flag_{index}:",
            f"    type Alias{index} = CalendarCreateCommand",
            "else:",
            f"    type Alias{index} = str",
        )
    )

    with pytest.raises(_CommandPathBudgetExceeded) as error:
        _wide_command_union_annotation_lines(source)

    assert str(error.value) == _COMMAND_ANALYSIS_PATH_BUDGET_MESSAGE


def test_wide_command_union_detector_ignores_literal_values_and_annotated_metadata() -> None:
    """Literal 值与 Annotated metadata 是数据位置，字符串不得按 forward ref 解析。"""
    source = (
        "from typing import Annotated as A, Literal as L\n"
        "import typing\n"
        'LiteralAlias: L["CalendarCreateCommand | CalendarUpdateCommand"]\n'
        'AnnotatedAlias: A[str, "CalendarCreateCommand | CalendarUpdateCommand"]\n'
        'LiteralModule: typing.Literal["CalendarCreateCommand | CalendarUpdateCommand"]\n'
        'AnnotatedModule: typing.Annotated[str, "CalendarCreateCommand | CalendarUpdateCommand"]\n'
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_parses_forward_refs_only_in_type_positions() -> None:
    """Annotated 首参数、普通泛型参数与 Union 成员中的字符串仍是类型 forward ref。"""
    source = (
        "from typing import Annotated as A, Union\n"
        'AnnotatedCommand: A["CalendarCreateCommand | CalendarUpdateCommand", "metadata"]\n'
        'GenericCommand: list["CalendarCreateCommand | CalendarRestoreCommand"]\n'
        'UnionCommand: Union["MailSendCommand", CalendarCreateCommand]\n'
    )

    assert _wide_command_union_annotation_lines(source) == (2, 3, 4)


def test_wide_command_union_detector_rejects_generic_parameter_on_function() -> None:
    """PEP 695 同步函数类型参数的宽 bound 必须报告 bound 所在行。"""
    source = (
        "def execute[\n"
        "    T: CalendarCreateCommand | CalendarUpdateCommand,\n"
        "](command: T) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_wide_command_union_detector_rejects_generic_parameter_on_async_function() -> None:
    """PEP 695 异步函数类型参数也属于架构签名，不能绕过宽命令门禁。"""
    source = (
        "async def execute[\n"
        "    T: CalendarCreateCommand | CalendarUpdateCommand,\n"
        "](command: T) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_wide_command_union_detector_rejects_generic_parameter_class_constraints() -> None:
    """PEP 695 泛型类的 constraints tuple 必须汇总全部具体命令叶。"""
    source = (
        "class Adapter[\n"
        "    T: (CalendarCreateCommand, CalendarUpdateCommand),\n"
        "]:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_wide_command_union_detector_rejects_generic_parameter_on_type_alias() -> None:
    """PEP 695 泛型 alias 的宽类型参数 bound 必须在 alias 值之外单独检查。"""
    source = (
        "type Alias[\n"
        "    T: CalendarCreateCommand | CalendarUpdateCommand,\n"
        "] = T\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_wide_command_union_detector_rejects_generic_parameter_legacy_typevar_bound() -> None:
    """重命名 TypeVar 的 bound 应延迟解析后置命令 import，并报告 bound 行。"""
    source = (
        "from typing import TypeVar as TypeParameter\n"
        "Wide = TypeParameter(\n"
        '    "Wide",\n'
        "    bound=Create | Update,\n"
        ")\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand as Create, CalendarUpdateCommand as Update\n"
    )

    assert _wide_command_union_annotation_lines(source) == (4,)


def test_wide_command_union_detector_rejects_generic_parameter_legacy_typevar_constraints() -> None:
    """typing 模块 alias 的 TypeVar 位置约束须按一组汇总，而非逐参数孤立判断。"""
    source = (
        "import typing as typing_module\n"
        "Wide = typing_module.TypeVar(\n"
        '    "Wide",\n'
        "    CalendarCreateCommand,\n"
        "    CalendarUpdateCommand,\n"
        ")\n"
    )

    assert _wide_command_union_annotation_lines(source) == (4,)


def test_wide_command_union_detector_rejects_typing_extensions_typevar_alias() -> None:
    """typing_extensions 的重命名 TypeVar 约束也必须报告首个 constraint 行。"""
    source = (
        "from typing_extensions import TypeVar as TV\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand, CalendarUpdateCommand\n"
        "T = TV(\n"
        '    "T",\n'
        "    CalendarCreateCommand,\n"
        "    CalendarUpdateCommand,\n"
        ")\n"
        "def execute(command: T) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_wide_command_union_detector_rejects_typing_extensions_typevar_module() -> None:
    """typing_extensions 模块 alias 的 TypeVar 宽 bound 必须报告 bound 行。"""
    source = (
        "import typing_extensions as te\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand, CalendarUpdateCommand\n"
        "T = te.TypeVar(\n"
        '    "T",\n'
        "    bound=CalendarCreateCommand | CalendarUpdateCommand,\n"
        ")\n"
        "def execute(command: T) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_wide_command_union_detector_allows_safe_typing_extensions_typevars() -> None:
    """typing_extensions TypeVar 的官方 alias 与单命令加普通类型约束不得误报。"""
    source = (
        "from typing_extensions import TypeVar as TV\n"
        "import typing_extensions as te\n"
        'Official = TV("Official", CalendarCommand, TrustedCommand)\n'
        'Single = te.TypeVar("Single", CalendarCreateCommand, str)\n'
        "def use_official(command: Official) -> None: ...\n"
        "def use_single(command: Single) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_restricts_typing_extensions_typevar_sources() -> None:
    """普通 shadow、第三方 re-export、wildcard 与一般 Call 都不能伪装受信 TypeVar。"""
    source = (
        "from typing_extensions import TypeVar as TV\n"
        "import typing_extensions as te\n"
        "TV = runtime_type_var\n"
        "te = runtime_typing_extensions\n"
        'ShadowedAlias = TV("ShadowedAlias", CalendarCreateCommand, CalendarUpdateCommand)\n'
        'ShadowedModule = te.TypeVar("ShadowedModule", CalendarCreateCommand, CalendarUpdateCommand)\n'
        "from vendor_typing import TypeVar as VendorTypeVar\n"
        'ThirdParty = VendorTypeVar("ThirdParty", CalendarCreateCommand, CalendarUpdateCommand)\n'
        "from typing_extensions import *\n"
        'Wildcard = TypeVar("Wildcard", CalendarCreateCommand, CalendarUpdateCommand)\n'
        'Runtime = Factory("Runtime", CalendarCreateCommand, CalendarUpdateCommand)\n'
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_typevar_usage_semantics_expands_pep695_bound_at_use_site() -> None:
    """PEP 695 单命令 bound 必须成为 T 的直接含义，并参与外层真实 union。"""
    source = (
        "def execute[\n"
        "    T: CalendarCreateCommand,\n"
        "](command: T | CalendarUpdateCommand) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_typevar_usage_semantics_expands_legacy_bound_at_use_site() -> None:
    """旧式 TypeVar 单命令 bound 也必须在后续注解使用处展开。"""
    source = (
        "from typing import TypeVar\n"
        'T = TypeVar("T", bound=CalendarCreateCommand)\n'
        "def execute(command: T | CalendarUpdateCommand) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_typevar_usage_semantics_expands_legacy_constraints_at_use_site() -> None:
    """constraints 中单个命令与普通类型的集合须保留命令直接含义供 T 使用。"""
    source = (
        "from typing import TypeVar\n"
        'T = TypeVar("T", CalendarCreateCommand, str)\n'
        "def execute(command: T | CalendarUpdateCommand) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_typevar_usage_semantics_allows_pep695_container_bounds() -> None:
    """tuple/Callable bound 内部的多个命令只是容器参数，不是直接命令集合。"""
    source = (
        "from typing import Callable\n"
        "def use_tuple[\n"
        "    T: tuple[CalendarCreateCommand, CalendarUpdateCommand],\n"
        "](command: T) -> None: ...\n"
        "def use_callable[\n"
        "    U: Callable[[CalendarCreateCommand], CalendarUpdateCommand],\n"
        "](command: U) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_typevar_usage_semantics_allows_legacy_container_bounds() -> None:
    """旧式 TypeVar bound 同样不能把 tuple/Callable 内部类型聚合成宽命令。"""
    source = (
        "from typing import Callable, TypeVar\n"
        "TupleBound = TypeVar(\n"
        '    "TupleBound",\n'
        "    bound=tuple[CalendarCreateCommand, CalendarUpdateCommand],\n"
        ")\n"
        "CallableBound = TypeVar(\n"
        '    "CallableBound",\n'
        "    bound=Callable[[CalendarCreateCommand], CalendarUpdateCommand],\n"
        ")\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_typevar_usage_semantics_rejects_top_level_constraints() -> None:
    """两个顶层具体命令 constraint 仍须在声明处报告首个 constraint 行。"""
    source = (
        "from typing import TypeVar\n"
        "T = TypeVar(\n"
        '    "T",\n'
        "    CalendarCreateCommand,\n"
        "    CalendarUpdateCommand,\n"
        ")\n"
    )

    assert _wide_command_union_annotation_lines(source) == (4,)


def test_generic_alias_application_expands_fixed_command_value() -> None:
    """参数化 alias 的固定命令值必须参与应用点外层 union。"""
    source = (
        "type Narrow[T] = CalendarCreateCommand\n"
        "def execute(command: Narrow[str] | CalendarUpdateCommand) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_generic_alias_application_substitutes_single_parameter() -> None:
    """单形参 alias 应以实参替换 RHS，并在应用点报告新形成的宽 union。"""
    source = (
        "type Provider[T] = CalendarCreateCommand | T\n"
        "def execute(command: Provider[CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_generic_alias_application_substitutes_multiple_parameters() -> None:
    """多形参 alias 必须按位置替换所有实参，再判断展开结果的真实 union。"""
    source = (
        "type Either[L, R] = L | R\n"
        "def execute(command: Either[CalendarCreateCommand, CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_generic_alias_application_does_not_flatten_container_result() -> None:
    """alias 展开成普通 tuple 容器时，两个命令实参仍不是直接命令 union。"""
    source = (
        "type Pair[L, R] = tuple[L, R]\n"
        "def execute(command: Pair[CalendarCreateCommand, CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_generic_base_application_reports_union_in_class_base() -> None:
    """ClassDef base 的类型参数位置属于架构边界，宽命令 union 必须报告 base 行。"""
    source = (
        "class Adapter(BaseAdapter[CalendarCreateCommand | CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (1,)


def test_class_model_expands_qualified_generic_alias() -> None:
    """类命名空间中的 PEP 695 generic alias 应在限定应用点展开。"""
    source = (
        "class Types:\n"
        "    type Provider[T] = CalendarCreateCommand | T\n"
        "def execute(command: Types.Provider[CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_class_model_expands_nested_qualified_generic_alias() -> None:
    """多层 class 属性链必须定位最终命名空间中的 generic alias。"""
    source = (
        "class Outer:\n"
        "    class Types:\n"
        "        type Provider[T] = CalendarCreateCommand | T\n"
        "def execute(command: Outer.Types.Provider[CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (4,)


def test_class_model_expands_qualified_legacy_generic_alias() -> None:
    """类内传统 TypeAlias 的隐式 TypeVar 也须支持限定参数化应用。"""
    source = (
        "from typing import TypeAlias, TypeVar\n"
        'T = TypeVar("T")\n'
        "class Types:\n"
        "    Provider: TypeAlias = CalendarCreateCommand | T\n"
        "def execute(command: Types.Provider[CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_class_model_allows_safe_qualified_generic_aliases() -> None:
    """限定 alias 展开后仅含官方 alias 或普通容器时不得误报。"""
    source = (
        "class Types:\n"
        "    type Official[T] = CalendarCommand | T\n"
        "    type Pair[L, R] = tuple[L, R]\n"
        "def official(command: Types.Official[str]) -> None: ...\n"
        "def pair(command: Types.Pair[CalendarCreateCommand, CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_class_model_preserves_pep695_bound_in_method_signature() -> None:
    """泛型类体内方法必须看到类 TypeVar 的单命令 bound。"""
    source = (
        "class Adapter[T: CalendarCreateCommand]:\n"
        "    def execute(self, command: T | CalendarUpdateCommand) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_class_model_preserves_pep695_constraints_in_field_annotation() -> None:
    """泛型类字段使用 T 时须展开顶层 constraints 的直接命令含义。"""
    source = (
        "class Adapter[T: (CalendarCreateCommand, str)]:\n"
        "    command: T | CalendarUpdateCommand\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_class_model_does_not_flatten_container_bounds_in_class_body() -> None:
    """类 TypeVar 的 tuple/Callable bound 内部命令不得提升成 T 的直接含义。"""
    source = (
        "from typing import Callable\n"
        "class TupleSafe[T: tuple[CalendarCreateCommand, CalendarUpdateCommand]]:\n"
        "    command: T\n"
        "class CallableSafe[T: Callable[[CalendarCreateCommand], CalendarUpdateCommand]]:\n"
        "    command: T\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_class_model_specializes_two_parameter_generic_base_members() -> None:
    """子类基类实参应替换 Base 方法形参，并在继承点报告新宽 union。"""
    source = (
        "class Base[L, R]:\n"
        "    def execute(self, command: L | R) -> None: ...\n"
        "class Adapter(Base[CalendarCreateCommand, CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_class_model_specializes_fixed_command_generic_base_member() -> None:
    """Base 成员的固定命令与子类实参组合成宽 union 时必须报告继承点。"""
    source = (
        "class Base[T]:\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "class Adapter(Base[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_class_model_allows_safe_generic_base_member_specialization() -> None:
    """继承特化后仍为容器、官方 alias 或单命令的成员签名保持安全。"""
    source = (
        "class Base[T]:\n"
        "    def execute(self, command: tuple[CalendarCreateCommand, T]) -> None: ...\n"
        "class Concrete(Base[CalendarUpdateCommand]):\n"
        "    pass\n"
        "class Official(Base[CalendarCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


@pytest.mark.parametrize(
    ("generic_import", "generic_expression", "expected_line"),
    (
        ("from typing import Generic, TypeVar\n", "Generic", 6),
        ("from typing_extensions import Generic, TypeVar\n", "Generic", 6),
        (
            "import typing as typing_module\nfrom typing import TypeVar\n",
            "typing_module.Generic",
            7,
        ),
        (
            (
                "import typing_extensions as typing_extensions_module\n"
                "from typing import TypeVar\n"
            ),
            "typing_extensions_module.Generic",
            7,
        ),
        ("from typing import Generic as GenericBase, TypeVar\n", "GenericBase", 6),
    ),
)
def test_legacy_generic_base_specializes_direct_member_union(
    generic_import: str,
    generic_expression: str,
    expected_line: int,
) -> None:
    """受信 Generic[T] 基类的直接成员须在子类特化点展开并报告宽 union。"""
    source = (
        generic_import
        + "from ai_employee.domain.calendar_actions import "
        + "CalendarCreateCommand, CalendarUpdateCommand\n"
        + 'T = TypeVar("T")\n'
        + f"class Base({generic_expression}[T]):\n"
        + "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        + "class Adapter(Base[CalendarUpdateCommand]):\n"
        + "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (expected_line,)


def test_legacy_generic_base_via_type_alias_specializes_direct_member() -> None:
    """传统 TypeAlias 间接包装参数化 Base 时仍须解析直接基类特化。"""
    source = (
        "from typing import Generic, TypeAlias, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T")\n'
        "class Base(Generic[T]):\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "BaseAlias: TypeAlias = Base[T]\n"
        "class Adapter(BaseAlias[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (7,)


def test_legacy_generic_base_applies_default_to_bare_subclass() -> None:
    """legacy Generic[T] 的 TypeVar default 应支持裸基类继承特化。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T", default=CalendarUpdateCommand)\n'
        "class Base(Generic[T]):\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "class Adapter(Base):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (6,)


def test_legacy_generic_base_default_uses_typevar_definition_state() -> None:
    """裸 Generic 的 default 不得被 Base 类体内后置同名绑定改写。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T", default=CalendarUpdateCommand)\n'
        "class Base(Generic[T]):\n"
        "    CalendarUpdateCommand = str\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "class Adapter(Base):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (7,)


def test_nested_legacy_generic_base_default_uses_typevar_definition_state() -> None:
    """限定 Outer.Base 的裸应用也须读取模块 TypeVar 的声明点 default。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T", default=CalendarUpdateCommand)\n'
        "class Outer:\n"
        "    class Base(Generic[T]):\n"
        "        CalendarUpdateCommand = str\n"
        "        def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "class Adapter(Outer.Base):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (8,)


def test_legacy_generic_base_partial_application_uses_trailing_default_state() -> None:
    """部分 Generic 应用须保留显式前参，并从声明点补齐尾 TypeVar default。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'L = TypeVar("L")\n'
        'R = TypeVar("R", default=CalendarUpdateCommand)\n'
        "class Base(Generic[L, R]):\n"
        "    CalendarUpdateCommand = str\n"
        "    def execute(self, command: L | R) -> None: ...\n"
        "class Adapter(Base[CalendarCreateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (8,)


def test_legacy_generic_base_paramspec_application_fills_single_trailing_default() -> None:
    """Generic[P, R] 的 ParamSpec 实参后应从声明点补齐单个尾 default。"""
    source = (
        "from typing import Callable, Generic\n"
        "from typing_extensions import ParamSpec, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'P = ParamSpec("P")\n'
        'R = TypeVar("R", default=CalendarUpdateCommand)\n'
        "class Base(Generic[P, R]):\n"
        "    def execute(self, command: Callable[P, str] | CalendarCreateCommand | R) -> None: ...\n"
        "class Adapter(Base[[int]]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (8,)


def test_legacy_generic_base_paramspec_application_fills_multiple_trailing_defaults() -> None:
    """Generic[P, R, S] 应按顺序补齐连续尾 defaults，不能丢失最后命令叶。"""
    source = (
        "from typing import Callable, Generic\n"
        "from typing_extensions import ParamSpec, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'P = ParamSpec("P")\n'
        'R = TypeVar("R", default=str)\n'
        'S = TypeVar("S", default=CalendarUpdateCommand)\n'
        "class Base(Generic[P, R, S]):\n"
        "    def execute(self, command: Callable[P, str] | CalendarCreateCommand | R | S) -> None: ...\n"
        "class Adapter(Base[[int]]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (9,)


def test_legacy_generic_base_paramspec_complete_application_overrides_defaults() -> None:
    """完整 Generic[P, R] 应用显式实参覆盖 default，保持安全。"""
    source = (
        "from typing import Callable, Generic\n"
        "from typing_extensions import ParamSpec, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'P = ParamSpec("P")\n'
        'R = TypeVar("R", default=CalendarUpdateCommand)\n'
        "class Base(Generic[P, R]):\n"
        "    def execute(self, command: Callable[P, str] | CalendarCreateCommand | R) -> None: ...\n"
        "class Adapter(Base[[int], str]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_generic_base_paramspec_missing_required_tail_fails_closed() -> None:
    """ParamSpec 后缺少无 default 尾参数时，class 应用必须保持 opaque。"""
    source = (
        "from typing import Callable, Generic\n"
        "from typing_extensions import ParamSpec, TypeVar\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand\n"
        'P = ParamSpec("P")\n'
        'R = TypeVar("R")\n'
        "class Base(Generic[P, R]):\n"
        "    def execute(self, command: Callable[P, str] | CalendarCreateCommand | R) -> None: ...\n"
        "class Adapter(Base[[int]]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_generic_base_paramspec_callable_member_remains_safe() -> None:
    """ParamSpec 仅位于 Callable 容器时，class 特化不得提升内部命令。"""
    source = (
        "from typing import Callable, Generic\n"
        "from typing_extensions import ParamSpec\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand\n"
        'P = ParamSpec("P")\n'
        "class Base(Generic[P]):\n"
        "    def execute(self, command: Callable[P, CalendarCreateCommand]) -> None: ...\n"
        "class Adapter(Base[[int]]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_generic_base_typevartuple_specialization_remains_safe() -> None:
    """TypeVarTuple 变体不应因 class 应用补 default 而产生宽 union。"""
    source = (
        "from typing import Generic, Unpack\n"
        "from typing_extensions import TypeVarTuple\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand\n"
        'Ts = TypeVarTuple("Ts")\n'
        "class Base(Generic[Ts]):\n"
        "    def execute(self, command: tuple[Unpack[Ts], CalendarCreateCommand]) -> None: ...\n"
        "class Adapter(Base[str]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


@pytest.mark.parametrize(
    "default_expression",
    (
        "CalendarUpdateCommand",
        "tuple[CalendarCreateCommand, CalendarUpdateCommand]",
    ),
)
def test_legacy_generic_base_keeps_safe_defaults_opaque(
    default_expression: str,
) -> None:
    """单命令或普通容器 default 单独应用时都不得构成宽命令 union。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        f'T = TypeVar("T", default={default_expression})\n'
        "class Base(Generic[T]):\n"
        "    def execute(self, command: T) -> None: ...\n"
        "class Adapter(Base):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_generic_base_explicit_argument_overrides_default_state() -> None:
    """显式 class 实参使用调用点含义，并覆盖同位置的 TypeVar default。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T", default=CalendarUpdateCommand)\n'
        "class Base(Generic[T]):\n"
        "    CalendarUpdateCommand = str\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "class Adapter(Base[str]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_generic_base_without_default_remains_opaque_when_bare() -> None:
    """无 default 的裸 Generic 应用必须 fail closed，不能猜测类内同名类型。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T")\n'
        "class Base(Generic[T]):\n"
        "    CalendarUpdateCommand = str\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "class Adapter(Base):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_generic_base_does_not_misrecognize_shadowed_generic() -> None:
    """普通同名 shadow 后的 Generic[T] 不得被当作受信泛型基类。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T")\n'
        "Generic = runtime_generic\n"
        "class Base(Generic[T]):\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "class Adapter(Base[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


@pytest.mark.parametrize(
    "member_annotation",
    (
        "tuple[CalendarCreateCommand, T]",
        "Callable[[CalendarCreateCommand], T]",
    ),
)
def test_legacy_generic_base_keeps_container_members_safe(
    member_annotation: str,
) -> None:
    """直接成员中的容器/Callable 参数不能因基类特化被提升为宽 union。"""
    source = (
        "from typing import Callable, Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T")\n'
        "class Base(Generic[T]):\n"
        f"    def execute(self, command: {member_annotation}) -> None: ...\n"
        "class Adapter(Base[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_generic_base_ignores_method_typevar_shadow() -> None:
    """方法自有 TypeVar shadow class 参数时不得错误继承外层替换。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T")\n'
        "class Base(Generic[T]):\n"
        '    def execute[U](self, command: CalendarCreateCommand | U) -> None: ...\n'
        "class Adapter(Base[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


@pytest.mark.parametrize(
    "adapter_base",
    (
        "Base",
        "Base[CalendarCreateCommand, CalendarUpdateCommand]",
    ),
)
def test_legacy_generic_base_keeps_incomplete_application_opaque(
    adapter_base: str,
) -> None:
    """无 default 的裸或参数过多基类应用必须保持 opaque。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T")\n'
        "class Base(Generic[T]):\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        f"class Adapter({adapter_base}):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_generic_base_propagates_pep695_members_through_one_intermediate_class() -> None:
    """PEP 695 两级继承须把最终实参传到 Base 的直接成员模板。"""
    source = (
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        "class Base[T]:\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "class Mid[T](Base[T]):\n"
        "    pass\n"
        "class Adapter(Mid[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (6,)


@pytest.mark.parametrize(
    ("generic_import", "generic_expression", "expected_line"),
    (
        ("from typing import Generic, TypeVar\n", "Generic", 8),
        ("from typing_extensions import Generic, TypeVar\n", "Generic", 8),
        (
            "import typing as typing_module\nfrom typing import TypeVar\n",
            "typing_module.Generic",
            9,
        ),
        ("from typing import Generic as G, TypeVar\n", "G", 8),
    ),
)
def test_legacy_generic_base_propagates_members_through_one_intermediate_class(
    generic_import: str,
    generic_expression: str,
    expected_line: int,
) -> None:
    """legacy Generic[T] 两级继承须沿 Base→Mid 传递直接成员替换。"""
    source = (
        generic_import
        + "from ai_employee.domain.calendar_actions import "
        + "CalendarCreateCommand, CalendarUpdateCommand\n"
        + 'T = TypeVar("T")\n'
        + f"class Base({generic_expression}[T]):\n"
        + "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        + "class Mid(Base[T]):\n"
        + "    pass\n"
        + "class Adapter(Mid[CalendarUpdateCommand]):\n"
        + "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (expected_line,)


@pytest.mark.parametrize(
    ("generic_import", "generic_expression", "base_order", "expected_line"),
    (
        (
            "from typing import Generic, TypeVar\n",
            "Generic",
            "{generic}[U], Base[U]",
            8,
        ),
        (
            "from typing_extensions import Generic, TypeVar\n",
            "Generic",
            "Base[U], {generic}[U]",
            8,
        ),
        (
            "import typing_extensions as te\nfrom typing import TypeVar\n",
            "te.Generic",
            "{generic}[U], Base[U]",
            9,
        ),
        (
            "from typing import Generic as G, TypeVar\n",
            "G",
            "Base[U], {generic}[U]",
            8,
        ),
    ),
)
def test_legacy_generic_base_merges_duplicate_generic_candidates(
    generic_import: str,
    generic_expression: str,
    base_order: str,
    expected_line: int,
) -> None:
    """Generic[U] 与 Base[U] 的重复候选须按相同映射合并，不能丢失中间层。"""
    source = (
        generic_import
        + "from ai_employee.domain.calendar_actions import "
        + "CalendarCreateCommand, CalendarUpdateCommand\n"
        + 'U = TypeVar("U")\n'
        + f"class Base({generic_expression}[U]):\n"
        + "    def execute(self, command: CalendarCreateCommand | U) -> None: ...\n"
        + f"class Mid({base_order.format(generic=generic_expression)}):\n"
        + "    pass\n"
        + "class Adapter(Mid[CalendarUpdateCommand]):\n"
        + "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (expected_line,)


def test_legacy_generic_base_merges_duplicate_candidates_through_type_alias() -> None:
    """Generic 与 TypeAlias 间接 Base 的相同参数映射也须合并。"""
    source = (
        "from typing import Generic, TypeAlias, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'U = TypeVar("U")\n'
        "class Base(Generic[U]):\n"
        "    def execute(self, command: CalendarCreateCommand | U) -> None: ...\n"
        "BaseAlias: TypeAlias = Base\n"
        "class Mid(Generic[U], BaseAlias[U]):\n"
        "    pass\n"
        "class Adapter(Mid[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (9,)


def test_legacy_generic_base_candidate_mapping_conflict_fails_closed() -> None:
    """Generic 与 Base 参数映射不同步时必须 opaque，不能凭一项候选猜测。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'U = TypeVar("U")\n'
        'V = TypeVar("V")\n'
        "class Base(Generic[U]):\n"
        "    def execute(self, command: CalendarCreateCommand | U) -> None: ...\n"
        "class Mid(Generic[U, V], Base[V]):\n"
        "    pass\n"
        "class Adapter(Mid[CalendarUpdateCommand, CalendarCreateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_generic_base_duplicate_candidates_keep_incomplete_application_opaque() -> None:
    """重复候选的中间/最终裸应用缺少实参时仍须 fail closed。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand\n"
        'U = TypeVar("U")\n'
        "class Base(Generic[U]):\n"
        "    def execute(self, command: CalendarCreateCommand | U) -> None: ...\n"
        "class Mid(Generic[U], Base[U]):\n"
        "    pass\n"
        "class Adapter(Mid):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_generic_base_propagates_through_legacy_type_alias_and_middle_class() -> None:
    """传统 TypeAlias 间接基类加中间 class 时仍须命中最终继承点。"""
    source = (
        "from typing import Generic, TypeAlias, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T")\n'
        "class Base(Generic[T]):\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "BaseAlias: TypeAlias = Base\n"
        "class Mid(BaseAlias[T]):\n"
        "    pass\n"
        "class Adapter(Mid[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (9,)


@pytest.mark.parametrize(
    "member_annotation",
    (
        "tuple[CalendarCreateCommand, T]",
        "Callable[[CalendarCreateCommand], T]",
    ),
)
def test_generic_base_multilevel_container_members_remain_safe(
    member_annotation: str,
) -> None:
    """多级特化不能把容器/Callable 内部命令提升到直接 union。"""
    source = (
        "from typing import Callable, Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T")\n'
        "class Base(Generic[T]):\n"
        f"    def execute(self, command: {member_annotation}) -> None: ...\n"
        "class Mid(Base[T]):\n"
        "    pass\n"
        "class Adapter(Mid[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_generic_base_multilevel_method_typevar_shadow_remains_safe() -> None:
    """中间层传播不能穿透直接成员方法自身的 TypeVar shadow。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T")\n'
        "class Base(Generic[T]):\n"
        '    def execute[U](self, command: CalendarCreateCommand | U) -> None: ...\n'
        "class Mid(Base[T]):\n"
        "    pass\n"
        "class Adapter(Mid[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_generic_base_multilevel_incomplete_application_is_opaque() -> None:
    """中间层或最终层缺少必需实参时必须 fail closed。"""
    source = (
        "from typing import Generic, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T")\n'
        "class Base(Generic[T]):\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "class Mid(Base[T]):\n"
        "    pass\n"
        "class Adapter(Mid):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_generic_base_multilevel_does_not_duplicate_base_wide_union() -> None:
    """Base 已有宽 union 时只报告其成员行，不在最终继承点重复报告。"""
    source = (
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        "class Base[T]:\n"
        "    def execute(self, command: CalendarCreateCommand | CalendarUpdateCommand) -> None: ...\n"
        "class Mid[T](Base[T]):\n"
        "    pass\n"
        "class Adapter(Mid[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (3,)


def test_generic_base_multilevel_cyclic_alias_fails_closed() -> None:
    """循环 TypeAlias 不得导致递归溢出或凭猜测报告继承宽 union。"""
    source = (
        "from typing import Generic, TypeAlias, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'T = TypeVar("T")\n'
        "class Base(Generic[T]):\n"
        "    def execute(self, command: CalendarCreateCommand | T) -> None: ...\n"
        "Left: TypeAlias = Right\n"
        "Right: TypeAlias = Left\n"
        "class Mid(Left[T]):\n"
        "    pass\n"
        "class Adapter(Mid[CalendarUpdateCommand]):\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_generic_alias_application_allows_official_and_single_command_arguments() -> None:
    """替换后仅含官方 alias 或单个具体命令的 generic alias 应保持安全。"""
    source = (
        "type Provider[T] = CalendarCreateCommand | T\n"
        "def official(command: Provider[CalendarCommand]) -> None: ...\n"
        "def ordinary(command: Provider[str]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_generic_alias_application_substitutes_annotated_alias() -> None:
    """传统 TypeAlias 应从 RHS 推断 TypeVar，并在应用点展开单个实参。"""
    source = (
        "from typing import TypeAlias, TypeVar\n"
        'T = TypeVar("T")\n'
        "Provider: TypeAlias = CalendarCreateCommand | T\n"
        "def execute(command: Provider[CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (4,)


def test_legacy_generic_alias_application_substitutes_multiple_parameters() -> None:
    """传统双形参 TypeAlias 必须按 RHS 首次出现顺序替换全部实参。"""
    source = (
        "from typing import TypeAlias, TypeVar\n"
        'L = TypeVar("L")\n'
        'R = TypeVar("R")\n'
        "Either: TypeAlias = L | R\n"
        "def execute(command: Either[CalendarCreateCommand, CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_legacy_generic_alias_application_preserves_first_reference_order() -> None:
    """形参顺序取 RHS 首次引用次序，不能按变量名或声明顺序重排。"""
    source = (
        "from typing import TypeAlias, TypeVar\n"
        'L = TypeVar("L")\n'
        'R = TypeVar("R")\n'
        "Ordered: TypeAlias = R | tuple[L] | CalendarCreateCommand\n"
        "def execute(command: Ordered[CalendarUpdateCommand, str]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_legacy_generic_alias_application_does_not_flatten_container_result() -> None:
    """传统泛型 alias 展开成 tuple 时，命令实参仍只是容器参数。"""
    source = (
        "from typing import TypeAlias, TypeVar\n"
        'L = TypeVar("L")\n'
        'R = TypeVar("R")\n'
        "Pair: TypeAlias = tuple[L, R]\n"
        "def execute(command: Pair[CalendarCreateCommand, CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_generic_alias_application_substitutes_assign_union_alias() -> None:
    """strict mypy 接受的传统 Assign Union alias 也必须推断隐式 TypeVar。"""
    source = (
        "from typing import TypeVar, Union\n"
        'T = TypeVar("T")\n'
        "Provider = Union[CalendarCreateCommand, T]\n"
        "def execute(command: Provider[CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (4,)


def test_legacy_generic_alias_application_supports_typing_extensions_typevar() -> None:
    """typing_extensions.TypeVar 建立的传统泛型 alias 使用相同替换语义。"""
    source = (
        "from typing import TypeAlias\n"
        "from typing_extensions import TypeVar as TV\n"
        'T = TV("T")\n'
        "Provider: TypeAlias = CalendarCreateCommand | T\n"
        "def execute(command: Provider[CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_legacy_generic_alias_application_requires_trusted_typevar_state() -> None:
    """普通同名值与官方 alias 都不能被猜测为传统 alias 的隐式形参。"""
    source = (
        "from typing import TypeAlias\n"
        "from ai_employee.application.commands import CalendarCommand as Official\n"
        "Ordinary = runtime_value\n"
        "OrdinaryAlias: TypeAlias = CalendarCreateCommand | Ordinary\n"
        "OfficialAlias: TypeAlias = CalendarCreateCommand | Official\n"
        "def ordinary(command: OrdinaryAlias[CalendarUpdateCommand]) -> None: ...\n"
        "def official(command: OfficialAlias[CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


@pytest.mark.parametrize(
    "type_var_import",
    (
        "from typing import TypeVar",
        "from typing_extensions import TypeVar",
    ),
)
def test_alias_default_model_fills_single_trailing_default(
    type_var_import: str,
) -> None:
    """受信 typing 工厂的部分 alias 应以唯一尾 default 补齐缺省实参。"""
    source = (
        "from typing import TypeAlias\n"
        f"{type_var_import}\n"
        'L = TypeVar("L")\n'
        'R = TypeVar("R", default=CalendarUpdateCommand)\n'
        "Pair: TypeAlias = L | R\n"
        "def execute(command: Pair[CalendarCreateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (6,)


def test_alias_default_model_fills_multiple_trailing_defaults_in_order() -> None:
    """多个连续尾 default 必须按形参位置补齐，不能交换默认实参。"""
    source = (
        "from typing import TypeAlias\n"
        "from typing_extensions import TypeVar\n"
        'L = TypeVar("L")\n'
        'R = TypeVar("R", default=str)\n'
        'S = TypeVar("S", default=CalendarUpdateCommand)\n'
        "Ordered: TypeAlias = L | S | tuple[R]\n"
        "def execute(command: Ordered[CalendarCreateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (7,)


def test_alias_default_model_explicit_argument_overrides_default() -> None:
    """显式实参必须覆盖对应 default，不能保留默认命令叶。"""
    source = (
        "from typing import TypeAlias\n"
        "from typing_extensions import TypeVar\n"
        'L = TypeVar("L")\n'
        'R = TypeVar("R", default=CalendarUpdateCommand)\n'
        "Pair: TypeAlias = L | R\n"
        "def execute(command: Pair[CalendarCreateCommand, str]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_alias_default_model_keeps_missing_required_parameter_opaque() -> None:
    """部分应用若仍缺少无 default 的形参，必须整体 opaque 而非猜测替换。"""
    source = (
        "from typing import TypeAlias, TypeVar\n"
        'L = TypeVar("L")\n'
        'R = TypeVar("R")\n'
        "Pair: TypeAlias = CalendarCreateCommand | L | R\n"
        "def execute(command: Pair[CalendarUpdateCommand]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_alias_default_model_uses_typevar_definition_state() -> None:
    """模块 TypeVar 的 default 不得被 alias owner 内后置同名 shadow 改写。"""
    source = (
        "from typing import TypeAlias\n"
        "from typing_extensions import TypeVar\n"
        'T = TypeVar("T", default=CalendarUpdateCommand)\n'
        "class Types:\n"
        "    Provider: TypeAlias = CalendarCreateCommand | T\n"
        "    CalendarUpdateCommand = str\n"
        "def execute(command: Types.Provider) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (7,)


def test_alias_default_model_keeps_single_and_container_defaults_safe() -> None:
    """单命令 default 与包含多命令的普通容器都不得单独构成宽 union。"""
    source = (
        "from typing import TypeAlias\n"
        "from typing_extensions import TypeVar\n"
        'L = TypeVar("L")\n'
        'R = TypeVar("R", default=tuple[CalendarCreateCommand, CalendarUpdateCommand])\n'
        "Pair: TypeAlias = L | R\n"
        'T = TypeVar("T", default=CalendarUpdateCommand)\n'
        "Single: TypeAlias = T\n"
        "def container(command: Pair[str]) -> None: ...\n"
        "def single(command: Single) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_parameter_model_applies_typevar_default_to_bare_alias() -> None:
    """裸传统泛型 alias 应使用 typing_extensions.TypeVar 的默认实参。"""
    source = (
        "from typing import TypeAlias\n"
        "from typing_extensions import TypeVar\n"
        'T = TypeVar("T", default=CalendarUpdateCommand)\n'
        "Provider: TypeAlias = CalendarCreateCommand | T\n"
        "def execute(command: Provider) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_legacy_parameter_model_combines_bound_default_with_outer_union() -> None:
    """带 bound/default 的裸 alias 应把默认单命令贡献给外层真实 union。"""
    source = (
        "from typing import TypeAlias\n"
        "from typing_extensions import TypeVar\n"
        'T = TypeVar("T", bound=CalendarCreateCommand, default=CalendarCreateCommand)\n'
        "Provider: TypeAlias = T\n"
        "def execute(command: Provider | CalendarUpdateCommand) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_legacy_parameter_model_does_not_flatten_default_container() -> None:
    """TypeVar 默认值为 tuple 容器时，内部多个命令不是 alias 的直接命令集。"""
    source = (
        "from typing import TypeAlias\n"
        "from typing_extensions import TypeVar\n"
        'T = TypeVar("T", default=tuple[CalendarCreateCommand, CalendarUpdateCommand])\n'
        "Provider: TypeAlias = T\n"
        "def execute(command: Provider) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_parameter_model_preserves_fixed_leaf_with_typevartuple() -> None:
    """TypeVarTuple alias 参数化后须保留 RHS 固定命令叶供外层 union 使用。"""
    source = (
        "from typing import TypeAlias, Unpack\n"
        "from typing_extensions import TypeVarTuple\n"
        'Ts = TypeVarTuple("Ts")\n'
        "Provider: TypeAlias = CalendarCreateCommand | tuple[Unpack[Ts]]\n"
        "def execute(command: Provider[str] | CalendarUpdateCommand) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_legacy_parameter_model_preserves_fixed_leaf_with_paramspec() -> None:
    """ParamSpec alias 的参数列表替换不得丢失 RHS 固定命令叶。"""
    source = (
        "from typing import Callable, TypeAlias\n"
        "from typing_extensions import ParamSpec\n"
        'P = ParamSpec("P")\n'
        "Provider: TypeAlias = CalendarCreateCommand | Callable[P, str]\n"
        "def execute(command: Provider[[int]] | CalendarUpdateCommand) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_legacy_parameter_model_keeps_variadic_container_aliases_safe() -> None:
    """TypeVarTuple/ParamSpec 只出现在容器内部时不得提升内部命令类型。"""
    source = (
        "from typing import Callable, TypeAlias, Unpack\n"
        "from typing_extensions import ParamSpec, TypeVarTuple\n"
        'Ts = TypeVarTuple("Ts")\n'
        'P = ParamSpec("P")\n'
        "TupleContainer: TypeAlias = tuple[CalendarCreateCommand, Unpack[Ts]]\n"
        "CallableContainer: TypeAlias = Callable[P, CalendarUpdateCommand]\n"
        "def tuple_value(command: TupleContainer[CalendarUpdateCommand]) -> None: ...\n"
        "def callable_value(command: CallableContainer[[int]]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_variadic_alias_binds_paramspec_before_trailing_default() -> None:
    """ParamSpec 位于前缀时，省略的尾 TypeVar 仍须使用声明点 default。"""
    source = (
        "from typing import Callable, TypeAlias\n"
        "from typing_extensions import ParamSpec, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'P = ParamSpec("P")\n'
        'R = TypeVar("R", default=CalendarUpdateCommand)\n'
        "Provider: TypeAlias = Callable[P, str] | CalendarCreateCommand | R\n"
        "def execute(command: Provider[[int]]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (7,)


def test_legacy_variadic_alias_fills_multiple_trailing_defaults_in_order() -> None:
    """ParamSpec 后的连续尾 default 必须按声明顺序全部补齐。"""
    source = (
        "from typing import Callable, TypeAlias\n"
        "from typing_extensions import ParamSpec, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'P = ParamSpec("P")\n'
        'R = TypeVar("R", default=str)\n'
        'S = TypeVar("S", default=CalendarUpdateCommand)\n'
        "Provider: TypeAlias = Callable[P, str] | CalendarCreateCommand | R | S\n"
        "def execute(command: Provider[[int]]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == (8,)


def test_legacy_variadic_alias_explicit_trailing_argument_overrides_default() -> None:
    """显式尾实参必须覆盖对应 default，不能把旧命令叶带入应用结果。"""
    source = (
        "from typing import Callable, TypeAlias\n"
        "from typing_extensions import ParamSpec, TypeVar\n"
        "from ai_employee.domain.calendar_actions import "
        "CalendarCreateCommand, CalendarUpdateCommand\n"
        'P = ParamSpec("P")\n'
        'R = TypeVar("R", default=CalendarUpdateCommand)\n'
        "Provider: TypeAlias = Callable[P, str] | CalendarCreateCommand | R\n"
        "def execute(command: Provider[[int], str]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_variadic_alias_keeps_callable_and_container_arguments_safe() -> None:
    """变元只存在 Callable/普通容器内部时，应用不得提升内部命令叶。"""
    source = (
        "from typing import Callable, TypeAlias, Unpack\n"
        "from typing_extensions import ParamSpec, TypeVarTuple\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand\n"
        'P = ParamSpec("P")\n'
        'Ts = TypeVarTuple("Ts")\n'
        "CallableContainer: TypeAlias = Callable[P, CalendarCreateCommand]\n"
        "TupleContainer: TypeAlias = tuple[Unpack[Ts], CalendarCreateCommand]\n"
        "def callable_value(command: CallableContainer[[int]]) -> None: ...\n"
        "def tuple_value(command: TupleContainer[str]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_legacy_variadic_alias_keeps_missing_required_tail_opaque() -> None:
    """变元后仍缺少无 default 尾形参时，应用必须 fail closed。"""
    source = (
        "from typing import Callable, TypeAlias, TypeVar\n"
        "from typing_extensions import ParamSpec\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand\n"
        'P = ParamSpec("P")\n'
        'R = TypeVar("R")\n'
        "Provider: TypeAlias = Callable[P, str] | CalendarCreateCommand | R\n"
        "def execute(command: Provider[[int]]) -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_function_type_comment_reports_wide_command_union() -> None:
    """strict mypy 接受的函数 type comment 必须进入同一签名门禁。"""
    source = (
        "def execute(command):  # type: (CalendarCreateCommand | CalendarUpdateCommand) -> None\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (1,)


def test_function_type_comment_allows_official_and_single_command_types() -> None:
    """type comment 中官方 alias、单命令与普通容器不得误报。"""
    source = (
        "def official(command):  # type: (CalendarCommand) -> None\n"
        "    pass\n"
        "def single(command):  # type: (CalendarCreateCommand) -> None\n"
        "    pass\n"
        "def container(command):  # type: (tuple[CalendarCreateCommand, CalendarUpdateCommand]) -> None\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


@pytest.mark.parametrize(
    ("parameters", "expected_line"),
    (
        (
            (
                "    command,  # type: CalendarCreateCommand | CalendarUpdateCommand\n"
                "    /,"
            ),
            2,
        ),
        (
            "    command,  # type: CalendarCreateCommand | CalendarUpdateCommand",
            2,
        ),
        (
            (
                "    *,\n"
                "    command,  # type: CalendarCreateCommand | CalendarUpdateCommand"
            ),
            3,
        ),
        (
            "    *command,  # type: CalendarCreateCommand | CalendarUpdateCommand",
            2,
        ),
        (
            "    **command,  # type: CalendarCreateCommand | CalendarUpdateCommand",
            2,
        ),
    ),
)
def test_argument_type_comment_reports_each_parameter_kind(
    parameters: str,
    expected_line: int,
) -> None:
    """位置、普通、关键字、可变位置与可变关键字 comment 都属于签名边界。"""
    source = f"def execute(\n{parameters}\n) -> None: ...\n"

    assert _wide_command_union_annotation_lines(source) == (expected_line,)


def test_argument_type_comment_allows_safe_types() -> None:
    """参数 comment 中官方 alias、单命令与普通容器不得误报。"""
    source = (
        "def official(\n"
        "    command,  # type: CalendarCommand\n"
        ") -> None: ...\n"
        "def single(\n"
        "    command,  # type: CalendarCreateCommand\n"
        ") -> None: ...\n"
        "def container(\n"
        "    command,  # type: tuple[CalendarCreateCommand, CalendarUpdateCommand]\n"
        ") -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_argument_type_comment_treats_invalid_syntax_as_opaque() -> None:
    """无效 comment 不得被部分解析或导致崩溃；strict mypy 负责语法拒绝。"""
    source = (
        "def execute(\n"
        "    command,  # type: CalendarCreateCommand |\n"
        ") -> None: ...\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_allows_safe_generic_parameter_forms() -> None:
    """官方 alias、Optional、单命令约束及 metadata 都不得误报成宽命令集合。"""
    source = (
        "from typing import Annotated, Literal, TypeVar as TV\n"
        "import typing\n"
        "type OptionalAlias[T: CalendarCreateCommand | None] = T\n"
        "class SingleCommand[T: (CalendarCreateCommand, str)]:\n"
        "    pass\n"
        "type OfficialAlias[T: CalendarCommand | TrustedCommand] = T\n"
        'LegacyOptional = TV("LegacyOptional", bound=CalendarUpdateCommand | None)\n'
        'LegacyMixed = TV("LegacyMixed", CalendarCreateCommand, str)\n'
        'LegacyOfficial = typing.TypeVar("LegacyOfficial", CalendarCommand, TrustedCommand)\n'
        'LiteralData = TV("LiteralData", bound=Literal["CalendarCreateCommand | CalendarUpdateCommand"])\n'
        'AnnotatedData = typing.TypeVar("AnnotatedData", bound=typing.Annotated[str, "CalendarCreateCommand | CalendarUpdateCommand"])\n'
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_ignores_runtime_calls_and_shadowed_typevar() -> None:
    """只有可证明来自 typing 的 TypeVar 才可开放参数检查，普通 Call 与 shadow 忽略。"""
    source = (
        "from typing import TypeVar as TV\n"
        "import typing\n"
        'Runtime = Factory("Runtime", CalendarCreateCommand, CalendarUpdateCommand)\n'
        "TV = runtime_type_var\n"
        'ShadowedAlias = TV("ShadowedAlias", CalendarCreateCommand, CalendarUpdateCommand)\n'
        "typing = runtime_typing\n"
        'ShadowedModule = typing.TypeVar("ShadowedModule", CalendarCreateCommand, CalendarUpdateCommand)\n'
        'Unimported = TypeVar("Unimported", CalendarCreateCommand, CalendarUpdateCommand)\n'
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_respects_pep695_parameter_name_shadow() -> None:
    """类型参数可合法 shadow 外层命令名，签名、类字段与 alias 值都须使用局部名。"""
    source = (
        "def execute[CalendarCreateCommand](\n"
        "    command: CalendarCreateCommand | CalendarUpdateCommand,\n"
        ") -> None:\n"
        "    pass\n"
        "class Adapter[CalendarCreateCommand]:\n"
        "    command: CalendarCreateCommand | CalendarUpdateCommand\n"
        "type Alias[CalendarCreateCommand] = CalendarCreateCommand | CalendarUpdateCommand\n"
    )

    assert _wide_command_union_annotation_lines(source) == ()


def test_wide_command_union_detector_preserves_generic_alias_command_expansion() -> None:
    """泛型 alias 的固定命令值仍须可展开，类型参数 frame 不能让既有检测退化。"""
    source = (
        "type Narrow[T] = CalendarCreateCommand\n"
        "def execute(command: Narrow | CalendarUpdateCommand) -> None:\n"
        "    pass\n"
    )

    assert _wide_command_union_annotation_lines(source) == (2,)


def test_wide_command_union_detector_resolves_legacy_typevar_in_class_frame() -> None:
    """类内 TypeVar bound 须同时解析后置类 alias 与类定义之后的模块 import。"""
    source = (
        "from typing import TypeVar as TV\n"
        "class Adapter:\n"
        "    Wide = TV(\n"
        '        "Wide",\n'
        "        bound=Local | Update,\n"
        "    )\n"
        "    type Local = Create\n"
        "from ai_employee.domain.calendar_actions import CalendarCreateCommand as Create, CalendarUpdateCommand as Update\n"
    )

    assert _wide_command_union_annotation_lines(source) == (5,)


def test_wide_command_union_detector_preserves_typevar_unknown_path_correlation() -> None:
    """TypeVar constraints 必须逐路径聚合互斥 alias，并在任一路径真实变宽时报告。"""
    source = (
        "from typing import TypeVar\n"
        "if runtime_flag:\n"
        "    type Left = CalendarCreateCommand\n"
        "    type Right = str\n"
        "else:\n"
        "    type Left = str\n"
        "    type Right = CalendarUpdateCommand\n"
        "Correlated = TypeVar(\n"
        '    "Correlated",\n'
        "    Left,\n"
        "    Right,\n"
        ")\n"
        "Violating = TypeVar(\n"
        '    "Violating",\n'
        "    Left,\n"
        "    CalendarRestoreCommand,\n"
        ")\n"
    )

    assert _wide_command_union_annotation_lines(source) == (15,)


def test_adapter_boundaries_do_not_rebuild_public_command_unions() -> None:
    """适配器边界签名必须复用公开 alias，防止未来静默扩宽命令联合。"""
    violations: dict[str, tuple[int, ...]] = {}
    for directory in ADAPTER_BOUNDARY_DIRECTORIES:
        for path in directory.rglob("*.py"):
            # 保留 ``__init__`` 段可让 helper 用统一的“去掉最后一段”规则取得包上下文。
            scanned_module_name = ".".join(
                path.relative_to(BACKEND_SOURCE.parent).with_suffix("").parts
            )
            lines = _wide_command_union_annotation_lines(
                path.read_text(encoding="utf-8"),
                module_name=scanned_module_name,
            )
            if lines:
                violations[str(path.relative_to(BACKEND_SOURCE))] = lines

    assert violations == {}


def test_canonical_json_and_hash_ignore_mapping_key_order() -> None:
    """相同载荷的对象键插入顺序不得改变审批绑定哈希。"""
    payload = _mail_payload()
    reversed_payload = dict(reversed(tuple(payload.items())))

    assert canonical_command_json(payload) == canonical_command_json(reversed_payload)
    assert trusted_command_hash(payload) == trusted_command_hash(reversed_payload)


def test_canonical_json_contains_protocol_identity_and_unescaped_unicode() -> None:
    """动作名、Schema 版本和完整 Unicode 载荷都必须进入 SHA-256 输入。"""
    canonical = canonical_command_json(_mail_payload())
    canonical_text = canonical.decode("utf-8")

    assert isinstance(canonical, bytes)
    assert canonical == canonical_text.encode("utf-8")
    assert '"action":"mail.send"' in canonical_text
    assert '"schema_version":"mail_send.v1"' in canonical_text
    assert "Synthetic 会议" in canonical_text
    assert "\\u4f1a" not in canonical_text
    assert trusted_command_hash(_mail_payload()) == hashlib.sha256(canonical).hexdigest()


def test_mail_canonical_json_and_hash_match_frozen_golden_vector() -> None:
    """邮件黄金向量锁定 Unicode、UTC datetime、地址归一与完整审批哈希。"""
    payload = _mail_payload(
        draft_version=7,
        message_date="2026-08-06T01:30:00Z",
        to=["Owner.Name+tag@Example.TEST", "Owner.Name+tag@example.test"],
        cc=["other(comment)@Example.TEST"],
        subject="审批会议 ✅",
        body_text="第一行\nSecond line",
    )
    expected_canonical = (
        b'{"action":"mail.send","bcc":[],"body_text":"\xe7\xac\xac\xe4\xb8\x80\xe8\xa1\x8c\\nSecond line",'
        b'"cc":["other@example.test"],"connection_id":"00000000-0000-0000-0000-000000000022",'
        b'"draft_id":"00000000-0000-0000-0000-000000000023","draft_version":7,'
        b'"message_date":"2026-08-06T01:30:00Z","mode":"new",'
        b'"operation_id":"00000000-0000-0000-0000-000000000021",'
        b'"schema_version":"mail_send.v1","source_message_id":null,"source_thread_id":null,'
        b'"subject":"\xe5\xae\xa1\xe6\x89\xb9\xe4\xbc\x9a\xe8\xae\xae \xe2\x9c\x85","thread_headers":null,'
        b'"to":["Owner.Name+tag@example.test"]}'
    )
    expected_hash = "928c4360eb80deac3d40930925e9812ba580c7eb7f6a22a3f403dd12820554f8"

    assert canonical_command_json(payload) == expected_canonical
    assert trusted_command_hash(payload) == expected_hash


def test_all_day_calendar_canonical_json_and_hash_match_frozen_golden_vector() -> None:
    """全天日历黄金向量锁定纯 date、Unicode、参会人归一与完整审批哈希。"""
    payload = _calendar_payload(
        "calendar.create",
        client_event_id="a00000000000000000000000044",
        title="全天评审",
        description="固定日期",
        location="上海",
        starts_at="2026-08-06",
        ends_at="2026-08-08",
        all_day=True,
        attendees=["owner(comment)@Example.TEST", "owner@example.test"],
        notification_policy="none",
    )
    expected_canonical = (
        b'{"action":"calendar.create","all_day":true,"attendees":["owner@example.test"],'
        b'"calendar_id":"primary-synthetic","client_event_id":"a00000000000000000000000044",'
        b'"connection_id":"00000000-0000-0000-0000-000000000022",'
        b'"description":"\xe5\x9b\xba\xe5\xae\x9a\xe6\x97\xa5\xe6\x9c\x9f","ends_at":"2026-08-08",'
        b'"location":"\xe4\xb8\x8a\xe6\xb5\xb7","notification_policy":"none",'
        b'"operation_id":"00000000-0000-0000-0000-000000000021",'
        b'"schema_version":"calendar_create.v1","starts_at":"2026-08-06",'
        b'"timezone":"Asia/Shanghai","title":"\xe5\x85\xa8\xe5\xa4\xa9\xe8\xaf\x84\xe5\xae\xa1"}'
    )
    expected_hash = "d8356b17a7f2cdda7a31d850c5e19393e99ac7e1c47c5cba4f7ee3d14d70ca96"

    assert canonical_command_json(payload) == expected_canonical
    assert trusted_command_hash(payload) == expected_hash


def test_hash_is_stable_across_fresh_python_process_and_key_order() -> None:
    """规范化输出不得依赖当前解释器对象顺序或进程内状态。"""
    payload = dict(reversed(tuple(_mail_payload().items())))
    encoded_payload = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    script = (
        "import json\n"
        "from ai_employee.application.commands import trusted_command_hash\n"
        f"payload = json.loads({encoded_payload!r})\n"
        "print(trusted_command_hash(payload))\n"
    )
    environment = os.environ.copy()
    backend_src = Path(__file__).resolve().parents[3] / "src"
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(backend_src), environment.get("PYTHONPATH", "")) if path
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.stderr == ""
    assert completed.stdout.strip() == trusted_command_hash(_mail_payload())


def test_canonical_boundary_rejects_nan_and_unvalidated_extra_fields() -> None:
    """非标准 JSON 数值和未进入严格 Schema 的字段都不能被规范化或哈希。"""
    nan_payload = _mail_payload(provider_extension=float("nan"))
    with pytest.raises(ValueError):
        canonical_command_json(nan_payload)

    with pytest.raises(TrustedCommandValidationError):
        canonical_command_json(_mail_payload(provider_extension={"synthetic": True}))


@pytest.mark.parametrize(
    "entrypoint",
    (parse_trusted_command, canonical_command_json, trusted_command_hash),
)
def test_public_validation_errors_are_stable_and_do_not_expose_input(
    entrypoint: object,
) -> None:
    """三个公开入口必须丢弃 Pydantic 细节，只暴露固定、可序列化且脱敏的错误。"""
    marker = "SYNTHETIC-SENSITIVE-MARKER"
    payload = _mail_payload(body_text=marker, provider_extension=marker)

    with pytest.raises(ValueError) as captured:
        entrypoint(payload)  # type: ignore[operator]

    error = captured.value
    public_error = json.dumps(
        {
            "type": type(error).__name__,
            "message": str(error),
            "repr": repr(error),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert type(error).__name__ == "TrustedCommandValidationError"
    assert str(error) == "trusted command validation failed"
    assert marker not in public_error
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    "entrypoint",
    (parse_trusted_command, canonical_command_json, trusted_command_hash),
)
@pytest.mark.parametrize("surrogate_location", ("value", "key"))
def test_public_boundary_rejects_unpaired_surrogates_without_exposing_input(
    entrypoint: object,
    surrogate_location: str,
) -> None:
    """字符串值或对象键含代理码位时，三个入口都只能暴露固定脱敏错误。"""
    marker = "SYNTHETIC-SENSITIVE-SURROGATE-MARKER"
    payload = _mail_payload()
    if surrogate_location == "value":
        payload["body_text"] = f"{marker}\ud800"
    else:
        payload[f"{marker}\udfff"] = "synthetic"

    with pytest.raises(TrustedCommandValidationError) as captured:
        entrypoint(payload)  # type: ignore[operator]

    error = captured.value
    public_error = repr(
        (
            type(error).__name__,
            str(error),
            repr(error),
            error.args,
            error.__cause__,
            error.__context__,
        )
    )
    assert str(error) == "trusted command validation failed"
    assert marker not in public_error
    assert error.args == ("trusted command validation failed",)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_json_boundary_rejects_python_only_values() -> None:
    """Mapping 入参仍须是真正 JSON 值，不能让 UUID 对象绕过线格式校验。"""
    with pytest.raises(TypeError):
        parse_trusted_command(_mail_payload(operation_id=OPERATION_ID))


@pytest.mark.parametrize("container_kind", ("mapping", "list"))
def test_json_boundary_rejects_recursive_containers_without_leaking_input(
    container_kind: str,
) -> None:
    """循环 dict/list 必须产生固定脱敏错误，不能泄漏 marker 或冒泡 RecursionError。"""
    marker = "SYNTHETIC-SENSITIVE-CYCLE-MARKER"
    if container_kind == "mapping":
        recursive_mapping: dict[str, object] = {}
        recursive_mapping[marker] = recursive_mapping
        recursive: object = recursive_mapping
    else:
        recursive_list: list[object] = [marker]
        recursive_list.append(recursive_list)
        recursive = recursive_list

    with pytest.raises(ValueError) as captured:
        parse_trusted_command(_mail_payload(body_text=recursive))

    assert type(captured.value) is ValueError
    assert str(captured.value) == "trusted command JSON containers must not contain cycles"
    assert marker not in repr(captured.value)


def test_json_boundary_allows_shared_nonrecursive_child_containers() -> None:
    """同一列表可被多个字段共享；路径检测不能把普通对象图误判为循环。"""
    shared_recipients = ["owner@example.test"]
    command = parse_trusted_command(
        _mail_payload(to=shared_recipients, cc=shared_recipients)
    )

    assert isinstance(command, MailSendCommand)
    assert command.to == ("owner@example.test",)
    assert command.cc == ()


@pytest.mark.parametrize("extra_field", ("from", "headers", "provider_extension"))
def test_mail_schema_forbids_from_arbitrary_headers_and_extensions(extra_field: str) -> None:
    """发件地址、任意 Header 容器和供应商扩展均不属于 M2 邮件命令。"""
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(_mail_payload(**{extra_field: "synthetic"}))


@pytest.mark.parametrize("malformed_kind", ("address", "message_id"))
def test_mail_parser_internal_failures_become_sanitized_application_errors(
    malformed_kind: str,
) -> None:
    """收件地址或回复头触发 stdlib 内部异常时，应用入口只能返回固定错误。"""
    if malformed_kind == "address":
        payload = _mail_payload(to=["a@["])
    else:
        payload = _reply_payload(
            thread_headers={
                "in_reply_to": "<J@[>",
                "references": ["<J@[>"],
            }
        )

    with pytest.raises(TrustedCommandValidationError) as captured:
        parse_trusted_command(payload)

    assert str(captured.value) == "trusted command validation failed"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_reply_headers_schema_forbids_arbitrary_mime_header_names() -> None:
    """回复头只能包含 in_reply_to 与 references，不能夹带任意 MIME Header。"""
    payload = _reply_payload()
    headers = dict(payload["thread_headers"])  # type: ignore[arg-type]
    headers["reply_to"] = "injected@example.test"
    payload["thread_headers"] = headers

    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(payload)


@pytest.mark.parametrize("mode", ("reply", "reply_all"))
@pytest.mark.parametrize("missing_field", ("source_thread_id", "source_message_id", "thread_headers"))
def test_reply_schema_requires_complete_source_binding(mode: str, missing_field: str) -> None:
    """回复类载荷缺少任一来源事实时必须在 Pydantic 边界失败。"""
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(_reply_payload(mode=mode, **{missing_field: None}))


@pytest.mark.parametrize("bound_field", ("source_thread_id", "source_message_id", "thread_headers"))
def test_new_schema_rejects_reply_binding(bound_field: str) -> None:
    """新邮件不得伪装携带源线程、源邮件或回复引用头。"""
    value: object
    if bound_field == "thread_headers":
        value = {
            "in_reply_to": "<source@example.test>",
            "references": ["<source@example.test>"],
        }
    else:
        value = "synthetic-source"

    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(_mail_payload(**{bound_field: value}))


@pytest.mark.parametrize(
    "overrides",
    (
        {"draft_version": 0},
        {"draft_version": True},
        {"message_date": "2026-08-06T09:30:00"},
        {"subject": "会" * 256},
        {"subject": "Synthetic\r\nBcc: injected@example.test"},
        {"body_text": "文" * 100_001},
        {"to": ["owner@example.test\r\nBcc: injected@example.test"]},
    ),
)
def test_mail_schema_rejects_invalid_versions_dates_text_and_headers(
    overrides: dict[str, object],
) -> None:
    """草稿版本、日期、文本长度和 Header 注入约束必须在 JSON 边界生效。"""
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(_mail_payload(**overrides))


@pytest.mark.parametrize(
    "message_date",
    (
        "2030-01-01T00:00Z",
        "2030-01-01T00:00:00+0000",
        "2030-01-01 00:00:00Z",
        "2030-01-01t00:00:00z",
        "2030-01-01T00:00:00+01:60",
        "2030-01-01T00:00:00+24:00",
        "2030-01-01T00:00:00.1234567Z",
        "2030-01-01T00:00:00-00:00",
        "2030-01-01T00:00:60Z",
    ),
)
def test_mail_schema_rejects_pydantic_lenient_non_rfc3339_dates(message_date: str) -> None:
    """原始 JSON 日期必须使用大写 T、秒和带冒号 offset，不能依赖宽松解析。"""
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(_mail_payload(message_date=message_date))


@pytest.mark.parametrize(
    "message_date",
    (
        "2030-01-01T00:00:00Z",
        "2030-01-01T00:00:00.123456Z",
        "2030-01-01T08:00:00+08:00",
        "2030-01-01T23:59:59.999999+23:59",
    ),
)
def test_mail_schema_accepts_strict_aware_rfc3339_dates(message_date: str) -> None:
    """带秒、可选小数秒以及 Z 或冒号 offset 的 RFC3339 日期应被接受。"""
    command = parse_trusted_command(_mail_payload(message_date=message_date))

    assert isinstance(command, MailSendCommand)
    assert command.message_date.utcoffset() is not None


@pytest.mark.parametrize("fraction", ("1234567", "1234568"))
@pytest.mark.parametrize("entrypoint", (canonical_command_json, trusted_command_hash))
def test_submicrosecond_mail_datetime_is_rejected_before_canonicalization(
    fraction: str,
    entrypoint: object,
) -> None:
    """超过微秒精度的不同值必须分别拒绝，不能截断后绑定到同一审批哈希。"""
    with pytest.raises(TrustedCommandValidationError):
        entrypoint(  # type: ignore[operator]
            _mail_payload(message_date=f"2030-01-01T00:00:00.{fraction}Z")
        )


def test_mail_schema_counts_unique_recipients_across_to_cc_bcc() -> None:
    """跨字段规范化去重后必须至少一人且最多五十人。"""
    command = parse_trusted_command(
        _mail_payload(
            to=["owner@Example.test"],
            cc=["owner(comment)@example.test", "second@example.test"],
            bcc=['"owner"@example.test', "second@EXAMPLE.TEST", "third@example.test"],
        )
    )
    assert isinstance(command, MailSendCommand)
    assert command.to == ("owner@example.test",)
    assert command.cc == ("second@example.test",)
    assert command.bcc == ("third@example.test",)

    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(_mail_payload(to=[], cc=[], bcc=[]))
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(
            _mail_payload(to=[f"recipient-{index}@example.test" for index in range(51)])
        )


@pytest.mark.parametrize(
    "extra_field",
    ("recurrence", "conference", "attachments", "provider_extension"),
)
def test_calendar_schema_forbids_out_of_scope_provider_fields(extra_field: str) -> None:
    """重复规则、会议链接、附件和供应商扩展不得进入日历命令联合。"""
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(_calendar_payload("calendar.create", **{extra_field: []}))


@pytest.mark.parametrize(
    "overrides",
    (
        {"starts_at": "2026-08-06T01:00:00", "ends_at": "2026-08-06T02:00:00Z"},
        {"starts_at": "2026-08-06T01:00:00Z", "ends_at": "2026-08-06T01:00:00Z"},
        {"starts_at": "2026-08-06", "ends_at": "2026-08-07"},
        {"timezone": "Not/A_Real_Zone"},
    ),
)
def test_timed_calendar_schema_rejects_invalid_or_mixed_time_values(
    overrides: dict[str, object],
) -> None:
    """定时事件必须使用带时区且严格递增的 datetime。"""
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(_calendar_payload("calendar.create", **overrides))


@pytest.mark.parametrize(
    ("starts_at", "ends_at"),
    (
        ("0001-01-01T00:00:00+14:00", "0001-01-01T01:00:00+14:00"),
        ("9999-12-31T22:00:00-14:00", "9999-12-31T23:00:00-14:00"),
    ),
)
def test_timed_calendar_schema_sanitizes_utc_conversion_overflow(
    starts_at: str,
    ends_at: str,
) -> None:
    """RFC3339 极值的 UTC 转换越界必须成为固定应用验证错误。"""
    with pytest.raises(TrustedCommandValidationError) as captured:
        parse_trusted_command(
            _calendar_payload(
                "calendar.create",
                starts_at=starts_at,
                ends_at=ends_at,
            )
        )

    assert str(captured.value) == "trusted command validation failed"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("starts_at", "2026-08-06T01:00Z"),
        ("ends_at", "2026-08-06T02:00Z"),
        ("starts_at", "2026-08-06T01:00:00+0000"),
        ("ends_at", "2026-08-06T02:00:00+0000"),
        ("starts_at", "2026-08-06 01:00:00Z"),
        ("ends_at", "2026-08-06 02:00:00Z"),
        ("starts_at", "2026-08-06T01:00:00+01:60"),
        ("ends_at", "2026-08-06T02:00:00+24:00"),
        ("starts_at", "2026-08-06T01:00:00.1234567Z"),
        ("ends_at", "2026-08-06T02:00:00-00:00"),
        ("starts_at", "2026-08-06T01:00:60Z"),
    ),
)
def test_timed_calendar_schema_rejects_pydantic_lenient_non_rfc3339_values(
    field_name: str,
    value: str,
) -> None:
    """定时日程起止值必须包含完整秒、T 和带冒号 offset 或 Z。"""
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(
            _calendar_payload("calendar.create", **{field_name: value})
        )


def test_timed_calendar_schema_accepts_strict_rfc3339_fraction_and_offset() -> None:
    """定时日程应接受六位小数秒与 RFC3339 允许的最大数字 offset。"""
    command = parse_trusted_command(
        _calendar_payload(
            "calendar.create",
            starts_at="2026-08-06T01:00:00.123456+23:59",
            ends_at="2026-08-06T02:00:00.654321+23:59",
        )
    )

    assert isinstance(command, CalendarCreateCommand)
    assert command.starts_at < command.ends_at


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("starts_at", date(2026, 8, 6)),
        ("ends_at", datetime(2026, 8, 6, 2, 0, tzinfo=UTC)),
    ),
)
def test_calendar_json_boundary_rejects_python_date_and_datetime_values(
    field_name: str,
    value: object,
) -> None:
    """调用方不能用 Python 时间对象绕过标准 JSON 线格式和原始字符串校验。"""
    with pytest.raises(TypeError):
        parse_trusted_command(
            _calendar_payload("calendar.create", **{field_name: value})
        )


@pytest.mark.parametrize(
    ("starts_at", "ends_at"),
    (
        ("2026-8-06", "2026-08-07"),
        ("2026-08-06", "2026-8-07"),
        ("20260806", "2026-08-07"),
    ),
)
def test_all_day_calendar_schema_rejects_noncanonical_iso_dates(
    starts_at: str,
    ends_at: str,
) -> None:
    """全天日程起止值只能使用固定宽度的 ``YYYY-MM-DD`` 日期文本。"""
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(
            _calendar_payload(
                "calendar.create",
                all_day=True,
                starts_at=starts_at,
                ends_at=ends_at,
            )
        )


def test_all_day_calendar_schema_requires_exclusive_pure_dates() -> None:
    """全天事件接受纯日期及 exclusive end，并拒绝 datetime 混入。"""
    command = parse_trusted_command(
        _calendar_payload(
            "calendar.create",
            all_day=True,
            starts_at="2026-08-06",
            ends_at="2026-08-07",
        )
    )
    assert isinstance(command, CalendarCreateCommand)
    assert command.all_day is True

    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(
            _calendar_payload(
                "calendar.create",
                all_day=True,
                starts_at="2026-08-06",
                ends_at="2026-08-06",
            )
        )
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(
            _calendar_payload(
                "calendar.create",
                all_day=True,
                starts_at="2026-08-06T01:00:00Z",
                ends_at="2026-08-07",
            )
        )


def test_calendar_attendees_are_normalized_deduplicated_and_limited() -> None:
    """日历边界按域名规范化参会人并在去重后限制最多五十人。"""
    command = parse_trusted_command(
        _calendar_payload(
            "calendar.create",
            attendees=[
                "owner@Example.test",
                "owner(comment)@example.TEST",
                '"owner"@example.test',
                "Owner@example.test",
            ],
        )
    )
    assert isinstance(command, CalendarCreateCommand)
    assert command.attendees == ("owner@example.test", "Owner@example.test")

    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(
            _calendar_payload(
                "calendar.create",
                attendees=[f"attendee-{index}@example.test" for index in range(51)],
            )
        )


def test_calendar_create_requires_frozen_client_event_id() -> None:
    """创建命令必须携带与 operation_id 对应的稳定 client_event_id。"""
    payload = _calendar_payload("calendar.create")
    del payload["client_event_id"]
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(payload)

    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(
            _calendar_payload("calendar.create", client_event_id="a0123456789")
        )


@pytest.mark.parametrize("action", ("calendar.update", "calendar.restore"))
@pytest.mark.parametrize(
    "missing_field",
    ("provider_event_id", "base_etag", "before_snapshot_id", "changed_fields"),
)
def test_calendar_update_and_restore_require_concurrency_and_snapshot_binding(
    action: str,
    missing_field: str,
) -> None:
    """修改与恢复缺少事件、ETag、快照或确定性差异时必须拒绝。"""
    payload = _calendar_payload(action)
    del payload[missing_field]

    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(payload)


@pytest.mark.parametrize("action", ("calendar.update", "calendar.restore"))
@pytest.mark.parametrize(
    "changed_fields",
    (
        [],
        ["title", "description"],
        ["title", "title"],
        ["provider_extension"],
    ),
)
def test_calendar_changed_fields_must_be_nonempty_sorted_unique_and_known(
    action: str,
    changed_fields: list[str],
) -> None:
    """changed_fields 只允许确定性描述显式完整期望状态。"""
    with pytest.raises(TrustedCommandValidationError):
        parse_trusted_command(
            _calendar_payload(action, changed_fields=changed_fields)
        )
