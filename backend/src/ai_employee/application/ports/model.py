"""供应商无关的模型网关端口。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ModelUsage:
    """一次模型调用的可审计用量，不包含提示词内容。"""

    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    estimated_cost_microusd: int | None = None


@dataclass(frozen=True)
class ModelResponse[T]:
    value: T
    usage: ModelUsage


class ModelGateway(Protocol):
    """异步结构化模型调用能力。"""

    async def complete(
        self,
        *,
        model_name: str,
        prompt_version: str,
        messages: Sequence[dict[str, str]],
        response_model: type[T],
    ) -> ModelResponse[T]: ...
