"""Google Gemini via the REST generateContent endpoint, with JSON-schema structured output.

Docs: https://ai.google.dev/gemini-api/docs/structured-output
"""

import time

import httpx

from rootsignal.providers.llm.base import LLMProvider, LLMResult, T
from rootsignal.providers.llm.http import build_result, json_schema_for, parse_output, post_json

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        client: httpx.AsyncClient,
        price_input: float = 0.0,
        price_output: float = 0.0,
        base_url: str = BASE_URL,
    ):
        self.api_key = api_key
        self.model = model
        self.client = client
        self.price_input = price_input
        self.price_output = price_output
        self.base_url = base_url.rstrip("/")

    async def generate_structured(
        self, prompt: str, schema: type[T], system: str = ""
    ) -> LLMResult[T]:
        started = time.perf_counter()
        body: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseFormat": {
                    "text": {"mimeType": "application/json", "schema": json_schema_for(schema)}
                },
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        data = await post_json(
            self.client,
            self.name,
            f"{self.base_url}/models/{self.model}:generateContent",
            body,
            headers={"x-goog-api-key": self.api_key},
        )
        candidates = data.get("candidates") or [{}]
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        output = parse_output(self.name, text, schema)

        meta = data.get("usageMetadata", {})
        output_tokens = meta.get("candidatesTokenCount", 0) + meta.get("thoughtsTokenCount", 0)
        return build_result(
            schema,
            output,
            self.name,
            self.model,
            meta.get("promptTokenCount", 0),
            output_tokens,
            started,
            self.price_input,
            self.price_output,
        )
