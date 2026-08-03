"""暴露严格绑定冻结载荷的人工审批决定接口。"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ai_employee.api.deps import ApiProblem, CsrfProtectedSession
from ai_employee.application.use_cases.approvals import ApprovalDecisionUseCase
from ai_employee.domain.errors import StateConflictError


class ApprovalDecisionRequest(BaseModel):
    """审批决定携带版本和载荷哈希，阻断陈旧或篡改授权。"""

    decision: str = Field(pattern="^(approved|rejected)$")
    version: int = Field(ge=1)
    payload_hash: str = Field(min_length=64, max_length=64)


def build_approvals_router() -> APIRouter:
    """构建审批决定路由，恢复始终通过持久 Outbox。"""
    from ai_employee.api.deps import get_approval_decision_use_case

    router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])

    @router.post("/{approval_id}/decision", status_code=204)
    async def decide_approval(
        approval_id: UUID,
        payload: ApprovalDecisionRequest,
        authenticated: CsrfProtectedSession,
        use_case: Annotated[ApprovalDecisionUseCase, Depends(get_approval_decision_use_case)],
    ) -> None:
        """原子验证用户、状态、版本、有效期和冻结哈希后记录决定。"""
        try:
            await use_case.execute(
                approval_id=approval_id,
                user_id=authenticated.user.id,
                decision=payload.decision,
                version=payload.version,
                payload_hash=payload.payload_hash,
                now=datetime.now(UTC),
            )
        except StateConflictError:
            raise ApiProblem(
                409,
                "approval_conflict",
                "Approval conflict",
                "The approval cannot be changed from its current state.",
            ) from None

    return router
