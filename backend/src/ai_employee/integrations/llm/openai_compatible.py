"""OpenAI-compatible HTTP 模型适配器，输出严格收窄为 Pydantic 模型。"""

import json
import time
from collections.abc import Sequence
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ai_employee.application.ports.model import ModelResponse, ModelUsage

T = TypeVar("T", bound=BaseModel)


class ModelGatewayError(RuntimeError):
    """模型调用失败的稳定内部错误。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OpenAICompatibleGateway:
    """调用 ``/chat/completions`` 并验证 JSON 结构。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        supports_json_schema: bool = True,
        timeout: float = 120.0,
        input_cost_per_million_usd: float = 0,
        output_cost_per_million_usd: float = 0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.supports_json_schema = supports_json_schema
        self.timeout = timeout
        self.input_rate = input_cost_per_million_usd
        self.output_rate = output_cost_per_million_usd

    async def complete(
        self,
        *,
        model_name: str,
        prompt_version: str,
        messages: Sequence[dict[str, str]],
        response_model: type[T],
    ) -> ModelResponse[T]:
        payload: dict[str, object] = {
            "model": model_name,
            "messages": list(messages),
            "temperature": 0,
        }
        if self.supports_json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": response_model.model_json_schema(),
                },
            }
        else:
            payload["messages"] = [
                {"role": "system", "content": "仅输出符合要求的 JSON，不要 Markdown。"},
                *messages,
            ]
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise ModelGatewayError("model_temporary_error")
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            value = response_model.model_validate(
                json.loads(content) if isinstance(content, str) else content
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ModelGatewayError("model_timeout") from exc
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValidationError) as exc:
            raise ModelGatewayError("model_invalid_output") from exc
        latency = int((time.monotonic() - started) * 1000)
        usage_data = data.get("usage", {})
        input_tokens = int(usage_data.get("prompt_tokens", 0))
        output_tokens = int(usage_data.get("completion_tokens", 0))
        cost: int | None = None
        if self.input_rate or self.output_rate:
            cost = round(
                (input_tokens * self.input_rate + output_tokens * self.output_rate)
                / 1_000_000
                * 1_000_000
            )
        return ModelResponse(
            value=value, usage=ModelUsage(input_tokens, output_tokens, latency, cost)
        )
