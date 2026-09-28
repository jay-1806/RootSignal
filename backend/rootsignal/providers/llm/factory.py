from rootsignal.config import Settings
from rootsignal.providers.llm.base import LLMProvider
from rootsignal.providers.llm.mock import MockLLMProvider


def get_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockLLMProvider()
    # Gemini and Grok providers land in Phase 3.
    raise NotImplementedError(f"LLM provider '{settings.llm_provider}' is not implemented yet")
