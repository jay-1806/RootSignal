"""Reasoners propose hypotheses and write the final narrative.

- RuleBasedReasoner: deterministic playbooks. Used in "mock" mode, as the fallback when
  the LLM fails, and as the baseline the evaluation compares against.
- LLMReasoner: Gemini/Grok. It only sees summaries of telemetry, and code validates
  everything it proposes.

Neither reasoner runs queries or sets confidence. The orchestrator does both.
"""

import json
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from rootsignal.core import topology
from rootsignal.core.models import Alert, Category, Hypothesis
from rootsignal.engine.checks import CATALOG, CheckName, Observation
from rootsignal.engine.plan import PLAYBOOKS, Expect, applicable
from rootsignal.engine.redact import redact_obj
from rootsignal.providers.llm import FallbackLLMProvider, LLMError, LLMProvider

RULE_BASED = "rule_based"


# --------------------------------------------------------------------------- data


class InvestigationContext(BaseModel):
    alert: Alert
    services: list[str]  # alert service + its dependencies
    triage: list[Observation] = Field(default_factory=list)
    similar_incidents: list[dict] = Field(default_factory=list)

    def anomalous_services(self) -> set[str]:
        return {o.service for o in self.triage if o.available and o.anomalous}


class ProposedCheck(BaseModel):
    check: CheckName
    service: str
    expect: Expect = Expect.ANOMALOUS
    pattern: str | None = Field(default=None, description="Only for log_search: short plain text")


class HypothesisDraft(BaseModel):
    category: Category
    service: str
    statement: str = Field(max_length=300)
    rationale: str = Field(default="", max_length=500)
    checks: list[ProposedCheck] = Field(default_factory=list, max_length=6)


class HypothesesOut(BaseModel):
    hypotheses: list[HypothesisDraft] = Field(max_length=6)


class Narrative(BaseModel):
    summary: str = Field(description="2-3 sentence incident summary for an on-call engineer")
    reasoning: str = Field(description="Why the evidence points to this cause and not the others")


class LLMCallRecord(BaseModel):
    purpose: str
    provider: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    success: bool = True
    error: str | None = None


class Reasoner(ABC):
    name: str

    @abstractmethod
    async def hypothesize(
        self,
        ctx: InvestigationContext,
        previous: list[Hypothesis],
        iteration: int,
        calls: list[LLMCallRecord],
    ) -> tuple[list[HypothesisDraft], str]:
        """New hypotheses to test, and who proposed them."""

    @abstractmethod
    async def narrate(
        self,
        ctx: InvestigationContext,
        top: Hypothesis,
        alternatives: list[Hypothesis],
        calls: list[LLMCallRecord],
    ) -> tuple[Narrative, str]:
        """Human-readable summary of the conclusion, and who wrote it."""


# --------------------------------------------------------------------------- rule-based

FIRST_ROUND = [
    Category.DEPLOY_REGRESSION,
    Category.MEMORY_LEAK,
    Category.DB_CONNECTION_EXHAUSTION,
    Category.CACHE_DEGRADATION,
]
LATER_ROUNDS = FIRST_ROUND + [Category.CONFIG_CHANGE]

STATEMENTS = {
    Category.DEPLOY_REGRESSION: "A recent deploy of {s} introduced a regression",
    Category.CONFIG_CHANGE: "A recent configuration change to {s} broke it",
    Category.MEMORY_LEAK: "{s} is leaking memory",
    Category.DB_CONNECTION_EXHAUSTION: "{s} has exhausted its database connection pool",
    Category.CACHE_DEGRADATION: "{s}'s cache (Redis) is slow or degraded",
    Category.DOWNSTREAM_FAILURE: "{s} is failing because a dependency is failing",
    Category.TRAFFIC_SURGE: "{s} is overloaded by a traffic surge",
}


class RuleBasedReasoner(Reasoner):
    name = RULE_BASED

    async def hypothesize(self, ctx, previous, iteration, calls):
        tested = {(h.category, h.service) for h in previous}
        if iteration == 1:
            anomalous = ctx.anomalous_services()
            services = [s for s in ctx.services if s == ctx.alert.service or s in anomalous]
            categories = FIRST_ROUND
        else:
            services = list(dict.fromkeys(ctx.services + topology.known_services()))
            categories = LATER_ROUNDS
        drafts = [
            HypothesisDraft(
                category=c,
                service=s,
                statement=STATEMENTS[c].format(s=s),
                rationale="standard playbook for the alerting service and its dependencies",
            )
            for s in services
            for c in categories
            if topology.is_known(s) and applicable(c, s) and (c, s) not in tested
        ]
        return drafts, self.name

    async def narrate(self, ctx, top, alternatives, calls):
        supporting = [e for e in top.evidence if e.supports][:3]
        summary = (
            f"Alert '{ctx.alert.title}' on {ctx.alert.service}. Most likely cause: "
            f"{top.statement} (confidence {top.confidence:.0%})."
        )
        reasoning = "Supporting evidence: " + (
            "; ".join(e.summary for e in supporting) if supporting else "none"
        )
        if alternatives:
            reasoning += ". Ruled less likely: " + "; ".join(
                f"{h.statement} ({h.confidence:.0%})" for h in alternatives[:3]
            )
        return Narrative(summary=summary, reasoning=reasoning + "."), self.name


# --------------------------------------------------------------------------- LLM

SYSTEM_PROMPT = """You are an experienced site reliability engineer investigating a \
production incident. You propose hypotheses and choose checks to test them. You do not \
run queries yourself. Rules:
- Use only services listed in `topology` and categories in `categories`.
- Use only checks listed in `check_catalog`. For each check, say whether the hypothesis \
predicts it will look "anomalous" or "normal". Checks that would rule the hypothesis \
out are valuable.
- `log_search` needs a short plain-text `pattern` (letters, digits, spaces, '|' for \
alternatives). Other checks take no pattern.
- Prefer root causes over symptoms: if a service fails because a dependency fails, \
hypothesize about the dependency.
- Never state confidence numbers. Confidence is computed from the evidence."""


def _signal(o: Observation) -> dict:
    return {
        "check": o.check.value,
        "service": o.service,
        "anomalous": o.anomalous,
        "summary": o.summary,
    }


def _hypothesis_digest(h: Hypothesis) -> dict:
    return {
        "category": h.category.value,
        "service": h.service,
        "statement": h.statement,
        "computed_confidence": h.confidence,
        "evidence": [
            {"supports": e.supports, "weight": e.weight, "summary": e.summary}
            for e in h.evidence[:5]
        ],
    }


class LLMReasoner(Reasoner):
    def __init__(self, llm: LLMProvider, fallback: Reasoner | None = None):
        self.llm = llm
        self.fallback = fallback or RuleBasedReasoner()
        self.name = llm.name

    async def _call(self, purpose: str, prompt: str, schema, calls: list[LLMCallRecord]):
        try:
            result = await self.llm.generate_structured(prompt, schema, SYSTEM_PROMPT)
        except LLMError as exc:
            calls.extend(self._failed_attempts(purpose))
            if not isinstance(self.llm, FallbackLLMProvider):
                calls.append(
                    LLMCallRecord(
                        purpose=purpose, provider=self.llm.name, success=False, error=str(exc)[:500]
                    )
                )
            raise
        calls.extend(self._failed_attempts(purpose))
        u = result.usage
        calls.append(
            LLMCallRecord(
                purpose=purpose,
                provider=u.provider,
                model=u.model,
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                latency_ms=u.latency_ms,
                cost_usd=u.cost_usd,
            )
        )
        return result.output

    def _failed_attempts(self, purpose: str) -> list[LLMCallRecord]:
        """Failed providers inside a fallback chain (e.g. Gemini 429 before Grok answered)."""
        if not isinstance(self.llm, FallbackLLMProvider):
            return []
        return [
            LLMCallRecord(
                purpose=purpose, provider=err.split(":", 1)[0], success=False, error=err[:500]
            )
            for err in self.llm.errors
        ]

    async def hypothesize(self, ctx, previous, iteration, calls):
        payload = {
            "task": (
                "Propose 2-5 hypotheses for the root cause, each with 2-5 checks."
                if iteration == 1
                else "The earlier hypotheses were not conclusive. Propose 1-4 NEW hypotheses "
                "(different category or service) that explain the evidence better."
            ),
            "alert": {
                "title": ctx.alert.title,
                "service": ctx.alert.service,
                "severity": ctx.alert.severity.value,
                "description": ctx.alert.description,
            },
            "topology": topology.describe(),
            "categories": [c.value for c in Category],
            "check_catalog": {c.value: desc for c, desc in CATALOG.items()},
            "example_checks_per_category": {
                c.value: [s[0].value for s in steps] for c, steps in PLAYBOOKS.items()
            },
            "triage_signals": [_signal(o) for o in ctx.triage if o.available],
            "similar_confirmed_incidents": [
                {k: i.get(k) for k in ("category", "service", "root_cause", "similarity")}
                for i in ctx.similar_incidents
            ],
            "previous_hypotheses": [_hypothesis_digest(h) for h in previous],
        }
        prompt = json.dumps(redact_obj(payload), default=str)
        try:
            out: HypothesesOut = await self._call(
                f"hypothesize#{iteration}", prompt, HypothesesOut, calls
            )
        except LLMError:
            drafts, _ = await self.fallback.hypothesize(ctx, previous, iteration, calls)
            return drafts, f"{RULE_BASED} (llm failed)"

        tested = {(h.category, h.service) for h in previous}
        valid = [
            d
            for d in out.hypotheses
            if topology.is_known(d.service)
            and applicable(d.category, d.service)
            and (d.category, d.service) not in tested
        ]
        if not valid:
            drafts, _ = await self.fallback.hypothesize(ctx, previous, iteration, calls)
            return drafts, f"{RULE_BASED} (llm proposed nothing usable)"
        return valid, self.name

    async def narrate(self, ctx, top, alternatives, calls):
        payload = {
            "task": (
                "Write the incident summary and reasoning for the conclusion below. Base it "
                "only on the listed evidence. Do not change or restate the confidence value."
            ),
            "alert": {"title": ctx.alert.title, "service": ctx.alert.service},
            "conclusion": _hypothesis_digest(top),
            "alternatives": [_hypothesis_digest(h) for h in alternatives[:3]],
        }
        prompt = json.dumps(redact_obj(payload), default=str)
        try:
            return await self._call("narrate", prompt, Narrative, calls), self.name
        except LLMError:
            narrative, _ = await self.fallback.narrate(ctx, top, alternatives, calls)
            return narrative, f"{RULE_BASED} (llm failed)"
