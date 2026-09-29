"""Evidence scoring: turn observations into weighted evidence and a computed confidence.

confidence = sigmoid(logit(PRIOR) + K * sum(+w for support, -w for contradiction))

The LLM never gets to state a confidence number. Each check's reliability and each
observation's severity come from code in `checks.py`.
"""

import math

from rootsignal.core.models import Evidence, EvidenceKind, Hypothesis
from rootsignal.engine.checks import KIND, RELIABILITY, Observation
from rootsignal.engine.plan import Expect, PlanItem

PRIOR = 0.25
K = 1.5
MATCHED_NORMAL_FACTOR = 0.5  # "normal as predicted" is weaker support than a matching anomaly
MISSING_ANOMALY_FACTOR = 0.6  # predicted anomaly that didn't show up
PAST_INCIDENT_MAX_WEIGHT = 0.5


def evidence_for(item: PlanItem, obs: Observation) -> Evidence | None:
    """Evidence from one planned check, or None if the result tells us nothing."""
    if not obs.available:
        return None
    reliability = RELIABILITY[item.check]
    predicted_anomaly = item.expect == Expect.ANOMALOUS
    matches = obs.anomalous == predicted_anomaly

    if matches:
        factor = max(obs.severity, 0.3) if obs.anomalous else MATCHED_NORMAL_FACTOR
    elif not item.required:
        return None
    else:
        # Contradiction. An anomaly the hypothesis says shouldn't be there counts fully.
        factor = max(obs.severity, 0.5) if obs.anomalous else MISSING_ANOMALY_FACTOR

    return Evidence(
        kind=KIND.get(item.check, EvidenceKind.METRIC),
        check=item.check.value,
        service=item.service,
        summary=obs.summary,
        query=obs.query,
        supports=matches,
        weight=round(min(1.0, reliability * factor), 3),
        started_at=obs.started_at,
        data={"expected": item.expect.value, **obs.data},
    )


def past_incident_evidence(hypothesis: Hypothesis, similar: list[dict]) -> list[Evidence]:
    """Support from confirmed past incidents with the same category and service."""
    out = []
    for incident in similar:
        if (
            incident.get("category") == hypothesis.category
            and incident.get("service") == hypothesis.service
        ):
            similarity = float(incident.get("similarity", 0.0))
            out.append(
                Evidence(
                    kind=EvidenceKind.PAST_INCIDENT,
                    check="incident_memory",
                    service=hypothesis.service,
                    summary=(
                        f"similar confirmed incident ({similarity:.0%} match): "
                        f"{incident.get('root_cause', '')[:160]}"
                    ),
                    supports=True,
                    weight=round(PAST_INCIDENT_MAX_WEIGHT * similarity, 3),
                    data={"memory_id": incident.get("id"), "similarity": similarity},
                )
            )
    return out


def confidence(evidence: list[Evidence]) -> float:
    score = sum(e.weight if e.supports else -e.weight for e in evidence)
    logit = math.log(PRIOR / (1 - PRIOR)) + K * score
    return round(1 / (1 + math.exp(-logit)), 3)


def score(
    hypothesis: Hypothesis,
    items: list[PlanItem],
    observations: dict[tuple[str, str, str], Observation],
    similar: list[dict],
) -> Hypothesis:
    evidence = [
        e
        for item in items
        if item.hypothesis_id == hypothesis.id
        and (obs := observations.get(item.key)) is not None
        and (e := evidence_for(item, obs)) is not None
    ]
    evidence += past_incident_evidence(hypothesis, similar)
    evidence.sort(key=lambda e: (not e.supports, -e.weight))
    return hypothesis.model_copy(update={"evidence": evidence, "confidence": confidence(evidence)})
