"""Rule grammar and decision precedence (no database)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from fraud_api.rules.engine import ActiveRule, apply_rules, evaluation_order
from fraud_api.rules.grammar import (
    FIELDS,
    MAX_DEPTH,
    MAX_NODES,
    Node,
    RuleValidationError,
    compile_condition,
    condition_sha256,
    grammar_description,
)
from fraud_api.rules.service import build_facts
from fraud_ml.features.spec import NUMERIC_FEATURES


def facts(**overrides: Any) -> dict[str, object]:
    base = build_facts(
        amount=Decimal("950.00"),
        category="shopping_net",
        channel="card_not_present",
        region=None,
        merchant_id="Acme",
        customer_id="cus_1",
        source="replay",
        status="scored",
        risk_score=0.02,
        risk_level="low",
        policy_decision="allow",
        features={n: 1.0 for n in NUMERIC_FEATURES} | {"seconds_since_prev_txn": float("nan")},
    )
    base.update(overrides)
    return base


def pred(field: str, op: str, value: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"field": field, "op": op}
    if op != "is_null":
        out["value"] = value
    return out


# ---------------------------------------------------------------- validation


def test_valid_condition_compiles_and_evaluates() -> None:
    node = compile_condition(
        {
            "all": [
                pred("amount", "gte", 900),
                pred("category", "in", ["shopping_net", "misc_net"]),
                {"not": pred("merchant_id", "eq", "Trusted Ltd")},
            ]
        }
    )
    assert node.evaluate(facts()) is True
    assert node.evaluate(facts(amount=Decimal("899.99"))) is False
    assert node.fields() == {"amount", "category", "merchant_id"}


@pytest.mark.parametrize(
    ("condition", "path_fragment"),
    [
        (pred("pan", "eq", "4111"), "field"),  # not on the allowlist
        (pred("is_fraud", "eq", 1), "field"),  # labels are never available at scoring time
        (pred("amount", "in", [1, 2]), "op"),  # set operator on a decimal field
        (pred("category", "gt", "a"), "op"),  # ordering on an enum
        (pred("amount", "gt", "900"), "value"),  # string literal for a number
        (pred("amount", "gt", True), "value"),  # bool is not a number
        (pred("risk_score", "gt", float("nan")), "$"),  # NaN is not JSON
        (pred("risk_score", "lt", 1e309), "$"),  # inf
        (pred("category", "eq", "crypto"), "value"),  # enum value outside the closed set
        (pred("merchant_id", "eq", ""), "value"),
        (pred("merchant_id", "eq", "x" * 201), "value"),
        (pred("amount", "is_null"), "op"),  # is_null only on nullable fields
        (pred("category", "in", []), "value"),
        (pred("category", "in", ["grocery_pos"] * 101), "value"),
        ({"field": "amount", "op": "gt", "value": 1, "extra": 1}, "$"),
        ({"field": "amount", "op": "gt"}, "$"),
        ({"all": []}, "all"),
        ({"all": [pred("amount", "gt", 1)], "any": [pred("amount", "gt", 1)]}, "$"),
        ({"xor": [pred("amount", "gt", 1)]}, "$"),
        ({"not": [pred("amount", "gt", 1)]}, "not"),
        ([pred("amount", "gt", 1)], "$"),
        ("__import__('os').system('true')", "$"),
        ({"field": "amount", "op": "__class__", "value": 1}, "op"),
    ],
)
def test_invalid_conditions_are_rejected(condition: Any, path_fragment: str) -> None:
    with pytest.raises(RuleValidationError) as exc:
        compile_condition(condition)
    assert exc.value.errors and any(path_fragment in e["path"] for e in exc.value.errors)


def test_errors_never_echo_submitted_values() -> None:
    with pytest.raises(RuleValidationError) as exc:
        compile_condition(pred("merchant_id", "eq", "SECRET-" + "x" * 300))
    assert "SECRET" not in str(exc.value)


def test_complexity_is_bounded() -> None:
    deep: dict[str, Any] = pred("amount", "gt", 1)
    for _ in range(MAX_DEPTH):
        deep = {"not": deep}
    compile_condition(deep)  # exactly MAX_DEPTH combinators: allowed
    with pytest.raises(RuleValidationError, match="nesting"):
        compile_condition({"not": deep})
    wide = {"any": [{"all": [pred("amount", "gt", i) for i in range(16)]} for _ in range(2)]}
    with pytest.raises(RuleValidationError, match=str(MAX_NODES)):
        compile_condition(wide)
    with pytest.raises(RuleValidationError, match="bytes"):
        compile_condition(pred("category", "in", ["grocery_pos" * 20] * 100))


def test_every_allowlisted_field_is_a_scoring_time_fact() -> None:
    assert set(FIELDS) == set(facts())
    described = grammar_description()["fields"]
    assert set(described) == set(FIELDS)


def test_condition_hash_is_canonical() -> None:
    a = {"all": [pred("amount", "gt", 1), pred("category", "eq", "travel")]}
    b = {"all": [{"value": 1, "op": "gt", "field": "amount"}, pred("category", "eq", "travel")]}
    assert condition_sha256(a) == condition_sha256(b)
    assert condition_sha256(a) != condition_sha256({"any": a["all"]})


# ---------------------------------------------------------------- three-valued logic


def test_missing_values_are_unknown_not_false() -> None:
    gt = compile_condition(pred("risk_score", "gt", 0.5))
    assert gt.evaluate(facts(risk_score=None)) is None
    assert (
        compile_condition({"not": pred("risk_score", "gt", 0.5)}).evaluate(facts(risk_score=None))
        is None
    )
    assert compile_condition(pred("risk_score", "is_null")).evaluate(facts(risk_score=None))
    # NaN features (no previous transaction) are missing too
    assert (
        compile_condition(pred("feature.seconds_since_prev_txn", "lt", 60)).evaluate(facts())
        is None
    )
    # any: true wins over unknown; all: false wins over unknown
    assert (
        compile_condition(
            {"any": [pred("risk_score", "gt", 0.5), pred("amount", "gt", 1)]}
        ).evaluate(facts(risk_score=None))
        is True
    )
    assert (
        compile_condition(
            {"all": [pred("risk_score", "gt", 0.5), pred("amount", "lt", 1)]}
        ).evaluate(facts(risk_score=None))
        is False
    )


def test_decimal_amounts_compare_exactly() -> None:
    node = compile_condition(pred("amount", "gte", 0.3))
    assert node.evaluate(facts(amount=Decimal("0.30"))) is True
    assert node.evaluate(facts(amount=Decimal("0.29"))) is False


def test_wrong_fact_type_is_an_error_not_a_match() -> None:
    node = compile_condition(pred("amount", "gt", 1))
    with pytest.raises(TypeError):
        node.evaluate(facts(amount=5.0))  # float where an exact Decimal is required


# ---------------------------------------------------------------- precedence


def rule(
    key: str, action: str, condition: Any, priority: int = 100, node: Node | None = None
) -> ActiveRule:
    compiled = node if node is not None else compile_condition(condition)
    return ActiveRule(uuid.uuid4(), key, 1, priority, action, compiled)


ALWAYS = pred("amount", "gt", 0)
NEVER = pred("amount", "lt", 0)


def test_no_rules_leaves_the_policy_decision_unchanged() -> None:
    for decision in ("allow", "challenge", "review", "decline"):
        r = apply_rules([], facts(), decision, "review")
        assert (r.status, r.decision, r.evaluations) == ("none", decision, ())


def test_most_severe_action_wins_and_rules_never_relax() -> None:
    rules = [
        rule("a", "challenge", ALWAYS),
        rule("b", "decline", ALWAYS),
        rule("c", "flag", ALWAYS),
    ]
    r = apply_rules(rules, facts(), "review", "review")
    assert r.decision == "decline" and r.policy_decision == "review"
    # a challenge rule cannot lower a policy decline
    r = apply_rules([rule("a", "challenge", ALWAYS)], facts(), "decline", "review")
    assert r.decision == "decline" and r.decided_by is None
    # flag records the hit only
    r = apply_rules([rule("c", "flag", ALWAYS)], facts(), "allow", "review")
    assert r.decision == "allow" and [e.outcome for e in r.evaluations] == ["matched"]
    assert r.decided_by is None


def test_priority_orders_evaluation_and_attribution() -> None:
    low = rule("zz_low", "decline", ALWAYS, priority=10)
    high = rule("aa_high", "decline", ALWAYS, priority=900)
    tie_b = rule("b", "decline", ALWAYS, priority=500)
    tie_a = rule("a", "decline", ALWAYS, priority=500)
    assert [x.rule_key for x in evaluation_order([low, tie_b, high, tie_a])] == [
        "aa_high",
        "a",
        "b",
        "zz_low",
    ]
    r = apply_rules([low, tie_b, tie_a], facts(), "allow", "review")
    assert r.decided_by is not None and r.decided_by.rule.rule_key == "a"
    assert sum(e.influenced_decision for e in r.evaluations) == 1
    # the decisive rule is the first one contributing the final action, even if lower priority
    r = apply_rules(
        [rule("p900", "review", ALWAYS, 900), rule("p1", "decline", ALWAYS, 1)],
        facts(),
        "allow",
        "review",
    )
    assert r.decision == "decline" and r.decided_by is not None
    assert r.decided_by.rule.rule_key == "p1"


def test_result_is_independent_of_input_order() -> None:
    rules = [
        rule(f"r{i}", a, ALWAYS if i % 2 else NEVER, i * 7 % 5)
        for i, a in enumerate(["flag", "challenge", "review", "decline", "review", "challenge"])
    ]
    a = apply_rules(rules, facts(), "allow", "review")
    b = apply_rules(list(reversed(rules)), facts(), "allow", "review")
    assert a == b


def test_unknown_outcome_does_not_match() -> None:
    r = apply_rules(
        [rule("score", "decline", pred("risk_score", "gt", 0.5))],
        facts(risk_score=None, status="degraded"),
        "review",
        "review",
    )
    assert r.decision == "review" and r.evaluations[0].outcome == "unknown"


class Exploding(Node):
    def evaluate(self, facts: Any) -> bool | None:
        raise RuntimeError("boom")

    def fields(self) -> set[str]:
        return set()


@pytest.mark.parametrize(
    ("action", "floor", "expected"),
    [
        ("flag", "review", "review"),
        ("challenge", "review", "review"),
        ("decline", "review", "decline"),
        ("flag", "decline", "decline"),
    ],
)
def test_rule_errors_fail_safe_and_never_allow(action: str, floor: str, expected: str) -> None:
    r = apply_rules([rule("bad", action, None, node=Exploding())], facts(), "allow", floor)
    assert r.status == "error" and r.decision == expected
    e = r.evaluations[0]
    assert (e.outcome, e.error_type, e.influenced_decision) == ("error", "RuntimeError", True)


def test_uncompilable_stored_rule_is_an_error() -> None:
    broken = ActiveRule(uuid.uuid4(), "broken", 3, 100, "flag", None)
    r = apply_rules([broken, rule("ok", "flag", ALWAYS)], facts(), "allow", "review")
    assert r.status == "error" and r.decision == "review"
    assert [e.outcome for e in r.evaluations] == ["error", "matched"]


def test_error_floor_must_not_be_lenient() -> None:
    with pytest.raises(ValueError):
        apply_rules([rule("a", "flag", ALWAYS)], facts(), "allow", "allow")
