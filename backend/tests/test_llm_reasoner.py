"""LLMReasoner inside the real orchestrator, with a scripted (mock) LLM."""

import pytest
from fakes import bad_deploy, db_pool_exhaustion
from test_scenarios import investigate, sessionmaker  # noqa: F401 - fixture

from rootsignal.core.models import RCA, Category
from rootsignal.engine.reasoners import LLMReasoner
from rootsignal.providers.llm import LLMError, MockLLMProvider

NARRATIVE = {"summary": "checkout 1.9.0 broke orders.", "reasoning": "version flip + KeyError."}


class FailingLLM(MockLLMProvider):
    name = "failing"

    async def generate_structured(self, prompt, schema, system=""):
        raise LLMError("failing: HTTP 429: quota exceeded")


async def test_llm_hypotheses_are_tested_and_narrative_used(sessionmaker):  # noqa: F811
    llm = MockLLMProvider(
        {
            "HypothesesOut": {
                "hypotheses": [
                    {
                        "category": "deploy_regression",
                        "service": "checkout",
                        "statement": "checkout 1.9.0 regressed order handling",
                        "checks": [
                            {"check": "log_search", "service": "checkout", "pattern": "KeyError"},
                            # invalid: unknown service and unsafe pattern -> dropped by code
                            {"check": "error_rate", "service": "payments"},
                            {"check": "log_search", "service": "checkout", "pattern": '"} or 1=1'},
                        ],
                    },
                    # invalid: inventory-only category on a service without a DB -> dropped
                    {
                        "category": "db_connection_exhaustion",
                        "service": "gateway",
                        "statement": "x",
                    },
                    {"category": "memory_leak", "service": "checkout", "statement": "leak"},
                ]
            },
            "Narrative": NARRATIVE,
        }
    )
    row, telemetry = await investigate(
        sessionmaker, bad_deploy, "HighErrorRate", "gateway", reasoner=LLMReasoner(llm)
    )
    rca = RCA.model_validate(row.rca)
    assert (rca.category, rca.service) == (Category.DEPLOY_REGRESSION, "checkout")
    assert rca.summary == NARRATIVE["summary"] and rca.written_by == "mock"
    assert {(h["category"], h["service"]) for h in row.hypotheses} == {
        ("deploy_regression", "checkout"),
        ("memory_leak", "checkout"),
    }
    # the LLM's extra check ran; the unsafe one never reached Loki
    assert any("KeyError" in q for q in telemetry.queries)
    assert not any("1=1" in q or "payments" in q for q in telemetry.queries)
    # confidence is computed, and the LLM's prompt never contained a confidence instruction to obey
    assert rca.confidence >= 0.7
    purposes = [c.purpose for c in row.llm_calls]
    assert purposes[0] == "hypothesize#1" and purposes[-1] == "narrate"
    assert all(c.success for c in row.llm_calls)


async def test_failing_llm_falls_back_to_rule_based(sessionmaker):  # noqa: F811
    row, _ = await investigate(
        sessionmaker,
        db_pool_exhaustion,
        "HighErrorRate",
        "checkout",
        reasoner=LLMReasoner(FailingLLM()),
    )
    rca = RCA.model_validate(row.rca)
    assert (rca.category, rca.service) == (Category.DB_CONNECTION_EXHAUSTION, "inventory")
    assert "rule_based (llm failed)" in row.reasoner
    assert rca.written_by == "rule_based (llm failed)"
    assert row.llm_calls and not any(c.success for c in row.llm_calls)
    assert "quota exceeded" in row.llm_calls[0].error


@pytest.mark.parametrize(
    "hypotheses", [[], [{"category": "memory_leak", "service": "nope", "statement": "x"}]]
)
async def test_unusable_llm_output_falls_back(sessionmaker, hypotheses):  # noqa: F811
    llm = MockLLMProvider({"HypothesesOut": {"hypotheses": hypotheses}, "Narrative": NARRATIVE})
    row, _ = await investigate(
        sessionmaker, bad_deploy, "HighErrorRate", "checkout", reasoner=LLMReasoner(llm)
    )
    assert "llm proposed nothing usable" in row.reasoner
    assert RCA.model_validate(row.rca).category == Category.DEPLOY_REGRESSION
