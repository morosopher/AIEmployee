"""验证歧义对话请求向模型提供版本化分类规则，同时隔离并脱敏用户输入。"""

import json
from pathlib import Path

import httpx
import pytest

from ai_employee.agents.daily_brief.nodes import classify_ambiguous_conversation_intent
from ai_employee.integrations.llm.openai_compatible import OpenAICompatibleGateway


@pytest.mark.asyncio
async def test_ambiguous_request_sends_classifier_rules_before_redacted_input() -> None:
    """通过真实 HTTP 适配器检查模型边界，防止把分类任务误发为无规则的普通聊天。"""
    requests: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """记录真实适配器组装的请求，仅替换外部网络响应。"""
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "explain_capabilities",
                                    "confidence": 1,
                                    "reason_code": "synthetic_capability_question",
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
            },
        )

    gateway = OpenAICompatibleGateway(
        base_url="https://model.example.test/v1",
        api_key="synthetic-key",
        transport=httpx.MockTransport(provider),
    )
    result = await classify_ambiguous_conversation_intent(
        "What can you do? Bearer synthetic-sensitive-value",
        model_gateway=gateway,
        model_name="synthetic-model",
    )

    assert result.intent == "explain_capabilities"
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    messages = payload["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    # 既有版本文件是事实来源；验证它确实进入供应商请求，而非只检查仓库存在该文件。
    prompt = (
        Path(__file__).resolve().parents[3]
        / "src/ai_employee/prompts/conversation_intent_v1.md"
    ).read_text(encoding="utf-8")
    assert messages[0]["content"] == prompt
    assert "What can you do?" in messages[1]["content"]
    assert "synthetic-sensitive-value" not in requests[0].content.decode()
