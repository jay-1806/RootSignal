import pytest

from rootsignal.core.state_machine import (
    TERMINAL_STATES,
    InvalidTransitionError,
    assert_transition,
    can_transition,
)
from rootsignal.core.state_machine import InvestigationState as S

HAPPY_PATH = [
    S.RECEIVED,
    S.GATHERING_CONTEXT,
    S.HYPOTHESIZING,
    S.PLANNING,
    S.QUERYING,
    S.SCORING,
    S.CONCLUDED,
    S.AWAITING_APPROVAL,
    S.REMEDIATING,
    S.RESOLVED,
]


def test_happy_path_is_allowed():
    for current, target in zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False):
        assert can_transition(current, target), f"{current} -> {target}"


def test_scoring_can_loop_back_to_refine_hypotheses():
    assert can_transition(S.SCORING, S.HYPOTHESIZING)


def test_cannot_skip_straight_to_remediation():
    assert not can_transition(S.RECEIVED, S.REMEDIATING)
    assert not can_transition(S.CONCLUDED, S.REMEDIATING)  # must go through approval


@pytest.mark.parametrize("state", [s for s in S if s not in TERMINAL_STATES])
def test_any_active_state_can_fail(state):
    assert can_transition(state, S.FAILED)


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
@pytest.mark.parametrize("target", list(S))
def test_terminal_states_are_final(state, target):
    assert not can_transition(state, target)


def test_assert_transition_raises_with_details():
    with pytest.raises(InvalidTransitionError) as exc:
        assert_transition(S.RECEIVED, S.CONCLUDED)
    assert exc.value.current == S.RECEIVED
    assert exc.value.target == S.CONCLUDED
