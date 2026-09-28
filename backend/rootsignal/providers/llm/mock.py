"""Deterministic LLM for local dev, tests, and CI. Costs nothing and needs no API key."""

import time
from typing import Any

from pydantic import ValidationError

from rootsignal.providers.llm.base import LLMError, LLMProvider, LLMResult, LLMUsage, T


class MockLLMProvider(LLMProvider):
    name = "mock"

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None):
        # Canned responses keyed by schema class name, e.g. {"HypothesisList": {...}}.
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []  # (schema name, prompt), handy in tests

    async def generate_structured(
        self, prompt: str, schema: type[T], system: str = ""
    ) -> LLMResult[T]:
        start = time.perf_counter()
        self.calls.append((schema.__name__, prompt))
        raw = self.responses.get(schema.__name__, {})
        try:
            output = schema.model_validate(raw)
        except ValidationError as exc:
            raise LLMError(
                f"MockLLMProvider has no valid canned response for '{schema.__name__}'"
            ) from exc
        usage = LLMUsage(
            provider=self.name,
            model="mock",
            input_tokens=len(prompt.split()),
            output_tokens=len(output.model_dump_json().split()),
            latency_ms=(time.perf_counter() - start) * 1000,
        )
        return LLMResult[schema](output=output, usage=usage)
