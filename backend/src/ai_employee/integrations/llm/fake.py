"""测试用确定性模型，绝不连接网络。"""

import json
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
            try:
                source = str(json.loads(source).get("thread_id", source))
            except json.JSONDecodeError:
                pass
            value = EmailJudgement(
                thread_id=source,
                category="other",
                urgency="normal",
                confidence=1,
                reason_codes=["fake_deterministic"],
            )
        else:
            value = response_model.model_validate({})
        usage = ModelUsage(output_tokens=1 if self.scenario == "partial" else 0)
        if self.scenario == "partial" and isinstance(value, EmailJudgement):
            value = value.model_copy(update={"reason_codes": ["fake_partial"]})
        return ModelResponse(value=value, usage=usage)


def build_model_gateway(settings: Any | None = None) -> Any:
    """按显式测试开关选择 Fake 或配置好的真实适配器。

    真实适配器只在调用方已经提供配置与 Secret 时构造，本函数不发起请求。
    """
    test_mode = getattr(settings, "app_test_mode", None)
    if test_mode is None:
        test_mode = os.getenv("APP_TEST_MODE", "false").lower() == "true"
    if test_mode:
        return FakeModelGateway()
    if settings is None:
        raise RuntimeError("normal model gateway requires settings")
    from ai_employee.integrations.llm.openai_compatible import OpenAICompatibleGateway

    return OpenAICompatibleGateway(
        base_url=settings.model_base_url,
        api_key=settings.read_secret_file(settings.model_api_key_file).get_secret_value(),
        supports_json_schema=settings.model_supports_json_schema,
        input_cost_per_million_usd=settings.model_input_cost_per_million_usd,
        output_cost_per_million_usd=settings.model_output_cost_per_million_usd,
    )
