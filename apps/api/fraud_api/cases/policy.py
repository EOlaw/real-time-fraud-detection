"""Case lifecycle and permission policy (pure; enforced by the service and the database).

Status machine::

    new ──► under_review ◄──► escalated
               │                  │
               └──────┬───────────┘
                      ▼  (resolve: sets resolved_at)
     resolved_fraud | resolved_legitimate | inconclusive
                      │
                      └──► under_review   (reopen: admin only; clears resolved_at)

* ``new`` cannot be resolved directly: someone must review it first.
* Closed statuses are reached only through *resolve*, which sets ``resolved_at`` in the same
  update; reopening clears it. The database checks that a case is closed exactly when
  ``resolved_at`` is set.

Who may do what (the API enforces this; the UI only hides controls):

=====================  =====  =======  ======  =======
action                 admin  analyst  viewer  service
=====================  =====  =======  ======  =======
read cases and labels    ✓       ✓        ✓       -
open a case              ✓       ✓        -       -
add a note               ✓       ✓        -       -
claim / release self     ✓       ✓        -       -
assign to someone else   ✓       -        -       -
change status, resolve   ✓    assignee    -       -
reopen a closed case     ✓       -        -       -
record a label           ✓       ✓        -       -
=====================  =====  =======  ======  =======

Nothing here touches an assessment: case status and labels never change a score or decision.
"""

from __future__ import annotations

import uuid
from typing import Final, Literal

from fraud_api.core.security import Role

OPEN_STATUSES: Final = ("new", "under_review", "escalated")
CLOSED_STATUSES: Final = ("resolved_fraud", "resolved_legitimate", "inconclusive")
TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "new": frozenset({"under_review", "escalated"}),
    "under_review": frozenset({"escalated", *CLOSED_STATUSES}),
    "escalated": frozenset({"under_review", *CLOSED_STATUSES}),
    **{s: frozenset({"under_review"}) for s in CLOSED_STATUSES},
}
OUTCOME_STATUS: Final = {
    "fraud": "resolved_fraud",
    "legitimate": "resolved_legitimate",
    "inconclusive": "inconclusive",
}
# Degraded assessments (risk level "unknown") have no score: treat them as high severity rather
# than letting a missing score look low risk.
SEVERITY_FOR_RISK: Final = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "critical": "critical",
    "unknown": "high",
}
SEVERITY_ORDER: Final = {"low": 0, "medium": 1, "high": 2, "critical": 3}

TransitionKind = Literal["status_changed", "resolved", "reopened"]


class PolicyError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def check_transition(
    current: str,
    target: str,
    role: Role,
    actor_id: uuid.UUID,
    assigned_to: uuid.UUID | None,
) -> TransitionKind:
    if role not in (Role.ADMIN, Role.ANALYST):
        raise PolicyError(403, "forbidden", "Insufficient role for this operation")
    if target not in TRANSITIONS:
        raise PolicyError(422, "invalid_status", "Unknown case status")
    if target == current:
        raise PolicyError(409, "invalid_transition", f"Case is already {current}")
    if target not in TRANSITIONS[current]:
        raise PolicyError(
            409,
            "invalid_transition",
            f"Cannot move a case from {current} to {target}"
            + ("; review it first" if current == "new" and target in CLOSED_STATUSES else ""),
        )
    reopening = current in CLOSED_STATUSES
    if reopening and role is not Role.ADMIN:
        raise PolicyError(403, "forbidden", "Only an admin can reopen a closed case")
    if role is Role.ANALYST and assigned_to != actor_id:
        raise PolicyError(
            403,
            "not_assignee",
            "Only the assigned analyst (or an admin) can change this case; claim it first",
        )
    if reopening:
        return "reopened"
    return "resolved" if target in CLOSED_STATUSES else "status_changed"


def check_assignment(
    role: Role, actor_id: uuid.UUID, current: uuid.UUID | None, target: uuid.UUID | None
) -> None:
    """Analysts may claim an unassigned case or release their own; admins may assign anyone."""
    if role is Role.ADMIN:
        return
    if role is not Role.ANALYST:
        raise PolicyError(403, "forbidden", "Insufficient role for this operation")
    claiming = current is None and target == actor_id
    releasing = current == actor_id and target is None
    if not (claiming or releasing):
        raise PolicyError(
            403,
            "forbidden",
            "Analysts can claim an unassigned case or release their own; ask an admin to reassign",
        )
