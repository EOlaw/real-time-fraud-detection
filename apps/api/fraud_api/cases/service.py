"""Investigation cases: open, queue, inspect, assign, note, change status, resolve.

Every mutation:
* is tenant-scoped (the organization comes from the token; other tenants' ids are 404);
* names the case version it read and is applied with ``UPDATE … WHERE version = expected``, so a
  stale writer gets 409 ``version_conflict`` and never overwrites another analyst's change (a
  database trigger also requires ``version`` to increase by exactly one);
* writes its case event (status/assignment history) and an ``audit_logs`` record in the same
  database transaction; notes and events are append-only;
* never touches ``risk_assessments``: opening, resolving or labeling cannot weaken a decision or
  turn a failed score into ``allow``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import Select, asc, desc, func, select, update
from sqlalchemy import case as sql_case
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from fraud_api.cases.policy import (
    CLOSED_STATUSES,
    OUTCOME_STATUS,
    SEVERITY_FOR_RISK,
    TRANSITIONS,
    PolicyError,
    check_assignment,
    check_transition,
)
from fraud_api.cases.schemas import (
    Assignee,
    AssignRequest,
    CaseCreate,
    CaseDetail,
    CaseEventOut,
    CaseNoteOut,
    CaseOpened,
    CasePage,
    CaseSummary,
    LabelCreate,
    ModelInfo,
    NoteCreate,
    ResolveRequest,
    StatusRequest,
    TransactionInfo,
    UserRef,
)
from fraud_api.core.errors import APIError
from fraud_api.core.security import Principal
from fraud_api.db.models import (
    AuditLog,
    Case,
    CaseEvent,
    CaseNote,
    FraudLabel,
    ModelVersion,
    RiskAssessment,
    Transaction,
    User,
)
from fraud_api.labels.service import _users, add_label, build_history, database_unavailable


def _now() -> datetime:
    return datetime.now(UTC)


def _policy(exc: PolicyError) -> APIError:
    return APIError(exc.status_code, exc.code, exc.message)


def _audit(
    session: Session,
    p: Principal,
    action: str,
    case: Case,
    details: dict[str, Any],
    cid: str | None,
) -> None:
    session.add(
        AuditLog(
            organization_id=p.organization_id,
            actor_user_id=p.user_id,
            action=action,
            resource_type="case",
            resource_id=str(case.id),
            details=details,
            correlation_id=cid,
        )
    )


def _event(
    session: Session,
    p: Principal,
    case_id: uuid.UUID,
    kind: str,
    from_value: str | None,
    to_value: str | None,
    version: int,
) -> None:
    session.add(
        CaseEvent(
            organization_id=p.organization_id,
            case_id=case_id,
            actor_id=p.user_id,
            kind=kind,
            from_value=from_value,
            to_value=to_value,
            case_version=version,
            created_at=_now(),
        )
    )


def _load(session: Session, org_id: uuid.UUID, case_id: uuid.UUID) -> Case:
    case = session.scalar(select(Case).where(Case.id == case_id, Case.organization_id == org_id))
    if case is None:
        raise APIError(404, "not_found", "Case not found")
    return case


def _check_version(case: Case, expected: int) -> None:
    """Stale writers learn they are stale before anything else is evaluated."""
    if case.version != expected:
        raise APIError(
            409,
            "version_conflict",
            f"The case changed since you read it (your version {expected}, current "
            f"{case.version}); nothing was changed. Reload and retry.",
        )


def _bump(session: Session, case: Case, expected: int, **values: Any) -> int:
    """Compare-and-set update; returns the new version or raises 409 without writing."""
    _check_version(case, expected)
    result = session.execute(
        update(Case)
        .where(Case.id == case.id, Case.version == expected)
        .values(version=expected + 1, updated_at=_now(), **values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        session.rollback()
        raise APIError(
            409,
            "version_conflict",
            "The case was changed concurrently; nothing was changed. Reload and retry.",
        )
    # The UPDATE bypassed the ORM; drop the stale in-memory copy so reads see the new row.
    session.expire(case)
    return expected + 1


def _summary_query() -> Select[Case, Transaction, RiskAssessment]:
    return (
        select(Case, Transaction, RiskAssessment)
        .join(Transaction, Transaction.id == Case.transaction_id)
        .join(RiskAssessment, RiskAssessment.id == Case.risk_assessment_id)
    )


def _current_labels(session: Session, txn_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not txn_ids:
        return {}
    superseded = select(FraudLabel.supersedes_id).where(FraudLabel.supersedes_id.is_not(None))
    rows = session.execute(
        select(FraudLabel.transaction_id, FraudLabel.label).where(
            FraudLabel.transaction_id.in_(txn_ids), FraudLabel.id.not_in(superseded)
        )
    ).all()
    return {t: label for t, label in rows}


def _summary(
    case: Case,
    txn: Transaction,
    ra: RiskAssessment,
    emails: dict[uuid.UUID, str],
    labels: dict[uuid.UUID, str],
) -> dict[str, Any]:
    def ref(uid: uuid.UUID) -> UserRef:
        return UserRef(id=uid, email=emails.get(uid, "unknown"))

    return {
        "id": case.id,
        "transaction_id": txn.external_id,
        "assessment_id": ra.id,
        "source": txn.source,
        "severity": case.severity,
        "status": case.status,
        "assigned_to": ref(case.assigned_to) if case.assigned_to else None,
        "opened_by": ref(case.opened_by),
        "opened_at": case.opened_at,
        "resolved_at": case.resolved_at,
        "updated_at": case.updated_at,
        "version": case.version,
        "assessment_status": ra.status,
        "risk_score": ra.risk_score,
        "policy_decision": ra.policy_decision,
        "decision": ra.decision,
        "current_label": labels.get(txn.id),
    }


def allowed_transitions(case: Case, principal: Principal) -> list[str]:
    out = []
    for target in sorted(TRANSITIONS[case.status]):
        try:
            check_transition(
                case.status, target, principal.role, principal.user_id, case.assigned_to
            )
            out.append(target)
        except PolicyError:
            pass
    return out


def case_detail(
    session: Session,
    principal: Principal,
    case_id: uuid.UUID,
    serving_model_id: uuid.UUID | None,
) -> CaseDetail:
    org_id = principal.organization_id
    row = session.execute(
        _summary_query().where(Case.id == case_id, Case.organization_id == org_id)
    ).first()
    if row is None:
        raise APIError(404, "not_found", "Case not found")
    case, txn, ra = row
    events = list(
        session.scalars(
            select(CaseEvent).where(CaseEvent.case_id == case.id).order_by(CaseEvent.id)
        )
    )
    notes = list(
        session.scalars(select(CaseNote).where(CaseNote.case_id == case.id).order_by(CaseNote.id))
    )
    ids = {case.opened_by, *(e.actor_id for e in events), *(n.author_id for n in notes)}
    if case.assigned_to:
        ids.add(case.assigned_to)
    emails = _users(session, ids)
    history = build_history(session, org_id, txn)
    mv = session.get(ModelVersion, ra.model_version_id) if ra.model_version_id else None
    return CaseDetail(
        **_summary(
            case, txn, ra, emails, {txn.id: history.current.label} if history.current else {}
        ),
        transaction=TransactionInfo(
            transaction_id=txn.external_id,
            customer_id=txn.customer_id,
            merchant_id=txn.merchant_id,
            amount=float(txn.amount),
            currency=txn.currency,
            category=txn.category,
            channel=txn.channel,
            event_time=txn.event_time,
            source=txn.source,
        ),
        model=ModelInfo(
            name=mv.name if mv else None,
            version=mv.version if mv else None,
            registry_status=mv.status if mv else None,
            serving=mv is not None and mv.id == serving_model_id,
        ),
        events=[
            CaseEventOut(
                id=e.id,
                kind=e.kind,
                from_value=e.from_value,
                to_value=e.to_value,
                actor=UserRef(id=e.actor_id, email=emails.get(e.actor_id, "unknown")),
                case_version=e.case_version,
                created_at=e.created_at,
            )
            for e in events
        ],
        notes=[
            CaseNoteOut(
                id=n.id,
                author=UserRef(id=n.author_id, email=emails.get(n.author_id, "unknown")),
                body=n.body,
                evidence=n.evidence,
                created_at=n.created_at,
            )
            for n in notes
        ],
        labels=history,
        allowed_transitions=allowed_transitions(case, principal),
    )


def open_case(
    session: Session,
    principal: Principal,
    body: CaseCreate,
    cid: str | None,
    serving_model_id: uuid.UUID | None,
) -> CaseOpened:
    org_id = principal.organization_id
    try:
        ra = session.scalar(
            select(RiskAssessment).where(
                RiskAssessment.id == body.assessment_id, RiskAssessment.organization_id == org_id
            )
        )
        if ra is None:
            raise APIError(404, "not_found", "Assessment not found")
        existing = session.scalar(
            select(Case.id).where(
                Case.organization_id == org_id, Case.transaction_id == ra.transaction_id
            )
        )
        if existing is not None:
            return CaseOpened(
                created=False, case=case_detail(session, principal, existing, serving_model_id)
            )
        now = _now()
        case = Case(
            organization_id=org_id,
            transaction_id=ra.transaction_id,
            risk_assessment_id=ra.id,
            severity=SEVERITY_FOR_RISK.get(ra.risk_level, "high"),
            status="new",
            opened_by=principal.user_id,
            opened_at=now,
            updated_at=now,
            version=1,
        )
        session.add(case)
        try:
            session.flush()
        except IntegrityError:
            # A concurrent request opened the case for this transaction first.
            session.rollback()
            existing = session.scalar(
                select(Case.id).where(
                    Case.organization_id == org_id, Case.transaction_id == ra.transaction_id
                )
            )
            if existing is None:
                raise
            return CaseOpened(
                created=False, case=case_detail(session, principal, existing, serving_model_id)
            )
        _event(session, principal, case.id, "created", None, "new", 1)
        if body.note:
            session.add(
                CaseNote(
                    organization_id=org_id,
                    case_id=case.id,
                    author_id=principal.user_id,
                    body=body.note,
                    evidence=[],
                    created_at=now,
                )
            )
        _audit(
            session,
            principal,
            "case.create",
            case,
            {
                "assessment_id": str(ra.id),
                "severity": case.severity,
                "assessment_status": ra.status,
                "decision": ra.decision,
            },
            cid,
        )
        session.commit()
        return CaseOpened(
            created=True, case=case_detail(session, principal, case.id, serving_model_id)
        )
    except SQLAlchemyError as exc:
        session.rollback()
        raise database_unavailable(exc) from exc


SortKey = Literal["opened_at", "severity", "updated_at"]
AssignedFilter = Literal["any", "me", "unassigned"]


def list_cases(
    session: Session,
    principal: Principal,
    statuses: list[str] | None,
    severities: list[str] | None,
    assigned: AssignedFilter,
    sources: list[str] | None,
    sort: SortKey,
    order: Literal["asc", "desc"],
    limit: int,
    offset: int,
) -> CasePage:
    stmt = _summary_query().where(Case.organization_id == principal.organization_id)
    if statuses:
        stmt = stmt.where(Case.status.in_(statuses))
    if severities:
        stmt = stmt.where(Case.severity.in_(severities))
    if sources:
        stmt = stmt.where(Transaction.source.in_(sources))
    if assigned == "me":
        stmt = stmt.where(Case.assigned_to == principal.user_id)
    elif assigned == "unassigned":
        stmt = stmt.where(Case.assigned_to.is_(None))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    if sort == "severity":
        column: Any = sql_case(
            {"low": 0, "medium": 1, "high": 2, "critical": 3}, value=Case.severity
        )
    else:
        column = getattr(Case, sort)
    direction = desc(column) if order == "desc" else asc(column)
    rows = session.execute(
        stmt.order_by(direction, Case.opened_at.desc(), Case.id).limit(limit).offset(offset)
    ).all()
    ids: set[uuid.UUID] = set()
    for c, _, _ in rows:
        ids.add(c.opened_by)
        if c.assigned_to:
            ids.add(c.assigned_to)
    emails = _users(session, ids)
    labels = _current_labels(session, [t.id for _, t, _ in rows])
    return CasePage(
        total=total,
        limit=limit,
        offset=offset,
        items=[CaseSummary(**_summary(c, t, r, emails, labels)) for c, t, r in rows],
    )


def assignees(session: Session, org_id: uuid.UUID) -> list[Assignee]:
    rows = session.scalars(
        select(User)
        .where(
            User.organization_id == org_id,
            User.is_active.is_(True),
            User.role.in_(["admin", "analyst"]),
        )
        .order_by(User.email)
    )
    return [Assignee(id=u.id, email=u.email, role=u.role) for u in rows]


def _mutate(session: Session, fn: Any) -> Any:
    try:
        return fn()
    except SQLAlchemyError as exc:
        session.rollback()
        raise database_unavailable(exc) from exc


def assign_case(
    session: Session,
    principal: Principal,
    case_id: uuid.UUID,
    body: AssignRequest,
    cid: str | None,
    serving_model_id: uuid.UUID | None,
) -> CaseDetail:
    def run() -> CaseDetail:
        case = _load(session, principal.organization_id, case_id)
        _check_version(case, body.expected_version)
        try:
            check_assignment(principal.role, principal.user_id, case.assigned_to, body.assignee_id)
        except PolicyError as exc:
            raise _policy(exc) from exc
        if body.assignee_id is not None:
            target = session.scalar(
                select(User).where(
                    User.id == body.assignee_id,
                    User.organization_id == principal.organization_id,
                    User.is_active.is_(True),
                    User.role.in_(["admin", "analyst"]),
                )
            )
            if target is None:
                raise APIError(
                    422,
                    "invalid_assignee",
                    "Assignee must be an active analyst or admin of this organization",
                )
        if body.assignee_id == case.assigned_to:
            raise APIError(409, "no_change", "The case already has that assignment")
        if case.status in CLOSED_STATUSES:
            raise APIError(409, "case_closed", "A closed case cannot be reassigned; reopen it")
        before = case.assigned_to
        version = _bump(session, case, body.expected_version, assigned_to=body.assignee_id)
        _event(
            session,
            principal,
            case.id,
            "assigned",
            str(before) if before else None,
            str(body.assignee_id) if body.assignee_id else None,
            version,
        )
        _audit(
            session,
            principal,
            "case.assign",
            case,
            {
                "from": str(before) if before else None,
                "to": str(body.assignee_id) if body.assignee_id else None,
                "version": version,
            },
            cid,
        )
        session.commit()
        return case_detail(session, principal, case.id, serving_model_id)

    return _mutate(session, run)  # type: ignore[no-any-return]


def change_status(
    session: Session,
    principal: Principal,
    case_id: uuid.UUID,
    body: StatusRequest,
    cid: str | None,
    serving_model_id: uuid.UUID | None,
) -> CaseDetail:
    def run() -> CaseDetail:
        case = _load(session, principal.organization_id, case_id)
        _check_version(case, body.expected_version)
        try:
            kind = check_transition(
                case.status, body.status, principal.role, principal.user_id, case.assigned_to
            )
        except PolicyError as exc:
            raise _policy(exc) from exc
        before = case.status
        extra: dict[str, Any] = {"status": body.status}
        if kind == "reopened":
            extra["resolved_at"] = None
        version = _bump(session, case, body.expected_version, **extra)
        _event(session, principal, case.id, kind, before, body.status, version)
        _audit(
            session,
            principal,
            "case.reopen" if kind == "reopened" else "case.status",
            case,
            {"from": before, "to": body.status, "version": version},
            cid,
        )
        session.commit()
        return case_detail(session, principal, case.id, serving_model_id)

    return _mutate(session, run)  # type: ignore[no-any-return]


def resolve_case(
    session: Session,
    principal: Principal,
    case_id: uuid.UUID,
    body: ResolveRequest,
    cid: str | None,
    serving_model_id: uuid.UUID | None,
) -> CaseDetail:
    def run() -> CaseDetail:
        case = _load(session, principal.organization_id, case_id)
        _check_version(case, body.expected_version)
        target = OUTCOME_STATUS[body.outcome]
        try:
            check_transition(
                case.status, target, principal.role, principal.user_id, case.assigned_to
            )
        except PolicyError as exc:
            raise _policy(exc) from exc
        before = case.status
        resolved_at = _now()
        version = _bump(
            session, case, body.expected_version, status=target, resolved_at=resolved_at
        )
        _event(session, principal, case.id, "resolved", before, target, version)
        if body.note:
            session.add(
                CaseNote(
                    organization_id=principal.organization_id,
                    case_id=case.id,
                    author_id=principal.user_id,
                    body=body.note,
                    evidence=[],
                    created_at=resolved_at,
                )
            )
        label_id = None
        if body.record_label:
            txn = session.get(Transaction, case.transaction_id)
            assert txn is not None
            if build_history(session, principal.organization_id, txn).current is not None:
                session.rollback()
                raise APIError(
                    409,
                    "label_exists",
                    "The transaction already has a label; nothing was changed. Resolve without "
                    "record_label and correct the label through /labels (it keeps history).",
                )
            label = add_label(
                session,
                principal,
                LabelCreate(
                    transaction_id=txn.external_id,
                    label=body.outcome,
                    source="analyst_review",
                    confidence=body.label_confidence,
                    labeled_at=resolved_at,
                    case_id=case.id,
                ),
                cid,
                commit=False,
            )
            label_id = label.id
        _audit(
            session,
            principal,
            "case.resolve",
            case,
            {"from": before, "to": target, "version": version, "label_id": label_id},
            cid,
        )
        session.commit()
        return case_detail(session, principal, case.id, serving_model_id)

    return _mutate(session, run)  # type: ignore[no-any-return]


def add_note(
    session: Session,
    principal: Principal,
    case_id: uuid.UUID,
    body: NoteCreate,
    cid: str | None,
) -> CaseNoteOut:
    def run() -> CaseNoteOut:
        case = _load(session, principal.organization_id, case_id)
        note = CaseNote(
            organization_id=principal.organization_id,
            case_id=case.id,
            author_id=principal.user_id,
            body=body.body,
            evidence=body.evidence,
            created_at=_now(),
        )
        session.add(note)
        session.flush()
        _audit(
            session,
            principal,
            "case.note",
            case,
            {"note_id": note.id, "length": len(body.body), "evidence": len(body.evidence)},
            cid,
        )
        session.commit()
        email = _users(session, {principal.user_id}).get(principal.user_id, "unknown")
        return CaseNoteOut(
            id=note.id,
            author=UserRef(id=principal.user_id, email=email),
            body=note.body,
            evidence=note.evidence,
            created_at=note.created_at,
        )

    return _mutate(session, run)  # type: ignore[no-any-return]
