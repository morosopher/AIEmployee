"""验证领域错误保留稳定机器契约且不会共享可变元数据。"""

from collections.abc import Callable, MutableMapping
from typing import cast

import pytest

from ai_employee.domain.errors import (
    DomainError,
    ErrorMetadataValue,
    InternalInvariantError,
    ModelOutputError,
    PermanentProviderError,
    StateConflictError,
    TransientProviderError,
    UserActionRequiredError,
)


def _transient_error_with_retry_after(retry_after: object) -> TransientProviderError:
    """通过动态调用边界构造临时错误，以覆盖运行时无效输入。"""
    constructor = cast(Callable[..., TransientProviderError], TransientProviderError)
    return constructor(
        error_code="provider_rate_limited",
        message="Synthetic temporary failure",
        retry_after=retry_after,
    )


def test_transient_provider_error_preserves_retry_contract() -> None:
    """临时供应商错误必须保留稳定错误码、消息与重试等待秒数。"""
    error = TransientProviderError(
        error_code="provider_rate_limited",
        message="Try again later",
        retry_after=30,
    )

    assert error.error_code == "provider_rate_limited"
    assert error.message == "Try again later"
    assert error.retry_after == 30
    assert error.args == ("Try again later",)


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        pytest.param(None, None, id="missing"),
        pytest.param(0, 0.0, id="zero-int"),
        pytest.param(30, 30.0, id="positive-int"),
        pytest.param(2.5, 2.5, id="positive-float"),
    ],
)
def test_transient_provider_error_normalizes_valid_retry_after(
    retry_after: float | None,
    expected: float | None,
) -> None:
    """缺失值保持为空，合法整数和浮点秒数统一规范为非负有限浮点数。"""
    error = TransientProviderError(
        error_code="provider_rate_limited",
        message="Synthetic temporary failure",
        retry_after=retry_after,
    )

    assert error.retry_after == expected
    if expected is not None:
        assert type(error.retry_after) is float


@pytest.mark.parametrize(
    "retry_after",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param("30", id="string"),
        pytest.param(object(), id="object"),
    ],
)
def test_transient_provider_error_rejects_non_numeric_retry_after(
    retry_after: object,
) -> None:
    """布尔值和运行时非数字输入必须以类型错误拒绝。"""
    with pytest.raises(TypeError, match="retry_after"):
        _transient_error_with_retry_after(retry_after)


@pytest.mark.parametrize(
    "retry_after",
    [
        pytest.param(-1, id="negative-int"),
        pytest.param(-0.5, id="negative-float"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param(10**10_000, id="overflowing-int"),
    ],
)
def test_transient_provider_error_rejects_invalid_numeric_retry_after(
    retry_after: object,
) -> None:
    """负数、非有限值和无法转为浮点秒数的超大整数必须以值错误拒绝。"""
    with pytest.raises(ValueError, match="retry_after"):
        _transient_error_with_retry_after(retry_after)


@pytest.mark.parametrize(
    ("error", "expected_error_code"),
    [
        (
            DomainError(error_code="domain_error", message="Synthetic domain failure"),
            "domain_error",
        ),
        (
            UserActionRequiredError(
                error_code="user_action_required",
                message="Synthetic user action required",
            ),
            "user_action_required",
        ),
        (
            TransientProviderError(
                error_code="provider_temporarily_unavailable",
                message="Synthetic temporary failure",
            ),
            "provider_temporarily_unavailable",
        ),
        (
            PermanentProviderError(
                error_code="provider_permission_denied",
                message="Synthetic permanent failure",
            ),
            "provider_permission_denied",
        ),
        (
            ModelOutputError(
                error_code="model_output_invalid",
                message="Synthetic model output failure",
            ),
            "model_output_invalid",
        ),
        (
            StateConflictError(
                error_code="state_conflict",
                message="Synthetic state conflict",
            ),
            "state_conflict",
        ),
        (
            InternalInvariantError(
                error_code="internal_invariant_broken",
                message="Synthetic invariant failure",
            ),
            "internal_invariant_broken",
        ),
    ],
)
def test_public_domain_errors_expose_stable_error_codes(
    error: DomainError,
    expected_error_code: str,
) -> None:
    """每个公开错误类别都必须向上层暴露调用方指定的稳定错误码。"""
    assert error.error_code == expected_error_code


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    [
        pytest.param("error_code", "tampered_error_code", id="error-code"),
        pytest.param("message", "Tampered message", id="message"),
        pytest.param("metadata", {"attempt": 99}, id="metadata"),
    ],
)
def test_domain_error_public_fields_cannot_be_rebound(
    attribute: str,
    replacement: object,
) -> None:
    """错误码、安全消息和冻结元数据构造后都必须保持只读且彼此一致。"""
    error = DomainError(
        error_code="synthetic_domain_error",
        message="Synthetic safe message",
        metadata={"attempt": 1},
    )

    with pytest.raises(AttributeError):
        setattr(error, attribute, replacement)

    assert error.error_code == "synthetic_domain_error"
    assert error.message == "Synthetic safe message"
    assert error.metadata == {"attempt": 1}
    assert error.args == ("Synthetic safe message",)
    assert str(error) == error.message


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    [
        pytest.param("args", ("Tampered args",), id="args"),
        pytest.param("_message", "Tampered message", id="private-message"),
        pytest.param("_error_code", "tampered_error_code", id="private-error-code"),
        pytest.param("_metadata", {"attempt": 99}, id="private-metadata"),
    ],
)
def test_domain_error_contract_backing_fields_cannot_be_rebound(
    attribute: str,
    replacement: object,
) -> None:
    """异常参数和私有后备字段也必须冻结，避免绕过只读 properties。"""
    error = DomainError(
        error_code="synthetic_domain_error",
        message="Synthetic safe message",
        metadata={"attempt": 1},
    )

    with pytest.raises(AttributeError):
        setattr(error, attribute, replacement)

    assert error.error_code == "synthetic_domain_error"
    assert error.message == "Synthetic safe message"
    assert error.metadata == {"attempt": 1}
    assert error.args == ("Synthetic safe message",)
    assert str(error) == error.message


def test_transient_provider_retry_after_cannot_be_rebound() -> None:
    """规范后的重试等待秒数必须只读，避免调度判断在异常传递期间漂移。"""
    error = TransientProviderError(
        error_code="provider_rate_limited",
        message="Synthetic temporary failure",
        retry_after=30,
    )
    attribute = "retry_after"

    with pytest.raises(AttributeError):
        setattr(error, attribute, 60)

    assert error.retry_after == 30.0


def test_transient_provider_retry_after_backing_field_cannot_be_rebound() -> None:
    """私有重试后备字段也必须冻结，避免绕过公开只读属性。"""
    error = TransientProviderError(
        error_code="provider_rate_limited",
        message="Synthetic temporary failure",
        retry_after=30,
    )
    attribute = "_retry_after"

    with pytest.raises(AttributeError):
        setattr(error, attribute, 60.0)

    assert error.retry_after == 30.0
    assert error.args == ("Synthetic temporary failure",)
    assert str(error) == error.message


def test_frozen_domain_error_preserves_exception_chaining() -> None:
    """冻结契约不得阻止正常 raise/catch、Traceback、cause 或 context 传播。"""
    try:
        try:
            raise RuntimeError("Synthetic root cause")
        except RuntimeError as cause:
            raise DomainError(
                error_code="synthetic_domain_error",
                message="Synthetic safe message",
                metadata={"attempt": 1},
            ) from cause
    except DomainError as error:
        assert isinstance(error.__cause__, RuntimeError)
        assert str(error.__cause__) == "Synthetic root cause"
        assert error.__context__ is error.__cause__
        assert error.__traceback__ is not None
        assert error.error_code == "synthetic_domain_error"
        assert error.message == "Synthetic safe message"
        assert error.metadata == {"attempt": 1}
        assert error.args == ("Synthetic safe message",)
        assert str(error) == error.message


def test_error_metadata_is_an_immutable_defensive_copy() -> None:
    """调用方和错误对象都不能在构造后篡改已脱敏机器元数据。"""
    source_metadata: dict[str, ErrorMetadataValue] = {
        "provider": "gmail",
        "attempt": 1,
        "degraded": True,
    }
    error = PermanentProviderError(
        error_code="provider_permission_denied",
        message="Synthetic permanent failure",
        metadata=source_metadata,
    )

    source_metadata["provider"] = "calendar"
    source_metadata["attempt"] = 2

    assert error.metadata == {
        "provider": "gmail",
        "attempt": 1,
        "degraded": True,
    }
    with pytest.raises(TypeError):
        cast(MutableMapping[str, ErrorMetadataValue], error.metadata)["attempt"] = 3


def test_error_metadata_rejects_nested_content_containers() -> None:
    """错误元数据只接受扁平机器标量，避免承载正文、Prompt 或原始响应。"""
    invalid_metadata = cast(
        dict[str, ErrorMetadataValue],
        {"provider_response": {"synthetic_field": "synthetic_value"}},
    )

    with pytest.raises(TypeError, match="metadata"):
        DomainError(
            error_code="invalid_metadata",
            message="Synthetic metadata failure",
            metadata=invalid_metadata,
        )
