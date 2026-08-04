"""OpenAI-compatible 适配器只暴露稳定、脱敏的内部错误。"""

import json

import httpx
import pytest

from ai_employee.domain.briefs import ConversationIntent
from ai_employee.integrations.llm.openai_compatible import (
    ModelGatewayError,
    OpenAICompatibleGateway,
)


@pytest.mark.asyncio
async def test_schema_format_and_usage_cost() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"intent":"show_latest_brief","confidence":1,"reason_code":"x"}'
                        }
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        )

    gateway = OpenAICompatibleGateway(
        base_url="https://example.invalid",
        api_key="secret",
        input_cost_per_million_usd=1,
        output_cost_per_million_usd=2,
        transport=httpx.MockTransport(handler),
    )
    result = await gateway.complete(
        model_name="m",
        prompt_version="p",
        messages=[{"role": "user", "content": "x"}],
        response_model=ConversationIntent,
    )
    assert captured["response_format"]["type"] == "json_schema"
    assert result.usage.estimated_cost_microusd == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 500])
async def test_http_failures_are_stable_errors(status: int) -> None:
    gateway = OpenAICompatibleGateway(
        base_url="https://example.invalid",
        api_key="secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="sensitive body")
        ),
    )
    with pytest.raises(ModelGatewayError):
        await gateway.complete(
            model_name="m", prompt_version="p", messages=[], response_model=ConversationIntent
        )
