"""定义 CalendarEvent 字段 AAD 0019 迁移的冻结双阶段守卫。"""

from collections.abc import Callable, Mapping
from contextlib import suppress
from inspect import (
    isasyncgen,
    isasyncgenfunction,
    iscoroutine,
    iscoroutinefunction,
    isgenerator,
    isgeneratorfunction,
    signature,
)
from typing import Literal, NoReturn, Protocol, runtime_checkable

CalendarAadMigrationPhase = Literal["before_mutation", "before_commit"]

CALENDAR_AAD_0019_GUARD_ATTRIBUTE = "calendar_aad_0019_guard"
_INVARIANT_ERROR_MESSAGE = "calendar AAD migration invariant violation"
_SIGNATURE_CONNECTION_SENTINEL = object()
_AFFECTED_SET_EXISTS_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM public.calendar_events AS event
    WHERE (
        event.description_ciphertext IS NOT NULL
        AND event.description_nonce IS NOT NULL
        AND event.description_key_version IS NOT NULL
    ) OR (
        event.location_ciphertext IS NOT NULL
        AND event.location_nonce IS NOT NULL
        AND event.location_key_version IS NOT NULL
    )
) AS affected_set_exists
"""


class CalendarAadMigrationInvariantError(RuntimeError):
    """表示 0019 迁移守卫或本地可恢复事实违反冻结不变量。

    异常消息固定且不包含连接、日历、事件或凭据事实，允许迁移入口安全记录失败类别。
    """


def calendar_aad_migration_invariant_error() -> NoReturn:
    """抛出唯一稳定的 Calendar AAD 迁移不变量错误。"""
    raise CalendarAadMigrationInvariantError(_INVARIANT_ERROR_MESSAGE)


def validate_calendar_aad_migration_guard_result(result: object) -> None:
    """要求双阶段 guard 的同步调用严格返回 ``None``。

    Args:
        result: ``CalendarAadMigrationGuard.verify`` 的实际同步调用结果。

    Raises:
        CalendarAadMigrationInvariantError: 结果不是严格的 ``None``。

    该函数不执行 deferred result。coroutine 和同步 generator 可用同步 ``close`` 收回；
    cleanup 的普通异常会在抛固定错误前清除，避免敏感异常链越过迁移边界。async-generator
    只有需要 await 的 ``aclose``，因此这里不创建或驱动事件循环，也不执行其惰性正文；资源仍由
    原异步 owner 或 GC 负责。任何非 ``None`` 值最终都 fail closed。
    """
    if result is None:
        return
    if isasyncgen(result):
        # 同步 Alembic callback 不能安全执行任意异步清理；显式拒绝并保留原 owner 的关闭责任。
        calendar_aad_migration_invariant_error()
    if iscoroutine(result) or isgenerator(result):
        # ``suppress(Exception)`` 不捕获进程退出等 BaseException，且先退出异常处理上下文，
        # 再在下方抛固定 invariant，避免 cleanup 原文进入 ``__context__`` 或 traceback。
        with suppress(Exception):
            result.close()
    calendar_aad_migration_invariant_error()


def invoke_calendar_aad_migration_guard(callback: Callable[[], object]) -> None:
    """立即执行同一 guard 的冻结调用，并严格处置其实际返回值。

    Args:
        callback: 只封装当前调用点同一 guard 实例 ``verify`` 调用的零参数 callback。

    Raises:
        CalendarAadMigrationInvariantError: guard 实际返回值不是严格的 ``None``。

    本 helper 不捕获 callback 正文异常、不延迟执行，也不替换 guard identity；使用 callback
    仅为了在保留公开 ``verify -> None`` 契约时观察运行时违约返回值。
    """
    validate_calendar_aad_migration_guard_result(callback())


@runtime_checkable
class CalendarAadMigrationScalarResult(Protocol):
    """描述 strict-empty 查询只需消费的唯一标量结果。"""

    def scalar_one(self) -> object:
        """返回查询的唯一标量；调用方仍会严格验证其为真实布尔值。"""


@runtime_checkable
class CalendarAadMigrationConnection(Protocol):
    """描述守卫在同一外层迁移连接上所需的最小执行能力。"""

    def exec_driver_sql(self, statement: str) -> CalendarAadMigrationScalarResult:
        """在当前真实事务中执行固定驱动 SQL，不创建或切换连接。"""


@runtime_checkable
class CalendarAadMigrationConfig(Protocol):
    """描述 resolver 唯一允许读取的结构化配置投影。"""

    @property
    def attributes(self) -> Mapping[str, object]:
        """返回调用方显式注入的属性映射；不得从环境或其他配置面补值。"""


@runtime_checkable
class CalendarAadMigrationGuard(Protocol):
    """在同一 Alembic transaction 上验证 0019 的两个冻结边界。"""

    def verify(
        self,
        *,
        connection: CalendarAadMigrationConnection,
        phase: CalendarAadMigrationPhase,
    ) -> None:
        """拒绝不满足 strict-empty 或后续 artifact-bound rollout 的迁移。"""


class _StrictEmptyCalendarAadMigrationGuard:
    """只允许历史完整敏感字段 affected set 严格为空的内建守卫。"""

    def verify(
        self,
        *,
        connection: CalendarAadMigrationConnection,
        phase: CalendarAadMigrationPhase,
    ) -> None:
        """在两个阶段都以同一连接重新证明 affected set 仍严格为空。

        Args:
            connection: 当前 Alembic 外层事务绑定的同步连接。
            phase: 仅允许 mutation 前或最终提交前两个冻结阶段。

        Raises:
            CalendarAadMigrationInvariantError: 连接、阶段或 affected set 不满足契约。
        """
        if not isinstance(connection, CalendarAadMigrationConnection) or phase not in (
            "before_mutation",
            "before_commit",
        ):
            calendar_aad_migration_invariant_error()
        result = connection.exec_driver_sql(_AFFECTED_SET_EXISTS_SQL)
        if not isinstance(result, CalendarAadMigrationScalarResult):
            calendar_aad_migration_invariant_error()
        affected_set_exists = result.scalar_one()
        if type(affected_set_exists) is not bool or affected_set_exists:
            calendar_aad_migration_invariant_error()


def resolve_calendar_aad_migration_guard(
    config: CalendarAadMigrationConfig,
) -> CalendarAadMigrationGuard:
    """从唯一 Config attribute 解析 typed guard，缺失时返回 strict-empty 守卫。

    Args:
        config: 当前 Alembic 配置；不得从环境变量或其他 attribute 推断豁免。

    Returns:
        调用方注入的同一 typed 实例，或新的内建 strict-empty 实例。

    Raises:
        CalendarAadMigrationInvariantError: Config 或已提供对象不满足冻结协议。
    """
    if not isinstance(config, CalendarAadMigrationConfig):
        calendar_aad_migration_invariant_error()
    attributes = config.attributes
    if not isinstance(attributes, Mapping):
        calendar_aad_migration_invariant_error()
    if CALENDAR_AAD_0019_GUARD_ATTRIBUTE not in attributes:
        return _StrictEmptyCalendarAadMigrationGuard()
    guard = attributes[CALENDAR_AAD_0019_GUARD_ATTRIBUTE]
    if not isinstance(guard, CalendarAadMigrationGuard):
        calendar_aad_migration_invariant_error()
    attribute_read_failed = False
    verify: object = None
    try:
        verify = getattr(guard, "verify", None)
    except Exception:  # noqa: BLE001 - descriptor access must fail closed without leaking detail.
        attribute_read_failed = True
    if attribute_read_failed:
        calendar_aad_migration_invariant_error()
    if not callable(verify):
        calendar_aad_migration_invariant_error()
    inspection_failed = False
    deferred_function = False
    try:
        deferred_function = (
            iscoroutinefunction(verify) or isgeneratorfunction(verify) or isasyncgenfunction(verify)
        )
        signature(verify).bind(
            connection=_SIGNATURE_CONNECTION_SENTINEL,
            phase="before_mutation",
        )
    except Exception:  # noqa: BLE001 - inspect boundary must fail closed without leaking detail.
        # inspect 扩展点可运行第三方 ``__signature__``；普通异常必须先收敛为无内容事实。
        inspection_failed = True
    if inspection_failed or deferred_function:
        # 这里只验证同步调用形状，不执行或包装 guard，保证后续真实异常保持原始语义。
        calendar_aad_migration_invariant_error()
    return guard


__all__ = [
    "CALENDAR_AAD_0019_GUARD_ATTRIBUTE",
    "CalendarAadMigrationConfig",
    "CalendarAadMigrationConnection",
    "CalendarAadMigrationGuard",
    "CalendarAadMigrationInvariantError",
    "CalendarAadMigrationPhase",
    "CalendarAadMigrationScalarResult",
    "calendar_aad_migration_invariant_error",
    "invoke_calendar_aad_migration_guard",
    "resolve_calendar_aad_migration_guard",
    "validate_calendar_aad_migration_guard_result",
]
