"""Gemini and Grok providers against canned HTTP responses (no network, no tokens)."""

import json

import httpx
import pytest
from pydantic import BaseModel

from rootsignal.config import Settings
from rootsignal.providers.llm import (
    FallbackLLMProvider,
    GeminiProvider,
    GrokProvider,
    LLMError,
    get_llm_provider,
)
from rootsignal.providers.llm.http import json_schema_for


class Inner(BaseModel):
    name: str


class Out(BaseModel):
    answer: str
    items: list[Inner] = []


def client(handler) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    return httpx.AsyncClient(transport=httpx.MockTransport(record)), seen


def gemini_reply(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "thinking...", "thought": True},
                            {"text": json.dumps(payload)},
                        ]
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 20,
                "thoughtsTokenCount": 5,
            },
        },
    )


def grok_reply(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "grok-4.7",
            "output": [
                {"type": "reasoning", "content": []},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(payload)}],
                },
            ],
            "usage": {"input_tokens": 1_000_000, "output_tokens": 500_000},
        },
    )


def test_json_schema_inlines_refs():
    schema = json_schema_for(Out)
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)
    assert schema["properties"]["items"]["items"]["properties"]["name"]["type"] == "string"


async def test_gemini_request_and_parsing():
    c, seen = client(lambda _: gemini_reply({"answer": "ok", "items": [{"name": "a"}]}))
    llm = GeminiProvider("KEY", "gemini-test", c, price_input=1.0, price_output=2.0)
    result = await llm.generate_structured("hello", Out, system="be brief")

    assert result.output == Out(answer="ok", items=[Inner(name="a")])
    assert result.usage.input_tokens == 100 and result.usage.output_tokens == 25
    assert result.usage.cost_usd == pytest.approx((100 * 1 + 25 * 2) / 1e6)
    req = seen[0]
    assert req.url.path.endswith("/models/gemini-test:generateContent")
    assert req.headers["x-goog-api-key"] == "KEY"
    body = json.loads(req.content)
    fmt = body["generationConfig"]["responseFormat"]["text"]
    assert fmt["mimeType"] == "application/json" and fmt["schema"]["type"] == "object"
    assert body["systemInstruction"]["parts"][0]["text"] == "be brief"


async def test_grok_request_and_parsing():
    c, seen = client(lambda _: grok_reply({"answer": "yes"}))
    llm = GrokProvider("KEY", "grok-4.7", c, price_input=2.0, price_output=6.0)
    result = await llm.generate_structured("hi", Out, system="sys")

    assert result.output.answer == "yes"
    assert result.usage.cost_usd == pytest.approx(2.0 + 3.0)
    req = seen[0]
    assert req.url.path == "/v1/responses"
    assert req.headers["authorization"] == "Bearer KEY"
    body = json.loads(req.content)
    assert body["store"] is False
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert body["input"][0] == {"role": "system", "content": "sys"}


async def test_retries_once_on_429_then_succeeds():
    replies = iter([httpx.Response(429, text="slow down"), gemini_reply({"answer": "ok"})])
    c, seen = client(lambda _: next(replies))
    result = await GeminiProvider("K", "m", c).generate_structured("x", Out)
    assert result.output.answer == "ok" and len(seen) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, text="bad key"),
        gemini_reply({"wrong_field": 1}),
        httpx.Response(200, json={"candidates": []}),
    ],
)
async def test_gemini_failures_raise_llm_error(response):
    c, _ = client(lambda _: response)
    with pytest.raises(LLMError):
        await GeminiProvider("K", "m", c).generate_structured("x", Out)


async def test_fallback_uses_second_provider_and_records_errors():
    bad, _ = client(lambda _: httpx.Response(503, text="overloaded"))
    good, _ = client(lambda _: grok_reply({"answer": "from grok"}))
    chain = FallbackLLMProvider([GeminiProvider("K", "m", bad), GrokProvider("K", "g", good)])
    result = await chain.generate_structured("x", Out)
    assert result.output.answer == "from grok"
    assert chain.errors and chain.errors[0].startswith("gemini")
    assert chain.name == "gemini+grok"


def test_factory():
    c = httpx.AsyncClient()
    assert get_llm_provider(Settings(llm_provider="mock"), c) is None
    only = get_llm_provider(Settings(llm_provider="gemini", gemini_api_key="k", grok_api_key=""), c)
    assert isinstance(only, GeminiProvider)
    both = get_llm_provider(Settings(llm_provider="grok", gemini_api_key="k", grok_api_key="k"), c)
    assert isinstance(both, FallbackLLMProvider) and both.name == "grok+gemini"
    no_fb = Settings(llm_provider="grok", gemini_api_key="k", grok_api_key="k", llm_fallback=False)
    assert isinstance(get_llm_provider(no_fb, c), GrokProvider)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        get_llm_provider(Settings(llm_provider="gemini", gemini_api_key=""), c)


@pytest.mark.parametrize(
    "raw",
    ['"xai-abc123"', "'xai-abc123'", "xai-abc123\r", "  xai-abc123  ", "Bearer xai-abc123"],
)
def test_api_keys_are_cleaned(raw):
    assert Settings(grok_api_key=raw).grok_api_key == "xai-abc123"
