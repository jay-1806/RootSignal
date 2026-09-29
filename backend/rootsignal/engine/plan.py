"""Investigation plans: which checks test which hypothesis, and what result to expect.

Playbooks are the baseline plan for each category. The LLM can add checks on top, but
every hypothesis always gets its playbook checks. That keeps a weak or invalid LLM
plan from leaving a hypothesis untested.
"""

from enum import StrEnum

from pydantic import BaseModel

from rootsignal.core import topology
from rootsignal.core.models import Category
from rootsignal.engine.checks import CheckName, check_key, sanitize_pattern, valid_service


class Expect(StrEnum):
    ANOMALOUS = "anomalous"  # the hypothesis predicts this check will look abnormal
    NORMAL = "normal"  # the hypothesis predicts this check will look normal


class PlanItem(BaseModel):
    hypothesis_id: str
    check: CheckName
    service: str
    expect: Expect = Expect.ANOMALOUS
    pattern: str | None = None
    # If False, a result that doesn't match the expectation is simply ignored instead of
    # counting against the hypothesis (e.g. "restarts" before an OOM kill has happened).
    required: bool = True

    @property
    def key(self) -> tuple[str, str, str]:
        return check_key(self.check, self.service, self.pattern)


# (check, expect, pattern, required)
Step = tuple[CheckName, Expect, str | None, bool]

A, N = Expect.ANOMALOUS, Expect.NORMAL

PLAYBOOKS: dict[Category, list[Step]] = {
    Category.DEPLOY_REGRESSION: [
        (CheckName.VERSION_CHANGE, A, None, True),
        (CheckName.RECENT_CHANGES, A, None, True),
        (CheckName.ERROR_RATE, A, None, True),
        (CheckName.ERROR_LOGS, A, None, False),
    ],
    Category.CONFIG_CHANGE: [
        (CheckName.RECENT_CHANGES, A, None, True),
        (CheckName.VERSION_CHANGE, N, None, False),
        (CheckName.ERROR_RATE, A, None, False),
    ],
    Category.MEMORY_LEAK: [
        (CheckName.MEMORY, A, None, True),
        (CheckName.RESTARTS, A, None, False),
        (CheckName.LOG_SEARCH, A, "memory", False),
        (CheckName.VERSION_CHANGE, N, None, True),
    ],
    Category.DB_CONNECTION_EXHAUSTION: [
        (CheckName.DB_POOL, A, None, True),
        (CheckName.LOG_SEARCH, A, "database connection|pool", True),
        (CheckName.ERROR_RATE, A, None, False),
    ],
    Category.CACHE_DEGRADATION: [
        (CheckName.CACHE_LATENCY, A, None, True),
        (CheckName.LOG_SEARCH, A, "slow cache|cache", True),
        (CheckName.LATENCY_P95, A, None, False),
        (CheckName.DB_POOL, N, None, False),
    ],
    Category.DOWNSTREAM_FAILURE: [
        (CheckName.UPSTREAM_ERRORS, A, None, True),
        (CheckName.ERROR_RATE, A, None, False),
    ],
    Category.TRAFFIC_SURGE: [
        (CheckName.LATENCY_P95, A, None, True),
        (CheckName.RECENT_CHANGES, N, None, False),
    ],
}

# Categories that only make sense for services with a given capability.
REQUIRES: dict[Category, topology.Capability] = {
    Category.DB_CONNECTION_EXHAUSTION: topology.Capability.DATABASE,
    Category.CACHE_DEGRADATION: topology.Capability.CACHE,
}


def applicable(category: Category, service: str) -> bool:
    need = REQUIRES.get(category)
    return need is None or topology.has(service, need)


def playbook(hypothesis_id: str, category: Category, service: str) -> list[PlanItem]:
    return [
        PlanItem(
            hypothesis_id=hypothesis_id,
            check=check,
            service=service,
            expect=expect,
            pattern=pattern,
            required=required,
        )
        for check, expect, pattern, required in PLAYBOOKS.get(category, [])
    ]


def merge(baseline: list[PlanItem], extra: list[PlanItem]) -> list[PlanItem]:
    """Baseline items first, then extra items that test something new. Invalid extras are
    dropped: unknown services, bad service names, unsafe log patterns."""
    out = list(baseline)
    seen = {(i.hypothesis_id, i.key) for i in baseline}
    for item in extra:
        if not valid_service(item.service) or not topology.is_known(item.service):
            continue
        if item.check == CheckName.LOG_SEARCH:
            safe = sanitize_pattern(item.pattern)
            if safe is None:
                continue
            item = item.model_copy(update={"pattern": safe})
        elif item.pattern is not None:
            item = item.model_copy(update={"pattern": None})
        if (item.hypothesis_id, item.key) not in seen:
            seen.add((item.hypothesis_id, item.key))
            out.append(item)
    return out
