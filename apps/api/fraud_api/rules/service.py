"""Rule versions, activation and the per-assessment rule trace.

* Creating a rule writes a new immutable version (``max(version) + 1`` for the key, serialized by
  a transaction-scoped advisory lock) together with its audit record.
* Activation changes ``rule_activations`` and writes its audit record in one database
  transaction, under a per-tenant advisory lock, so concurrent activations are serialized and the
  active-rule limit cannot be exceeded. An optional compare-and-set guard
  (``expected_active_version``) turns a lost update into 409.
* Scoring reads the active versions inside the scoring transaction. Compiled conditions are cached
  by version id, which is safe because versions are immutable.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Final, Literal

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from fraud_api.core.errors import APIError
from fraud_api.core.logging import get_logger
from fraud_api.db.models import AuditLog, RuleActivation, RuleDefinition, RuleEvaluation
from fraud_api.rules.engine import ActiveRule, RulesResult
from fraud_api.rules.grammar import (
    Node,
    RuleValidationError,
    compile_condition,
    condition_sha256,
)
from fraud_api.rules.schemas import (
    ActivationOut,
    RuleCreate,
    RuleDetailOut,
    RuleListOut,
    RuleSummaryOut,
    RuleVersionOut,
)
from fraud_api.scoring.schemas import RuleEvaluationOut, RuleRef, RulesSummary, utcnow
from fraud_ml.features.spec import NUMERIC_FEATURES

log = get_logger(__name__)

MAX_ACTIVE_RULES: Final = 100

_compiled: dict[uuid.UUID, Node | None] = {}


def _lock(session: Session, key: str) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})


def _audit(
    session: Session,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    resource_id: str,
    details: dict[str, Any],
    correlation_id: str | None,
) -> None:
    session.add(
        AuditLog(
            organization_id=org_id,
            actor_user_id=actor_id,
            action=action,
            resource_type="rule",
            resource_id=resource_id,
            details=details,
            correlation_id=correlation_id,
        )
    )


def _version_out(d: RuleDefinition, active_id: uuid.UUID | None) -> RuleVersionOut:
    return RuleVersionOut(
        id=d.id,
        rule_key=d.rule_key,
        version=d.version,
        name=d.name,
        description=d.description,
        priority=d.priority,
        action=d.action,
        condition=d.condition,
        condition_sha256=d.condition_sha256,
        created_by=d.created_by,
        created_at=d.created_at,
        active=d.id == active_id,
    )


# ---------------------------------------------------------------- management


def create_rule(
    session: Session,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    body: RuleCreate,
    correlation_id: str | None,
) -> RuleVersionOut:
    try:
        compile_condition(body.condition)
    except RuleValidationError as exc:
        raise APIError(
            422, "invalid_rule", "Rule condition is not valid", details=exc.errors
        ) from exc
    _lock(session, f"rule-version:{org_id}:{body.rule_key}")
    latest = session.scalar(
        select(func.max(RuleDefinition.version)).where(
            RuleDefinition.organization_id == org_id, RuleDefinition.rule_key == body.rule_key
        )
    )
    d = RuleDefinition(
        organization_id=org_id,
        rule_key=body.rule_key,
        version=(latest or 0) + 1,
        name=body.name,
        description=body.description,
        priority=body.priority,
        action=body.action,
        condition=body.condition,
        condition_sha256=condition_sha256(body.condition),
        created_by=actor_id,
    )
    session.add(d)
    session.flush()
    _audit(
        session,
        org_id,
        actor_id,
        "rule.create",
        str(d.id),
        {
            "rule_key": d.rule_key,
            "version": d.version,
            "action": d.action,
            "priority": d.priority,
            "condition_sha256": d.condition_sha256,
        },
        correlation_id,
    )
    session.commit()
    session.refresh(d)
    active_id = session.scalar(
        select(RuleActivation.rule_definition_id).where(
            RuleActivation.organization_id == org_id, RuleActivation.rule_key == d.rule_key
        )
    )
    return _version_out(d, active_id)


def list_rules(session: Session, org_id: uuid.UUID) -> RuleListOut:
    stats = session.execute(
        select(
            RuleDefinition.rule_key,
            func.max(RuleDefinition.version).label("latest"),
            func.count().label("n"),
        )
        .where(RuleDefinition.organization_id == org_id)
        .group_by(RuleDefinition.rule_key)
        .order_by(RuleDefinition.rule_key)
    ).all()
    active = {
        a.rule_key: (a, d)
        for a, d in session.execute(
            select(RuleActivation, RuleDefinition)
            .join(RuleDefinition, RuleDefinition.id == RuleActivation.rule_definition_id)
            .where(RuleActivation.organization_id == org_id)
        ).all()
    }
    items = []
    for s in stats:
        act = active.get(s.rule_key)
        items.append(
            RuleSummaryOut(
                rule_key=s.rule_key,
                latest_version=s.latest,
                versions=s.n,
                active_version=act[1].version if act else None,
                active=_version_out(act[1], act[1].id) if act else None,
                activated_at=act[0].activated_at if act else None,
            )
        )
    return RuleListOut(active_count=len(active), max_active=MAX_ACTIVE_RULES, items=items)


def get_rule(session: Session, org_id: uuid.UUID, rule_key: str) -> RuleDetailOut:
    versions = session.scalars(
        select(RuleDefinition)
        .where(RuleDefinition.organization_id == org_id, RuleDefinition.rule_key == rule_key)
        .order_by(RuleDefinition.version.desc())
    ).all()
    if not versions:
        raise APIError(404, "not_found", "Rule not found")
    act = session.get(RuleActivation, (org_id, rule_key))
    active_id = act.rule_definition_id if act else None
    return RuleDetailOut(
        rule_key=rule_key,
        active_version=next((v.version for v in versions if v.id == active_id), None),
        activated_at=act.activated_at if act else None,
        versions=[_version_out(v, active_id) for v in versions],
    )


def get_rule_version(
    session: Session, org_id: uuid.UUID, rule_key: str, version: int
) -> RuleVersionOut:
    d = session.scalar(
        select(RuleDefinition).where(
            RuleDefinition.organization_id == org_id,
            RuleDefinition.rule_key == rule_key,
            RuleDefinition.version == version,
        )
    )
    if d is None:
        raise APIError(404, "not_found", "Rule version not found")
    act = session.get(RuleActivation, (org_id, rule_key))
    return _version_out(d, act.rule_definition_id if act else None)


def _current(session: Session, org_id: uuid.UUID, rule_key: str) -> tuple[int | None, Any]:
    row = session.execute(
        select(RuleDefinition.version, RuleActivation)
        .join(RuleDefinition, RuleDefinition.id == RuleActivation.rule_definition_id)
        .where(RuleActivation.organization_id == org_id, RuleActivation.rule_key == rule_key)
    ).first()
    return (row[0], row[1]) if row else (None, None)


def _check_expected(expected: int | Literal["none"] | None, current: int | None) -> None:
    if expected is None:
        return
    if (expected == "none" and current is not None) or (expected != "none" and expected != current):
        raise APIError(
            409,
            "activation_conflict",
            f"Active version is {current if current is not None else 'none'}, not {expected}",
        )


def activate_rule(
    session: Session,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    rule_key: str,
    version: int,
    expected: int | Literal["none"] | None,
    correlation_id: str | None,
) -> ActivationOut:
    # One lock per tenant serializes all activation changes (and the active-rule count check).
    _lock(session, f"rule-activation:{org_id}")
    d = session.scalar(
        select(RuleDefinition).where(
            RuleDefinition.organization_id == org_id,
            RuleDefinition.rule_key == rule_key,
            RuleDefinition.version == version,
        )
    )
    if d is None:
        session.rollback()
        raise APIError(404, "not_found", "Rule version not found")
    current, act = _current(session, org_id, rule_key)
    try:
        _check_expected(expected, current)
    except APIError:
        session.rollback()
        raise
    if current == version:
        session.rollback()
        return ActivationOut(
            rule_key=rule_key,
            active_version=version,
            previous_version=version,
            changed=False,
            activated_at=act.activated_at,
        )
    if current is None:
        count = session.scalar(
            select(func.count())
            .select_from(RuleActivation)
            .where(RuleActivation.organization_id == org_id)
        )
        if (count or 0) >= MAX_ACTIVE_RULES:
            session.rollback()
            raise APIError(
                409,
                "too_many_active_rules",
                f"At most {MAX_ACTIVE_RULES} rules may be active per organization",
            )
    now = utcnow()
    stmt = insert(RuleActivation).values(
        organization_id=org_id,
        rule_key=rule_key,
        rule_definition_id=d.id,
        activated_by=actor_id,
        activated_at=now,
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[RuleActivation.organization_id, RuleActivation.rule_key],
            set_={
                "rule_definition_id": d.id,
                "activated_by": actor_id,
                "activated_at": now,
            },
        )
    )
    _audit(
        session,
        org_id,
        actor_id,
        "rule.activate",
        str(d.id),
        {"rule_key": rule_key, "version": version, "previous_version": current},
        correlation_id,
    )
    session.commit()
    return ActivationOut(
        rule_key=rule_key,
        active_version=version,
        previous_version=current,
        changed=True,
        activated_at=now,
    )


def deactivate_rule(
    session: Session,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    rule_key: str,
    expected: int | Literal["none"] | None,
    correlation_id: str | None,
) -> ActivationOut:
    _lock(session, f"rule-activation:{org_id}")
    exists = session.scalar(
        select(func.count())
        .select_from(RuleDefinition)
        .where(RuleDefinition.organization_id == org_id, RuleDefinition.rule_key == rule_key)
    )
    if not exists:
        session.rollback()
        raise APIError(404, "not_found", "Rule not found")
    current, act = _current(session, org_id, rule_key)
    try:
        _check_expected(expected, current)
    except APIError:
        session.rollback()
        raise
    if act is None:
        session.rollback()
        return ActivationOut(
            rule_key=rule_key,
            active_version=None,
            previous_version=None,
            changed=False,
            activated_at=None,
        )
    definition_id = act.rule_definition_id
    session.delete(act)
    _audit(
        session,
        org_id,
        actor_id,
        "rule.deactivate",
        str(definition_id),
        {"rule_key": rule_key, "version": None, "previous_version": current},
        correlation_id,
    )
    session.commit()
    return ActivationOut(
        rule_key=rule_key,
        active_version=None,
        previous_version=current,
        changed=True,
        activated_at=None,
    )


# ---------------------------------------------------------------- scoring path


def _compile_cached(d: RuleDefinition) -> Node | None:
    if d.id not in _compiled:
        try:
            node: Node | None = compile_condition(d.condition)
        except RuleValidationError:
            # Only possible if a row bypassed the API. Evaluated as an error (fail safe).
            log.error("rule_condition_invalid", rule_key=d.rule_key, version=d.version)
            node = None
        if len(_compiled) > 10_000:
            _compiled.clear()
        _compiled[d.id] = node
    return _compiled[d.id]


def load_active_rules(session: Session, org_id: uuid.UUID) -> list[ActiveRule]:
    rows = session.scalars(
        select(RuleDefinition)
        .join(RuleActivation, RuleActivation.rule_definition_id == RuleDefinition.id)
        .where(RuleActivation.organization_id == org_id)
    ).all()
    return [
        ActiveRule(d.id, d.rule_key, d.version, d.priority, d.action, _compile_cached(d))
        for d in rows
    ]


def build_facts(
    *,
    amount: Decimal,
    category: str,
    channel: str,
    region: str | None,
    merchant_id: str | None,
    customer_id: str,
    source: str,
    status: str,
    risk_score: float | None,
    risk_level: str,
    policy_decision: str,
    features: dict[str, float | str] | None,
) -> dict[str, object]:
    """Facts available at scoring time (request, derived channel, point-in-time features, model
    score and policy outcome). Nothing from the future and no labels."""
    facts: dict[str, object] = {
        "amount": amount,
        "category": category,
        "channel": channel,
        "region": region,
        "merchant_id": merchant_id,
        "customer_id": customer_id,
        "source": source,
        "status": status,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "policy_decision": policy_decision,
    }
    for name in NUMERIC_FEATURES:
        value = features.get(name) if features is not None else None
        facts[f"feature.{name}"] = float(value) if isinstance(value, (int, float)) else None
    return facts


def rule_reasons(result: RulesResult) -> list[str]:
    out = []
    for e in result.evaluations:
        r = e.rule
        if e.outcome == "matched":
            out.append(f"rule {r.rule_key}@v{r.version}: {r.action}")
        elif e.outcome == "error":
            out.append(f"rule_error {r.rule_key}@v{r.version}: escalated")
    return out


def persist_evaluations(
    session: Session, org_id: uuid.UUID, assessment_id: uuid.UUID, result: RulesResult
) -> None:
    for e in result.evaluations:
        session.add(
            RuleEvaluation(
                organization_id=org_id,
                risk_assessment_id=assessment_id,
                rule_definition_id=e.rule.id,
                rule_key=e.rule.rule_key,
                outcome=e.outcome,
                influenced_decision=e.influenced_decision,
                error_type=e.error_type,
            )
        )
        if e.outcome == "error":
            log.warning(
                "rule_evaluation_failed",
                rule_key=e.rule.rule_key,
                version=e.rule.version,
                error_type=e.error_type,
            )


def _summary(status: str, evaluations: Sequence[RuleEvaluationOut]) -> RulesSummary:
    def ref(e: RuleEvaluationOut) -> RuleRef:
        return RuleRef(rule_id=e.rule_id, rule_key=e.rule_key, version=e.version, action=e.action)

    return RulesSummary(
        status=status,
        evaluated=len(evaluations),
        matched=[ref(e) for e in evaluations if e.outcome == "matched"],
        errors=[ref(e) for e in evaluations if e.outcome == "error"],
        decided_by=next((ref(e) for e in evaluations if e.influenced_decision), None),
    )


def evaluations_from_result(result: RulesResult) -> list[RuleEvaluationOut]:
    return [
        RuleEvaluationOut(
            rule_id=e.rule.id,
            rule_key=e.rule.rule_key,
            version=e.rule.version,
            action=e.rule.action,
            priority=e.rule.priority,
            outcome=e.outcome,
            influenced_decision=e.influenced_decision,
            error_type=e.error_type,
        )
        for e in result.evaluations
    ]


def summary_from_result(result: RulesResult) -> RulesSummary:
    return _summary(result.status, evaluations_from_result(result))


def stored_evaluations(session: Session, assessment_id: uuid.UUID) -> list[RuleEvaluationOut]:
    rows = session.execute(
        select(RuleEvaluation, RuleDefinition)
        .join(RuleDefinition, RuleDefinition.id == RuleEvaluation.rule_definition_id)
        .where(RuleEvaluation.risk_assessment_id == assessment_id)
        .order_by(RuleDefinition.priority.desc(), RuleDefinition.rule_key)
    ).all()
    return [
        RuleEvaluationOut(
            rule_id=d.id,
            rule_key=d.rule_key,
            version=d.version,
            action=d.action,
            priority=d.priority,
            outcome=e.outcome,
            influenced_decision=e.influenced_decision,
            error_type=e.error_type,
        )
        for e, d in rows
    ]


def stored_summary(
    session: Session, assessment_id: uuid.UUID, rules_status: str
) -> tuple[RulesSummary, list[RuleEvaluationOut]]:
    evaluations = [] if rules_status == "none" else stored_evaluations(session, assessment_id)
    return _summary(rules_status, evaluations), evaluations
