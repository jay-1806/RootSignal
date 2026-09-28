"""LLM provider interface.

Every provider returns *validated* Pydantic objects plus usage stats. The engine never
parses free text, and it can compare cost and latency across providers.
"""

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMUsage(BaseModel):
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0


class LLMResult(BaseModel, Generic[T]):
    output: T
    usage: LLMUsage


class LLMError(RuntimeError):
    """Raised when a provider fails or returns output that doesn't match the schema."""


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def generate_structured(
        self, prompt: str, schema: type[T], system: str = ""
    ) -> LLMResult[T]:
        """Return an instance of `schema` generated from `prompt`."""
