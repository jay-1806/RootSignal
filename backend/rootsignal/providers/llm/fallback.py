from rootsignal.providers.llm.base import LLMError, LLMProvider, LLMResult, T


class FallbackLLMProvider(LLMProvider):
    """Try providers in order; the first successful answer wins.

    `errors` keeps the failures from the most recent call so callers can record them.
    """

    def __init__(self, providers: list[LLMProvider]):
        if not providers:
            raise ValueError("FallbackLLMProvider needs at least one provider")
        self.providers = providers
        self.name = "+".join(p.name for p in providers)
        self.errors: list[str] = []

    async def generate_structured(
        self, prompt: str, schema: type[T], system: str = ""
    ) -> LLMResult[T]:
        self.errors = []
        for provider in self.providers:
            try:
                return await provider.generate_structured(prompt, schema, system)
            except LLMError as exc:
                self.errors.append(str(exc))
        raise LLMError("all LLM providers failed: " + " | ".join(self.errors))
