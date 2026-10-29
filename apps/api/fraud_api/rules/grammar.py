"""Rule condition grammar: a closed JSON language, validated up front and interpreted, never eval'd.

    condition := predicate
               | {"all": [condition, ...]}      # AND, 1..16 children
               | {"any": [condition, ...]}      # OR,  1..16 children
               | {"not": condition}
    predicate := {"field": <field>, "op": <op>, "value": <typed literal>}
               | {"field": <field>, "op": "is_null"}

Fields come from ``FIELDS`` (an allowlist of facts known at scoring time); each field has a type
that fixes its operators and the JSON type of its literal. Complexity is bounded (depth, node
count, list sizes, string lengths). Evaluation uses three-valued (Kleene) logic: a comparison on
a missing value (``null``: degraded score, no history, absent region) is *unknown*, not false, and
a rule only matches when its condition is *true*. ``is_null`` is the only predicate that is never
unknown.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from fraud_ml.domains.card_payments import CHANNELS, MERCHANT_CATEGORIES
from fraud_ml.features.spec import NUMERIC_FEATURES

MAX_DEPTH: Final = 4  # nesting of all/any/not
MAX_NODES: Final = 32  # predicates + combinators
MAX_CHILDREN: Final = 16
MAX_LIST_VALUES: Final = 100
MAX_STRING: Final = 200
MAX_JSON_BYTES: Final = 8192


class FieldType(StrEnum):
    DECIMAL = "decimal"  # exact money comparison
    NUMBER = "number"  # float (scores, features)
    STRING = "string"  # free identifier
    ENUM = "enum"  # closed set of strings


@dataclass(frozen=True, slots=True)
class FieldSpec:
    type: FieldType
    nullable: bool
    description: str
    values: tuple[str, ...] = ()  # ENUM only


_ORDERED_OPS: Final = frozenset({"eq", "ne", "lt", "lte", "gt", "gte"})
_SET_OPS: Final = frozenset({"eq", "ne", "in", "not_in"})
OPERATORS: Final[Mapping[FieldType, frozenset[str]]] = {
    FieldType.DECIMAL: _ORDERED_OPS,
    FieldType.NUMBER: _ORDERED_OPS,
    FieldType.STRING: _SET_OPS,
    FieldType.ENUM: _SET_OPS,
}

DECISIONS: Final = ("allow", "challenge", "review", "decline")

FIELDS: Final[Mapping[str, FieldSpec]] = {
    "amount": FieldSpec(FieldType.DECIMAL, False, "Transaction amount (exact decimal, USD)"),
    "category": FieldSpec(FieldType.ENUM, False, "Merchant category", MERCHANT_CATEGORIES),
    "channel": FieldSpec(FieldType.ENUM, False, "Derived payment channel", CHANNELS),
    "region": FieldSpec(FieldType.STRING, True, "Region from the request"),
    "merchant_id": FieldSpec(FieldType.STRING, True, "Merchant identifier from the request"),
    "customer_id": FieldSpec(FieldType.STRING, False, "Tokenized customer identifier"),
    "source": FieldSpec(
        FieldType.ENUM, False, "Event source label", ("live", "replay", "simulated")
    ),
    "status": FieldSpec(
        FieldType.ENUM, False, "Assessment status (degraded = no score)", ("scored", "degraded")
    ),
    "risk_score": FieldSpec(FieldType.NUMBER, True, "Model fraud probability (null when degraded)"),
    "risk_level": FieldSpec(
        FieldType.ENUM,
        False,
        "Risk level from the model-bound policy",
        ("low", "medium", "high", "critical", "unknown"),
    ),
    "policy_decision": FieldSpec(
        FieldType.ENUM, False, "Decision of the model-bound policy, before rules", DECISIONS
    ),
    **{
        f"feature.{name}": FieldSpec(
            FieldType.NUMBER, True, "Point-in-time online feature (null if undefined)"
        )
        for name in NUMERIC_FEATURES
    },
}

Facts = Mapping[str, object]
Truth = bool | None  # None = unknown


class RuleValidationError(ValueError):
    """Condition rejected. ``errors`` holds ``{"path", "message"}`` dicts; messages never echo
    submitted values."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors))
        self.errors = errors


class Node:
    def evaluate(self, facts: Facts) -> Truth:  # pragma: no cover - abstract
        raise NotImplementedError

    def fields(self) -> set[str]:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class All(Node):
    children: tuple[Node, ...]

    def evaluate(self, facts: Facts) -> Truth:
        result: Truth = True
        for child in self.children:
            v = child.evaluate(facts)
            if v is False:
                return False
            if v is None:
                result = None
        return result

    def fields(self) -> set[str]:
        return set().union(*(c.fields() for c in self.children))


@dataclass(frozen=True, slots=True)
class Any_(Node):
    children: tuple[Node, ...]

    def evaluate(self, facts: Facts) -> Truth:
        result: Truth = False
        for child in self.children:
            v = child.evaluate(facts)
            if v is True:
                return True
            if v is None:
                result = None
        return result

    def fields(self) -> set[str]:
        return set().union(*(c.fields() for c in self.children))


@dataclass(frozen=True, slots=True)
class Not(Node):
    child: Node

    def evaluate(self, facts: Facts) -> Truth:
        v = self.child.evaluate(facts)
        return None if v is None else not v

    def fields(self) -> set[str]:
        return self.child.fields()


_COMPARE: Final[Mapping[str, Callable[[Any, Any], bool]]] = {
    "eq": lambda a, b: bool(a == b),
    "ne": lambda a, b: bool(a != b),
    "lt": lambda a, b: bool(a < b),
    "lte": lambda a, b: bool(a <= b),
    "gt": lambda a, b: bool(a > b),
    "gte": lambda a, b: bool(a >= b),
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
}


@dataclass(frozen=True, slots=True)
class Predicate(Node):
    field: str
    op: str
    value: object  # Decimal | float | str | frozenset[...] | None (is_null)

    def evaluate(self, facts: Facts) -> Truth:
        actual = facts[self.field]  # KeyError = missing fact = evaluation error (fail safe)
        if isinstance(actual, float) and math.isnan(actual):
            actual = None
        if self.op == "is_null":
            return actual is None
        if actual is None:
            return None
        spec = FIELDS[self.field]
        if spec.type is FieldType.DECIMAL:
            if not isinstance(actual, Decimal):
                raise TypeError(f"fact {self.field} must be Decimal")
        elif spec.type is FieldType.NUMBER:
            if isinstance(actual, bool) or not isinstance(actual, (int, float)):
                raise TypeError(f"fact {self.field} must be a number")
        elif not isinstance(actual, str):
            raise TypeError(f"fact {self.field} must be a string")
        return _COMPARE[self.op](actual, self.value)

    def fields(self) -> set[str]:
        return {self.field}


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class _Compiler:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []
        self.nodes = 0

    def fail(self, path: str, message: str) -> None:
        self.errors.append({"path": path, "message": message})

    def literal(self, spec: FieldSpec, v: object, path: str) -> object:
        if spec.type in (FieldType.DECIMAL, FieldType.NUMBER):
            if not _is_number(v):
                self.fail(path, "must be a finite JSON number")
                return None
            assert isinstance(v, (int, float))
            return Decimal(str(v)) if spec.type is FieldType.DECIMAL else float(v)
        if not isinstance(v, str):
            self.fail(path, "must be a JSON string")
            return None
        if not 1 <= len(v) <= MAX_STRING:
            self.fail(path, f"must be 1..{MAX_STRING} characters")
            return None
        if spec.type is FieldType.ENUM and v not in spec.values:
            self.fail(path, "is not an allowed value for this field")
            return None
        return v

    def predicate(self, raw: dict[str, Any], path: str) -> Node | None:
        field, op = raw.get("field"), raw.get("op")
        expected = {"field", "op"} if op == "is_null" else {"field", "op", "value"}
        if set(raw) != expected:
            self.fail(path, f"predicate must have exactly the keys {sorted(expected)}")
            return None
        if not isinstance(field, str) or field not in FIELDS:
            self.fail(f"{path}.field", "unknown field (see the field allowlist)")
            return None
        spec = FIELDS[field]
        if op == "is_null":
            if not spec.nullable:
                self.fail(f"{path}.op", "is_null is only allowed on nullable fields")
                return None
            return Predicate(field, "is_null", None)
        if not isinstance(op, str) or op not in OPERATORS[spec.type]:
            self.fail(f"{path}.op", f"operator not allowed for {spec.type.value} field")
            return None
        value = raw["value"]
        if op in ("in", "not_in"):
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_LIST_VALUES:
                self.fail(f"{path}.value", f"must be a list of 1..{MAX_LIST_VALUES} values")
                return None
            items = [self.literal(spec, v, f"{path}.value[{i}]") for i, v in enumerate(value)]
            return Predicate(field, op, frozenset(items))
        literal = self.literal(spec, value, f"{path}.value")
        return Predicate(field, op, literal)

    def node(self, raw: object, path: str, depth: int) -> Node | None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            if self.nodes == MAX_NODES + 1:
                self.fail(path, f"condition has more than {MAX_NODES} nodes")
            return None
        if not isinstance(raw, dict):
            self.fail(path, "must be a JSON object")
            return None
        if "field" in raw or "op" in raw:
            return self.predicate(raw, path)
        if len(raw) != 1:
            self.fail(path, "combinator must have exactly one key: all, any or not")
            return None
        ((key, body),) = raw.items()
        if key not in ("all", "any", "not"):
            self.fail(path, "unknown key (expected field/op/value, all, any or not)")
            return None
        if depth >= MAX_DEPTH:
            self.fail(path, f"nesting deeper than {MAX_DEPTH} levels")
            return None
        if key == "not":
            child = self.node(body, f"{path}.not", depth + 1)
            return Not(child) if child is not None else None
        if not isinstance(body, list) or not 1 <= len(body) <= MAX_CHILDREN:
            self.fail(f"{path}.{key}", f"must be a list of 1..{MAX_CHILDREN} conditions")
            return None
        children = [self.node(c, f"{path}.{key}[{i}]", depth + 1) for i, c in enumerate(body)]
        if any(c is None for c in children):
            return None
        kids = tuple(c for c in children if c is not None)
        return All(kids) if key == "all" else Any_(kids)


def compile_condition(raw: object) -> Node:
    """Validate ``raw`` against the grammar and return an immutable evaluation tree."""
    try:
        size = len(canonical_json(raw).encode())
    except (TypeError, ValueError):
        raise RuleValidationError([{"path": "$", "message": "not JSON-serializable"}]) from None
    if size > MAX_JSON_BYTES:
        raise RuleValidationError([{"path": "$", "message": f"larger than {MAX_JSON_BYTES} bytes"}])
    compiler = _Compiler()
    node = compiler.node(raw, "$", 0)
    if compiler.errors or node is None:
        raise RuleValidationError(compiler.errors or [{"path": "$", "message": "invalid"}])
    return node


def canonical_json(raw: object) -> str:
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)


def condition_sha256(raw: object) -> str:
    return hashlib.sha256(canonical_json(raw).encode()).hexdigest()


def grammar_description() -> dict[str, Any]:
    """Machine-readable grammar (served by ``GET /api/v1/rules/grammar``)."""
    return {
        "combinators": ["all", "any", "not"],
        "operators": {
            t.value: [*sorted(ops), "is_null (nullable fields)"] for t, ops in OPERATORS.items()
        },
        "fields": {
            name: {
                "type": spec.type.value,
                "nullable": spec.nullable,
                "description": spec.description,
                **({"values": list(spec.values)} if spec.values else {}),
            }
            for name, spec in FIELDS.items()
        },
        "limits": {
            "max_depth": MAX_DEPTH,
            "max_nodes": MAX_NODES,
            "max_children": MAX_CHILDREN,
            "max_list_values": MAX_LIST_VALUES,
            "max_string_length": MAX_STRING,
            "max_json_bytes": MAX_JSON_BYTES,
        },
        "logic": "three-valued: comparisons on null are unknown; a rule matches only when true",
    }
