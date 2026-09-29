"""Shared plumbing for HTTP-based LLM providers: schema conversion, retries, cost."""

import asyncio
import copy
import json
import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from rootsignal.providers.llm.base import LLMError, LLMResult, LLMUsage, T

RETRY_STATUSES = {429, 500, 502, 503, 504}


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema with `$ref`s inlined.

    Both Gemini and xAI accept `$ref`, but inlining keeps us inside the subset every
    provider handles. Our schemas are not recursive, so this always terminates.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                merged = {
                    **copy.deepcopy(defs[name]),
                    **{k: v for k, v in node.items() if k != "$ref"},
                }
                return resolve(merged)
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def cost_usd(input_tokens: int, output_tokens: int, price_in: float, price_out: float) -> float:
    return round((input_tokens * price_in + output_tokens * price_out) / 1_000_000, 6)


async def post_json(
    client: httpx.AsyncClient,
    provider: str,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    retries: int = 1,
) -> dict[str, Any]:
    """POST with one retry on rate limits / 5xx. Raises LLMError with a short reason."""
    for attempt in range(retries + 1):
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            if attempt < retries:
                await asyncio.sleep(1.0)
                continue
            raise LLMError(f"{provider}: request failed: {exc!r}") from exc
        if resp.status_code in RETRY_STATUSES and attempt < retries:
            await asyncio.sleep(1.0 * (attempt + 1))
            continue
        if resp.status_code >= 400:
            raise LLMError(f"{provider}: HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LLMError(f"{provider}: response was not JSON") from exc
    raise LLMError(f"{provider}: exhausted retries")  # pragma: no cover


def parse_output(provider: str, text: str, schema: type[T]) -> T:
    if not text.strip():
        raise LLMError(f"{provider}: empty response")
    try:
        return schema.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:
        raise LLMError(f"{provider}: output did not match {schema.__name__}: {exc}") from exc


def build_result(
    schema: type[T],
    output: T,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    started: float,
    price_in: float,
    price_out: float,
) -> LLMResult[T]:
    usage = LLMUsage(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        cost_usd=cost_usd(input_tokens, output_tokens, price_in, price_out),
    )
    return LLMResult[schema](output=output, usage=usage)
