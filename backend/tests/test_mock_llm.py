import pytest
from pydantic import BaseModel

from rootsignal.providers.llm import LLMError, MockLLMProvider


class HypothesisList(BaseModel):
    hypotheses: list[str]


async def test_returns_validated_canned_response():
    llm = MockLLMProvider({"HypothesisList": {"hypotheses": ["db pool exhausted"]}})
    result = await llm.generate_structured("why is checkout slow?", HypothesisList)
    assert result.output.hypotheses == ["db pool exhausted"]
    assert result.usage.provider == "mock"
    assert result.usage.cost_usd == 0.0
    assert llm.calls == [("HypothesisList", "why is checkout slow?")]


async def test_raises_when_response_does_not_match_schema():
    llm = MockLLMProvider({"HypothesisList": {"hypotheses": "not a list"}})
    with pytest.raises(LLMError):
        await llm.generate_structured("x", HypothesisList)
