"""Case lifecycle and assignment policy (no database)."""

from __future__ import annotations

import uuid

import pytest

from fraud_api.cases.policy import (
    CLOSED_STATUSES,
    OUTCOME_STATUS,
    SEVERITY_FOR_RISK,
    TRANSITIONS,
    PolicyError,
    check_assignment,
    check_transition,
)
from fraud_api.core.security import Role
from fraud_api.labels.metrics import _confusion

ME = uuid.UUID(int=1)
OTHER = uuid.UUID(int=2)


def code(fn: object, *args: object) -> tuple[int, str]:
    with pytest.raises(PolicyError) as exc:
        fn(*args)  # type: ignore[operator]
    return exc.value.status_code, exc.value.code


def test_legal_transitions_and_kinds() -> None:
    assert check_transition("new", "under_review", Role.ANALYST, ME, ME) == "status_changed"
    assert check_transition("under_review", "escalated", Role.ANALYST, ME, ME) == "status_changed"
    assert check_transition("escalated", "resolved_fraud", Role.ANALYST, ME, ME) == "resolved"
    assert check_transition("under_review", "inconclusive", Role.ADMIN, ME, None) == "resolved"
    assert check_transition("resolved_legitimate", "under_review", Role.ADMIN, ME, OTHER) == (
        "reopened"
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("new", "resolved_fraud"),  # must be reviewed first
        ("new", "inconclusive"),
        ("resolved_fraud", "resolved_legitimate"),  # closed -> closed
        ("resolved_fraud", "escalated"),
        ("under_review", "new"),  # never back to new
        ("escalated", "new"),
    ],
)
def test_illegal_transitions_are_409(current: str, target: str) -> None:
    assert code(check_transition, current, target, Role.ADMIN, ME, ME) == (
        409,
        "invalid_transition",
    )


def test_same_status_and_unknown_status() -> None:
    assert code(check_transition, "new", "new", Role.ADMIN, ME, ME)[1] == "invalid_transition"
    assert code(check_transition, "new", "approved", Role.ADMIN, ME, ME) == (
        422,
        "invalid_status",
    )


def test_role_and_assignment_rules() -> None:
    assert code(check_transition, "new", "under_review", Role.VIEWER, ME, ME)[1] == "forbidden"
    assert code(check_transition, "new", "under_review", Role.SERVICE, ME, ME)[1] == "forbidden"
    assert code(check_transition, "new", "under_review", Role.ANALYST, ME, OTHER) == (
        403,
        "not_assignee",
    )
    assert code(check_transition, "new", "under_review", Role.ANALYST, ME, None)[1] == (
        "not_assignee"
    )
    # admins act on any case; only admins reopen
    assert check_transition("new", "under_review", Role.ADMIN, ME, OTHER) == "status_changed"
    assert code(check_transition, "resolved_fraud", "under_review", Role.ANALYST, ME, ME) == (
        403,
        "forbidden",
    )


def test_assignment_policy() -> None:
    check_assignment(Role.ANALYST, ME, None, ME)  # claim
    check_assignment(Role.ANALYST, ME, ME, None)  # release own
    check_assignment(Role.ADMIN, ME, OTHER, ME)  # admin reassigns
    check_assignment(Role.ADMIN, ME, None, OTHER)
    for args in [
        (Role.ANALYST, ME, None, OTHER),
        (Role.ANALYST, ME, OTHER, ME),
        (Role.ANALYST, ME, OTHER, None),
        (Role.VIEWER, ME, None, ME),
    ]:
        assert code(check_assignment, *args)[1] == "forbidden"


def test_machine_is_closed_and_complete() -> None:
    assert set(TRANSITIONS) == {"new", "under_review", "escalated", *CLOSED_STATUSES}
    for targets in TRANSITIONS.values():
        assert targets <= set(TRANSITIONS)
    assert "new" not in set().union(*TRANSITIONS.values())  # nothing returns to new
    assert set(OUTCOME_STATUS.values()) == set(CLOSED_STATUSES)
    # a degraded assessment (no score) is never low severity
    assert SEVERITY_FOR_RISK["unknown"] == "high"


def test_confusion_rates_have_explicit_denominators() -> None:
    c = _confusion(tp=8, fp=2, fn=4, tn=86)
    assert (c.precision.numerator, c.precision.denominator, c.precision.rate) == (8, 10, 0.8)
    assert c.recall.denominator == 12 and c.false_positive_rate.denominator == 88
    empty = _confusion(0, 0, 0, 0)
    assert empty.precision.rate is None and empty.recall.rate is None
