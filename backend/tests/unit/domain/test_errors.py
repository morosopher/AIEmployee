"""验证领域错误保留稳定机器契约且不会共享可变元数据。"""

from collections.abc import MutableMapping
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
