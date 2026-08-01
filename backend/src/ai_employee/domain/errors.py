"""定义跨应用层与适配器边界传递的稳定纯领域错误。"""

import math
from collections.abc import Mapping
from types import MappingProxyType

type ErrorMetadataValue = str | int | float | bool | None
type ErrorMetadata = Mapping[str, ErrorMetadataValue]


def _freeze_metadata(
    metadata: Mapping[str, ErrorMetadataValue] | None,
) -> ErrorMetadata:
    """校验并复制调用方已脱敏的扁平机器元数据。

    元数据只允许 JSON 标量，刻意不接受嵌套容器。这个边界不尝试猜测字符串
    是否敏感；调用方仍必须只传供应商代号、尝试次数等已脱敏机器字段，不能传
    来源正文、Prompt 或供应商原始响应。复制后再包裹只读映射，避免异常跨层传递
    期间被调用方或消费者篡改。

    Args:
        metadata: 调用方已经完成脱敏的可选扁平机器字段。

    Returns:
        与输入对象解除共享且不可修改的元数据映射。

    Raises:
        TypeError: 键不是字符串，或值不是受支持的 JSON 标量。
        ValueError: 浮点值为 NaN 或无穷值，无法稳定序列化。
    """
    if metadata is None:
        return MappingProxyType({})

    copied: dict[str, ErrorMetadataValue] = {}
    for key, value in metadata.items():
        if type(key) is not str:
            raise TypeError("domain error metadata keys must be strings")
        if value is None or type(value) in {str, int, bool}:
            copied[key] = value
            continue
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("domain error metadata floats must be finite")
            copied[key] = value
            continue
        raise TypeError("domain error metadata values must be JSON scalars")

    return MappingProxyType(copied)


def _normalize_retry_after(retry_after: float | None) -> float | None:
    """把供应商重试提示规范为有限且非负的浮点秒数。

    Args:
        retry_after: 供应商返回的普通整数、浮点秒数或缺失值。

    Returns:
        ``None``，或可安全参与重试时间计算的非负有限浮点秒数。

    Raises:
        TypeError: 输入是布尔值或不是普通整数/浮点数。
        ValueError: 输入为负数、非有限值或大到无法转换为浮点数。
    """
    if retry_after is None:
        return None
    if type(retry_after) not in {int, float}:
        raise TypeError("retry_after must be an int, float, or None")

    try:
        normalized = float(retry_after)
    except OverflowError as error:
        raise ValueError("retry_after must be representable as finite seconds") from error

    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("retry_after must be finite and non-negative")
    return normalized


class DomainError(Exception):
    """所有可分类领域失败的稳定基类。

    Args:
        error_code: 供 API、Worker 与审计使用的稳定英文机器错误码。
        message: 面向上层映射的安全错误消息，不得包含敏感来源内容。
        metadata: 调用方已脱敏的扁平机器字段；构造时会复制并冻结。
    """

    _error_code: str
    _message: str
    _metadata: ErrorMetadata

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        metadata: Mapping[str, ErrorMetadataValue] | None = None,
    ) -> None:
        """保存稳定错误契约，并让标准异常字符串保持为安全消息。"""
        super().__init__(message)
        self._error_code = error_code
        self._message = message
        self._metadata = _freeze_metadata(metadata)

    @property
    def error_code(self) -> str:
        """返回构造时冻结的稳定英文机器错误码。"""
        return self._error_code

    @property
    def message(self) -> str:
        """返回与 ``BaseException.args`` 一致的安全错误消息。"""
        return self._message

    @property
    def metadata(self) -> ErrorMetadata:
        """返回构造时复制并冻结的已脱敏机器元数据。"""
        return self._metadata


class UserActionRequiredError(DomainError):
    """表示必须由用户修复授权、连接或其他前置条件的错误。"""


class TransientProviderError(DomainError):
    """表示供应商限流、超时或临时不可用等可重试错误。"""

    _retry_after: float | None

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        retry_after: float | None = None,
        metadata: Mapping[str, ErrorMetadataValue] | None = None,
    ) -> None:
        """保存临时错误并规范供应商建议的可选重试等待秒数。

        Args:
            error_code: 稳定英文机器错误码。
            message: 不含敏感供应商内容的安全消息。
            retry_after: 供应商建议的等待秒数；缺失时由上层重试策略决定。
            metadata: 调用方已脱敏的扁平机器字段。

        Raises:
            TypeError: ``retry_after`` 是布尔值或不是普通整数/浮点数。
            ValueError: ``retry_after`` 为负数、非有限值或无法转为浮点数。
        """
        super().__init__(error_code=error_code, message=message, metadata=metadata)
        self._retry_after = _normalize_retry_after(retry_after)

    @property
    def retry_after(self) -> float | None:
        """返回构造时规范且冻结的供应商重试等待秒数。"""
        return self._retry_after


class PermanentProviderError(DomainError):
    """表示资源不存在或永久权限不足等不可自动重试的供应商错误。"""


class ModelOutputError(DomainError):
    """表示模型结构化输出在受限修复后仍不符合领域契约。"""


class StateConflictError(DomainError):
    """表示请求与当前持久业务状态冲突的确定性错误。"""


class InternalInvariantError(DomainError):
    """表示程序内部不变量被破坏且默认不得自动重试的错误。"""
