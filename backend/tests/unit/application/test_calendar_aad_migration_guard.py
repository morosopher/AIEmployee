"""验证 Calendar AAD 0019 迁移守卫的供应商无关组合边界。"""

from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass, field
from inspect import signature
from time import time
from traceback import format_exception

import pytest

import ai_employee.application.ports.calendar_aad_migration_guard as guard_port
from ai_employee.application.ports.calendar_aad_migration_guard import (
    CALENDAR_AAD_0019_GUARD_ATTRIBUTE,
    CalendarAadMigrationGuard,
    CalendarAadMigrationInvariantError,
    resolve_calendar_aad_migration_guard,
)


@dataclass(slots=True)
class _ConfigFake:
    """只公开 resolver 允许读取的 attribute 映射。"""

    attributes: dict[str, object] = field(default_factory=dict)


class _NonCallableVerifyGuard:
    """模拟 runtime Protocol 会误接受的非 callable ``verify`` 属性。"""

    verify = 1


class _WrongKeywordSignatureGuard:
    """模拟存在 ``verify``，但无法接收冻结关键字参数的错误对象。"""

    def verify(self) -> None:
        """故意不接收 connection 或 phase。"""


class _UninspectableSignatureGuard:
    """模拟 callable 存在但标准库无法取得可靠签名的对象。"""

    verify = time


class _AsyncGuard:
    """模拟同步迁移调用后会忽略 coroutine 的异步守卫。"""

    async def verify(self, *, connection: object, phase: str) -> None:
        """异步函数体若未 await 就不会执行任何守卫逻辑。"""
        del connection, phase


class _GeneratorGuard:
    """模拟同步迁移调用后会忽略 lazy generator 的守卫。"""

    def verify(self, *, connection: object, phase: str) -> object:
        """生成器函数体若未迭代就不会执行任何守卫逻辑。"""
        del connection, phase
        yield None


class _AsyncGeneratorGuard:
    """模拟同步迁移调用后会忽略 async generator 的守卫。"""

    async def verify(self, *, connection: object, phase: str) -> object:
        """异步生成器函数体若未迭代就不会执行任何守卫逻辑。"""
        del connection, phase
        yield None


class _ExplodingSignatureCallable:
    """模拟自定义 signature 检查泄漏任意普通异常文本的 callable。"""

    @property
    def __signature__(self) -> object:
        """抛出不得跨越 resolver 安全边界的合成异常。"""
        raise RuntimeError("sensitive synthetic signature detail")

    def __call__(self, *, connection: object, phase: str) -> None:
        """提供合法调用面；测试只允许 resolver 检查，不能执行。"""
        del connection, phase


class _ExplodingSignatureGuard:
    """携带无法安全检查自定义 signature 的守卫。"""

    verify = _ExplodingSignatureCallable()


class _ExplodingVerifyPropertyGuard:
    """模拟读取 ``verify`` 属性时泄漏敏感异常文本的对象。"""

    @property
    def verify(self) -> object:
        """抛出必须由 composition boundary 收敛的合成异常。"""
        raise RuntimeError("sensitive synthetic verify property detail")


@dataclass(slots=True)
class _KeywordOnlyGuard:
    """记录真实调用次数，用于证明 resolver 不探测调用且保持对象 identity。"""

    calls: list[tuple[object, str]] = field(default_factory=list)

    def verify(self, *, connection: object, phase: str) -> None:
        """记录显式调用；resolver 本身不得执行本方法。"""
        self.calls.append((connection, phase))


class _GuardWithOptionalParameter:
    """表示 Task 27C guard 可保留不影响冻结调用面的额外可选参数。"""

    def verify(
        self,
        *,
        connection: object,
        phase: str,
        audit_context: object | None = None,
    ) -> None:
        """接受冻结参数以及不要求调用方提供的可选扩展。"""
        del connection, phase, audit_context


class _RuntimeTypeErrorGuard:
    """证明 guard 自身执行错误不能被 resolver 包装或吞掉。"""

    def verify(self, *, connection: object, phase: str) -> None:
        """在真实执行时抛出调用方应原样观察的合成错误。"""
        del connection, phase
        raise TypeError("synthetic guard runtime failure")


class _NonNoneResultGuard:
    """模拟同步 guard 返回未处理结果的实现。"""

    def verify(self, *, connection: object, phase: str) -> object:
        """返回非 None 值，调用点必须将其视为 invariant violation。"""
        del connection, phase
        return object()


class _LazyResultGuard:
    """模拟同步 guard 返回尚未执行的 generator。"""

    def verify(self, *, connection: object, phase: str) -> object:
        """返回 lazy generator；验证 helper 必须关闭而不能执行其 body。"""
        del connection, phase

        def deferred() -> object:
            """如果被错误迭代则修改外部状态；正常路径不应触发。"""
            yield object()

        return deferred()


async def _deferred_coroutine() -> None:
    """提供一个可检查关闭状态的未启动 coroutine。"""


def _deferred_generator() -> Generator[object, None, None]:
    """提供一个可检查关闭状态的未启动 generator。"""
    yield object()


def _generator_with_sensitive_cleanup_error() -> Generator[None, None, None]:
    """提供一个已启动后会在 ``close`` 清理阶段抛出敏感普通异常的 generator。"""
    try:
        yield None
    finally:
        raise RuntimeError("sensitive cleanup detail")


async def _deferred_async_generator(
    body_calls: list[str],
) -> AsyncGenerator[object, None]:
    """记录 async-generator 正文是否被错误迭代或事件循环驱动。"""
    body_calls.append("body-started")
    yield object()


@pytest.mark.parametrize(
    "guard",
    (
        _NonCallableVerifyGuard(),
        _WrongKeywordSignatureGuard(),
        _UninspectableSignatureGuard(),
        _AsyncGuard(),
        _GeneratorGuard(),
        _AsyncGeneratorGuard(),
        _ExplodingSignatureGuard(),
        _ExplodingVerifyPropertyGuard(),
    ),
    ids=(
        "verify-not-callable",
        "wrong-keyword-signature",
        "signature-unavailable",
        "async-function",
        "generator-function",
        "async-generator-function",
        "signature-exception",
        "verify-property-exception",
    ),
)
def test_resolver_rejects_guard_without_callable_frozen_signature(guard: object) -> None:
    """非 callable 或不能接收两个冻结关键字的对象必须在 composition 时拒绝。"""
    config = _ConfigFake(attributes={CALENDAR_AAD_0019_GUARD_ATTRIBUTE: guard})

    with pytest.raises(
        CalendarAadMigrationInvariantError,
        match=r"^calendar AAD migration invariant violation$",
    ) as caught:
        resolve_calendar_aad_migration_guard(config)

    assert type(caught.value) is CalendarAadMigrationInvariantError
    assert type(caught.value).__module__ == (
        "ai_employee.application.ports.calendar_aad_migration_guard"
    )
    assert str(caught.value) == "calendar AAD migration invariant violation"
    assert "sensitive synthetic" not in "".join(format_exception(caught.value))


def test_guard_protocol_and_strict_empty_verify_keep_frozen_none_annotation() -> None:
    """公开协议与内建 strict-empty guard 必须保留计划冻结的 ``-> None``。"""
    strict_empty_guard = resolve_calendar_aad_migration_guard(_ConfigFake())

    assert signature(CalendarAadMigrationGuard.verify).return_annotation is None
    assert signature(type(strict_empty_guard).verify).return_annotation is None


def test_resolver_returns_exact_keyword_only_guard_without_probe_call() -> None:
    """合法 guard 必须保持同一 identity，resolver 不得通过执行方法来探测签名。"""
    guard = _KeywordOnlyGuard()
    config = _ConfigFake(attributes={CALENDAR_AAD_0019_GUARD_ATTRIBUTE: guard})

    resolved = resolve_calendar_aad_migration_guard(config)

    assert resolved is guard
    assert guard.calls == []


def test_resolver_accepts_guard_with_additional_optional_parameter() -> None:
    """额外可选参数不改变两个冻结关键字可调用的公开契约。"""
    guard = _GuardWithOptionalParameter()
    config = _ConfigFake(attributes={CALENDAR_AAD_0019_GUARD_ATTRIBUTE: guard})

    assert resolve_calendar_aad_migration_guard(config) is guard


def test_guard_runtime_type_error_is_not_wrapped_after_resolution() -> None:
    """resolver 只验证签名；guard 真实执行的 TypeError 必须原样传播。"""
    guard = _RuntimeTypeErrorGuard()
    config = _ConfigFake(attributes={CALENDAR_AAD_0019_GUARD_ATTRIBUTE: guard})
    resolved = resolve_calendar_aad_migration_guard(config)

    with pytest.raises(TypeError, match=r"^synthetic guard runtime failure$"):
        resolved.verify(connection=object(), phase="before_mutation")


def test_guard_result_must_be_exactly_none() -> None:
    """同步 guard 的实际调用结果不是 None 时必须稳定 fail closed。"""
    guard = _NonNoneResultGuard()
    config = _ConfigFake(attributes={CALENDAR_AAD_0019_GUARD_ATTRIBUTE: guard})
    resolved = resolve_calendar_aad_migration_guard(config)

    with pytest.raises(
        CalendarAadMigrationInvariantError,
        match=r"^calendar AAD migration invariant violation$",
    ):
        guard_port.validate_calendar_aad_migration_guard_result(
            resolved.verify(connection=object(), phase="before_mutation")
        )


def test_lazy_guard_result_is_closed_without_running_body() -> None:
    """generator/coroutine 返回值必须关闭后再抛稳定错误。"""
    coroutine = _deferred_coroutine()
    generator = _deferred_generator()

    for result in (coroutine, generator):
        with pytest.raises(
            CalendarAadMigrationInvariantError,
            match=r"^calendar AAD migration invariant violation$",
        ):
            guard_port.validate_calendar_aad_migration_guard_result(result)

    assert coroutine.cr_frame is None
    assert generator.gi_frame is None


def test_cleanup_exception_is_removed_from_stable_invariant_chain() -> None:
    """deferred cleanup 普通异常不得进入固定 invariant 的异常链或 traceback。"""
    generator = _generator_with_sensitive_cleanup_error()
    next(generator)

    with pytest.raises(
        CalendarAadMigrationInvariantError,
        match=r"^calendar AAD migration invariant violation$",
    ) as caught:
        guard_port.validate_calendar_aad_migration_guard_result(generator)

    assert type(caught.value) is CalendarAadMigrationInvariantError
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert "sensitive cleanup detail" not in "".join(format_exception(caught.value))


@pytest.mark.asyncio
async def test_async_generator_result_is_rejected_without_driving_lazy_body() -> None:
    """同步验证不得迭代或同步驱动 async-generator，资源由原异步 owner 清理。"""
    body_calls: list[str] = []
    async_generator = _deferred_async_generator(body_calls)

    try:
        with pytest.raises(
            CalendarAadMigrationInvariantError,
            match=r"^calendar AAD migration invariant violation$",
        ) as caught:
            guard_port.validate_calendar_aad_migration_guard_result(async_generator)

        assert type(caught.value) is CalendarAadMigrationInvariantError
        assert body_calls == []
        # frame 仍存在证明同步 helper 没有新建/驱动事件循环执行 ``aclose``；这是有意边界。
        assert async_generator.ag_frame is not None
    finally:
        # 测试拥有异步上下文，可安全 await 标准 ``aclose``，避免把测试资源留给 GC。
        await async_generator.aclose()

    assert async_generator.ag_frame is None
    assert body_calls == []
