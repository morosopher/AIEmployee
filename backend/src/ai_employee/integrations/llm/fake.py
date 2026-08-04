"""测试用确定性模型，绝不连接网络。"""

import os
from collections.abc import Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

from ai_employee.application.ports.model import ModelResponse, ModelUsage
from ai_employee.domain.briefs import ConversationIntent, EmailJudgement

T = TypeVar("T", bound=BaseModel)


class FakeModelGateway:
    """按 source_id 生成稳定结果，可模拟两次非法 JSON。"""

    def __init__(self, *, scenario: str | None = None) -> None:
        self.scenario = scenario or os.getenv("FAKE_MODEL_SCENARIO", "normal")
        self.calls: list[Sequence[dict[str, str]]] = []
        self._invalid_attempts = 0

    async def complete(
        self,
        *,
        model_name: str,
        prompt_version: str,
        messages: Sequence[dict[str, str]],
        response_model: type[T],
    ) -> ModelResponse[T]:
        self.calls.append(messages)
        if self.scenario is not None and self.scenario.startswith("invalid"):
            self._invalid_attempts += 1
            if self.scenario == "invalid_twice" or self._invalid_attempts <= 1:
                raise ValueError("invalid model JSON")
        if response_model is ConversationIntent:
            value: Any = ConversationIntent(
                intent="generate_daily_brief", confidence=1, reason_code="fake_deterministic"
            )
        elif response_model is EmailJudgement:
            source = next((m["content"] for m in messages if m.get("role") == "user"), "thread")
            value = EmailJudgement(
                thread_id=source,
                category="other",
                urgency="normal",
                confidence=1,
                reason_codes=["fake_deterministic"],
            )
        else:
            value = response_model.model_validate({})
        return ModelResponse(value=value, usage=ModelUsage())


def build_model_gateway() -> FakeModelGateway:
    """测试模式下构造 Fake，生产代码不应静默启用它。"""
    if os.getenv("APP_TEST_MODE", "false").lower() != "true":
        raise RuntimeError("FakeModelGateway requires APP_TEST_MODE=true")
    return FakeModelGateway()
