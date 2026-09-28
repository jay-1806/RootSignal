"""xAI Grok via the Responses API, with JSON-schema structured output.

Docs: https://docs.x.ai/docs/guides/structured-outputs
`store: false` keeps prompts (which contain incident data) off xAI's servers.
"""

import time

import httpx

from rootsignal.providers.llm.base import LLMProvider, LLMResult, T
from rootsignal.providers.llm.http import build_result, json_schema_for, parse_output, post_json

BASE_URL = "https://api.x.ai/v1"


class GrokProvider(LLMProvider):
    name = "grok"

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
        messages = [{"role": "user", "content": prompt}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        body = {
            "model": self.model,
            "input": messages,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema.__name__,
                    "schema": json_schema_for(schema),
                    "strict": True,
                }
            },
        }
        data = await post_json(
            self.client,
            self.name,
            f"{self.base_url}/responses",
            body,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        text = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                text += "".join(
                    c.get("text", "")
                    for c in item.get("content", [])
                    if c.get("type") == "output_text"
                )
        output = parse_output(self.name, text, schema)

        usage = data.get("usage", {})
        return build_result(
            schema,
            output,
            self.name,
            data.get("model", self.model),
            usage.get("input_tokens", usage.get("prompt_tokens", 0)),
            usage.get("output_tokens", usage.get("completion_tokens", 0)),
            started,
            self.price_input,
            self.price_output,
        )
