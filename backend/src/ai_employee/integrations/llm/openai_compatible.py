"""OpenAI-compatible HTTP 模型适配器，输出严格收窄为 Pydantic 模型。"""

import json
import time
from collections.abc import Sequence
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ai_employee.application.ports.model import ModelResponse, ModelUsage
from ai_employee.infrastructure.observability.metrics import Metrics

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
        transport: httpx.AsyncBaseTransport | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.supports_json_schema = supports_json_schema
        self.timeout = timeout
        self.input_rate = input_cost_per_million_usd
        self.output_rate = output_cost_per_million_usd
        self.transport = transport
        self.metrics = metrics

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
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
            )
            if response.status_code == 429 or response.status_code >= 500:
                self._record_provider_error("model_temporary_error")
                raise ModelGatewayError("model_temporary_error")
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            value = response_model.model_validate(
                json.loads(content) if isinstance(content, str) else content
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            self._record_provider_error("model_timeout")
            raise ModelGatewayError("model_timeout") from exc
        except httpx.HTTPStatusError as exc:
            # 非临时 HTTP 拒绝也不能把供应商响应正文或 URL 泄漏给调用层。
            self._record_provider_error("model_request_error")
            raise ModelGatewayError("model_request_error") from exc
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValidationError) as exc:
            self._record_invalid_output(model_name)
            raise ModelGatewayError("model_invalid_output") from exc
        latency = int((time.monotonic() - started) * 1000)
        usage_data = data.get("usage", {})
        try:
            input_tokens = _validated_tokens(usage_data.get("prompt_tokens", 0))
            output_tokens = _validated_tokens(usage_data.get("completion_tokens", 0))
        except (AttributeError, TypeError, ValueError) as exc:
            self._record_invalid_output(model_name)
            raise ModelGatewayError("model_invalid_output") from exc
        cost: int | None = None
        if self.input_rate or self.output_rate:
            cost = round(
                (input_tokens * self.input_rate + output_tokens * self.output_rate)
                / 1_000_000
                * 1_000_000
            )
        if self.metrics is not None:
            self.metrics.record_model_response(
                model=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=(cost or 0) / 1_000_000,
                latency_seconds=latency / 1000,
            )
        return ModelResponse(
            value=value, usage=ModelUsage(input_tokens, output_tokens, latency, cost)
        )

    def _record_provider_error(self, error_code: str) -> None:
        """记录已规范化模型错误，不把 URL、响应或提示词写入指标。"""
        if self.metrics is not None:
            self.metrics.record_provider_error(provider="model", error_code=error_code)

    def _record_invalid_output(self, model_name: str) -> None:
        """记录不可恢复的结构化输出失败，不输出供应商响应或 token 原值。"""
        if self.metrics is not None:
            self.metrics.record_model_schema_repair(model=model_name, outcome="requested")
        self._record_provider_error("model_invalid_output")


def _validated_tokens(value: object) -> int:
    """验证供应商 token 用量是非负整数，拒绝字符串和布尔值。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid token usage")
    return value
