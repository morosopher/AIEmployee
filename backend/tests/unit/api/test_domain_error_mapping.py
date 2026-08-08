"""验证领域错误在 API 边界拥有稳定且不泄露原始消息的 RFC 9457 映射。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_employee.api.deps import handle_domain_error
from ai_employee.domain.errors import (
    InternalInvariantError,
    ModelOutputError,
    PermanentProviderError,
    StateConflictError,
    TransientProviderError,
    UserActionRequiredError,
)


@pytest.mark.parametrize(
    ("error", "status", "retry_after"),
    [
        (UserActionRequiredError(error_code="reauth_required", message="secret token"), 403, None),
        (
            UserActionRequiredError(
                error_code="microsoft_reauthorization_required",
                message="secret provider detail",
            ),
            403,
            None,
        ),
        (
            UserActionRequiredError(
                error_code="connection_scope_missing",
                message="secret scope detail",
            ),
            409,
            None,
        ),
        (
            UserActionRequiredError(
                error_code="microsoft_admin_consent_required",
                message="secret administrator detail",
            ),
            409,
            None,
        ),
        (TransientProviderError(error_code="provider_busy", message="secret token", retry_after=12), 503, "12"),
        (PermanentProviderError(error_code="provider_denied", message="secret token"), 422, None),
        (ModelOutputError(error_code="model_invalid", message="secret prompt"), 422, None),
        (StateConflictError(error_code="state_conflict", message="secret body"), 409, None),
        (InternalInvariantError(error_code="invariant_failed", message="secret cookie"), 500, None),
    ],
)
def test_domain_error_problem_mapping_is_typed_and_redacted(
    error: Exception, status: int, retry_after: str | None
) -> None:
    """每个公开错误类型都应得到稳定状态，且不能把领域消息作为 HTTP detail。"""
    app = FastAPI()
    app.add_exception_handler(type(error), handle_domain_error)

    @app.get("/failure")
    def failure() -> None:
        """抛出合成领域失败以验证全局渲染边界。"""
        raise error

    response = TestClient(app, raise_server_exceptions=False).get("/failure")

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error_code"] == error.error_code
    assert "secret" not in response.text
    if retry_after is None:
        assert "retry-after" not in response.headers
    else:
        assert response.headers["retry-after"] == retry_after
