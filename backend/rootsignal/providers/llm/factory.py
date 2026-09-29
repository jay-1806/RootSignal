import httpx

from rootsignal.config import Settings
from rootsignal.providers.llm.base import LLMProvider
from rootsignal.providers.llm.fallback import FallbackLLMProvider
from rootsignal.providers.llm.gemini import GeminiProvider
from rootsignal.providers.llm.grok import GrokProvider


def _gemini(settings: Settings, client: httpx.AsyncClient) -> GeminiProvider | None:
    if not settings.gemini_api_key:
        return None
    return GeminiProvider(
        settings.gemini_api_key,
        settings.gemini_model,
        client,
        settings.gemini_price_input,
        settings.gemini_price_output,
    )


def _grok(settings: Settings, client: httpx.AsyncClient) -> GrokProvider | None:
    if not settings.grok_api_key:
        return None
    return GrokProvider(
        settings.grok_api_key,
        settings.grok_model,
        client,
        settings.grok_price_input,
        settings.grok_price_output,
    )


def get_llm_provider(settings: Settings, client: httpx.AsyncClient) -> LLMProvider | None:
    """The configured LLM, or None in "mock" mode (rule-based reasoning only).

    Primary provider first; with `llm_fallback`, the other provider is added if it has a key.
    """
    if settings.llm_provider == "mock":
        return None
    builders = {"gemini": _gemini, "grok": _grok}
    primary = builders[settings.llm_provider](settings, client)
    if primary is None:
        name = settings.llm_provider
        raise ValueError(f"LLM_PROVIDER={name} but {name.upper()}_API_KEY is empty")
    chain: list[LLMProvider] = [primary]
    if settings.llm_fallback:
        other = "grok" if settings.llm_provider == "gemini" else "gemini"
        secondary = builders[other](settings, client)
        if secondary is not None:
            chain.append(secondary)
    return chain[0] if len(chain) == 1 else FallbackLLMProvider(chain)
