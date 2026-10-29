"""Combining the model-bound policy decision with tenant rules.

Precedence (deterministic, documented in docs/architecture/rules-engine.md):

* Severity order: ``allow < challenge < review < decline``. A rule action is ``flag`` (record a
  hit, no decision change) or one of ``challenge|review|decline``. There is no ``allow`` action:
  rules can only escalate, never relax, the policy decision.
* Every active rule is evaluated, in order ``(priority DESC, rule_key ASC)``.
* Final decision = the most severe of: the policy decision, the action of every matched rule,
  and, for every rule whose evaluation failed, ``max(error_floor, rule action)`` where
  ``error_floor`` is the policy's ``on_scoring_failure`` (``review`` or ``decline``). A rule error
  therefore never yields ``allow``.
* ``decided_by`` is the first rule in evaluation order that contributed the final decision, and
  only when that decision is stricter than the policy decision; otherwise the policy decided.
* The model score and the policy decision are inputs only; nothing here changes them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from fraud_api.rules.grammar import Facts, Node

SEVERITY: Final = {"allow": 0, "challenge": 1, "review": 2, "decline": 3}
RULE_ACTIONS: Final = ("flag", "challenge", "review", "decline")
Outcome = Literal["matched", "not_matched", "unknown", "error"]
RulesStatus = Literal["none", "evaluated", "error"]


@dataclass(frozen=True, slots=True)
class ActiveRule:
    id: uuid.UUID
    rule_key: str
    version: int
    priority: int
    action: str
    node: Node | None  # None: the stored condition failed to compile (evaluated as an error)


@dataclass(frozen=True, slots=True)
class Evaluation:
    rule: ActiveRule
    outcome: Outcome
    influenced_decision: bool
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class RulesResult:
    status: RulesStatus
    policy_decision: str
    decision: str
    evaluations: tuple[Evaluation, ...]

    @property
    def decided_by(self) -> Evaluation | None:
        return next((e for e in self.evaluations if e.influenced_decision), None)


def evaluation_order(rules: Sequence[ActiveRule]) -> list[ActiveRule]:
    return sorted(rules, key=lambda r: (-r.priority, r.rule_key))


def _stricter(a: str, b: str) -> str:
    return a if SEVERITY[a] >= SEVERITY[b] else b


def apply_rules(
    rules: Sequence[ActiveRule], facts: Facts, policy_decision: str, error_floor: str
) -> RulesResult:
    if SEVERITY.get(error_floor, 0) < SEVERITY["review"]:
        raise ValueError("error_floor must be review or decline")
    if not rules:
        return RulesResult("none", policy_decision, policy_decision, ())
    staged: list[tuple[ActiveRule, Outcome, str | None, str | None]] = []
    final = policy_decision
    for rule in evaluation_order(rules):
        contributes: str | None = None
        error_type: str | None = None
        try:
            if rule.node is None:
                raise ValueError("stored condition does not compile")
            truth = rule.node.evaluate(facts)
            outcome: Outcome = (
                "matched" if truth is True else "unknown" if truth is None else "not_matched"
            )
            if outcome == "matched" and rule.action != "flag":
                contributes = rule.action
        except Exception as exc:  # fail safe: any evaluation failure escalates
            outcome, error_type = "error", type(exc).__name__
            contributes = (
                error_floor if rule.action == "flag" else _stricter(rule.action, error_floor)
            )
        if contributes is not None:
            final = _stricter(final, contributes)
        staged.append((rule, outcome, contributes, error_type))

    decisive_seen = False
    evaluations: list[Evaluation] = []
    for rule, outcome, contributes, error_type in staged:
        decisive = (
            not decisive_seen
            and contributes == final
            and SEVERITY[final] > SEVERITY[policy_decision]
        )
        decisive_seen = decisive_seen or decisive
        evaluations.append(Evaluation(rule, outcome, decisive, error_type))
    status: RulesStatus = "error" if any(e.outcome == "error" for e in evaluations) else "evaluated"
    return RulesResult(status, policy_decision, final, tuple(evaluations))
