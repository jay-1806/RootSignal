"""Investigation state machine.

Code decides the control flow. The LLM is only asked to plan and reason inside a
state. Every transition is checked here and recorded in the investigation's history.
"""

from enum import StrEnum


class InvestigationState(StrEnum):
    RECEIVED = "received"  # alert arrived
    GATHERING_CONTEXT = "gathering_context"  # services, recent deploys, similar incidents
    HYPOTHESIZING = "hypothesizing"  # LLM proposes possible causes
    PLANNING = "planning"  # LLM picks telemetry queries per hypothesis
    QUERYING = "querying"  # code runs the queries (read-only)
    SCORING = "scoring"  # evidence updates each hypothesis's confidence
    CONCLUDED = "concluded"  # RCA written
    AWAITING_APPROVAL = "awaiting_approval"  # fix proposed, waiting for a human
    REMEDIATING = "remediating"  # approved fix running
    RESOLVED = "resolved"
    FAILED = "failed"


S = InvestigationState

TERMINAL_STATES: frozenset[InvestigationState] = frozenset({S.RESOLVED, S.FAILED})

# Allowed moves. Any non-terminal state may also move to FAILED.
TRANSITIONS: dict[InvestigationState, frozenset[InvestigationState]] = {
    S.RECEIVED: frozenset({S.GATHERING_CONTEXT}),
    S.GATHERING_CONTEXT: frozenset({S.HYPOTHESIZING}),
    S.HYPOTHESIZING: frozenset({S.PLANNING}),
    S.PLANNING: frozenset({S.QUERYING}),
    S.QUERYING: frozenset({S.SCORING}),
    # After scoring: loop back to refine hypotheses, or conclude.
    S.SCORING: frozenset({S.HYPOTHESIZING, S.CONCLUDED}),
    # A human can accept the RCA without any fix.
    S.CONCLUDED: frozenset({S.AWAITING_APPROVAL, S.RESOLVED}),
    # Rejecting the fix still leaves a finished investigation.
    S.AWAITING_APPROVAL: frozenset({S.REMEDIATING, S.RESOLVED}),
    S.REMEDIATING: frozenset({S.RESOLVED}),
    S.RESOLVED: frozenset(),
    S.FAILED: frozenset(),
}


class InvalidTransitionError(ValueError):
    def __init__(self, current: InvestigationState, target: InvestigationState):
        super().__init__(f"Cannot move investigation from '{current}' to '{target}'")
        self.current = current
        self.target = target


def can_transition(current: InvestigationState, target: InvestigationState) -> bool:
    if current in TERMINAL_STATES:
        return False
    if target == S.FAILED:
        return True
    return target in TRANSITIONS[current]


def assert_transition(current: InvestigationState, target: InvestigationState) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)
