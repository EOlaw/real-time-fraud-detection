"""Monitoring and promotion CLI.

    python -m fraud_api.monitoring.cli build-reference --artifact ml/artifacts/current \\
        --policy ml/configs/decision_policy.yaml
    python -m fraud_api.monitoring.cli run --start 2026-10-27T00:00:00Z \\
        --end 2026-10-28T00:00:00Z --all-orgs [--every 3600]
    python -m fraud_api.monitoring.cli list
    python -m fraud_api.monitoring.cli ack --run <id> --user admin@demo.example.com --note "..."
    python -m fraud_api.monitoring.cli gates --artifact ml/artifacts/card-fraud-xgb-1.1.0 \\
        --policy ml/configs/policies/card-fraud-xgb-1.1.0.yaml --start ... --end ...
    python -m fraud_api.monitoring.cli promote ... --apply --operator NAME
    python -m fraud_api.monitoring.cli rollback --artifact ml/artifacts/card-fraud-xgb-1.0.0 \\
        --policy ml/configs/policies/card-fraud-xgb-1.0.0.yaml --operator NAME

``run`` and ``gates`` only read assessments and labels; they write monitoring tables and audit
records. ``promote --apply`` and ``rollback`` are the only commands that change serving state
(the ``current`` artifact link and the active policy file); both are audited and take effect when
API instances and workers restart. Output is aggregate only.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from fraud_api.core.config import get_settings
from fraud_api.core.logging import configure_logging, get_logger
from fraud_api.db.importer import load_canonical
from fraud_api.db.models import AuditLog, ModelVersion, MonitoringRun, Organization, User
from fraud_api.db.session import make_engine, make_session_factory
from fraud_api.insights.analytics import SOURCES, Window
from fraud_api.monitoring.gates import check_all
from fraud_api.monitoring.job import acknowledge, run_monitoring
from fraud_api.monitoring.reference import MonitoringError, build_profile, store_reference
from fraud_api.promotion import Gate, apply_promotion, check_promotion
from fraud_api.scoring.policy import DecisionPolicy
from fraud_ml.data.ingest import MANIFEST_FILE
from fraud_ml.models.artifact import load_artifact

log = get_logger(__name__)
DATA = Path("ml/data/processed/card_transactions.parquet")


def _ts(value: str) -> datetime:
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamps need a timezone offset")
    return t.astimezone(UTC)


def _window(args: argparse.Namespace) -> Window:
    if args.end <= args.start:
        sys.exit("--end must be after --start")
    return Window(args.start, args.end, "processed", tuple(args.source or SOURCES))


def _print_gates(gates: list[Gate]) -> bool:
    for g in gates:
        print(f"[{'PASS' if g.passed else 'FAIL'}] {g.name}: {g.detail}")
    return bool(gates) and all(g.passed for g in gates)


def _model_version(session: Any, spec: str | None) -> uuid.UUID | None:
    if spec is None:
        return None
    name, _, version = spec.partition("@")
    mv = session.scalar(
        select(ModelVersion.id).where(ModelVersion.name == name, ModelVersion.version == version)
    )
    if mv is None:
        sys.exit(f"model version {spec} is not registered")
    return mv  # type: ignore[no-any-return]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="fraud_api.monitoring.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build-reference", help="reference profile for a model version")
    b.add_argument("--artifact", type=Path, default=Path("ml/artifacts/current"))
    b.add_argument("--policy", type=Path, default=Path("ml/configs/decision_policy.yaml"))
    b.add_argument("--data", type=Path, default=DATA)

    r = sub.add_parser("run", help="compute and persist monitoring (idempotent)")
    r.add_argument("--start", type=_ts, help="window start (scored_at), inclusive")
    r.add_argument("--end", type=_ts, help="window end, exclusive")
    r.add_argument(
        "--last-hours",
        type=int,
        help="instead of --start/--end: the last N whole hours (recomputed on each --every run)",
    )
    scope = r.add_mutually_exclusive_group(required=True)
    scope.add_argument("--org", help="organization slug")
    scope.add_argument("--global", dest="global_", action="store_true", help="all tenants")
    scope.add_argument("--all-orgs", action="store_true", help="global plus each organization")
    r.add_argument("--model-version", help="name@version (default: active)")
    r.add_argument("--labels-as-of", type=_ts, help="default: now")
    r.add_argument("--maturity-days", type=int)
    r.add_argument("--label-cohort-start", type=_ts, help="event-time cohort for labels")
    r.add_argument("--label-cohort-end", type=_ts)
    r.add_argument("--include-probable", action="store_true")
    r.add_argument("--source", action="append", choices=list(SOURCES))
    r.add_argument("--every", type=float, default=0, help="repeat every N seconds (0 = once)")

    sub.add_parser("list", help="recent runs").add_argument("--limit", type=int, default=20)

    a = sub.add_parser("ack", help="acknowledge a warn/alert/failed run (audited)")
    a.add_argument("--run", type=uuid.UUID, required=True)
    a.add_argument("--user", required=True, help="email of an admin")
    a.add_argument("--note", required=True)

    for name in ("gates", "promote"):
        g = sub.add_parser(
            name, help="promotion gates" + (" and apply" if name == "promote" else "")
        )
        g.add_argument("--artifact", type=Path, required=True)
        g.add_argument("--policy", type=Path, required=True)
        g.add_argument("--data", type=Path, default=DATA)
        g.add_argument("--incumbent", type=Path, default=Path("ml/artifacts/current"))
        g.add_argument("--start", type=_ts, required=True, help="evidence window (scored_at)")
        g.add_argument("--end", type=_ts, required=True)
        g.add_argument("--labels-as-of", type=_ts)
        g.add_argument("--maturity-days", type=int, default=30)
        g.add_argument("--include-probable", action="store_true")
        g.add_argument("--parity-sample", type=int, default=500)
        g.add_argument("--source", action="append", choices=list(SOURCES))
        if name == "promote":
            g.add_argument("--apply", action="store_true")
            g.add_argument("--operator", required=True, help="who is promoting (audited)")
            g.add_argument("--current-link", type=Path, default=Path("ml/artifacts/current"))
            g.add_argument(
                "--active-policy", type=Path, default=Path("ml/configs/decision_policy.yaml")
            )
            g.add_argument("--archive-dir", type=Path, default=Path("ml/configs/policies"))

    rb = sub.add_parser("rollback", help="repoint serving to a previous artifact (audited)")
    rb.add_argument("--artifact", type=Path, required=True)
    rb.add_argument("--policy", type=Path, required=True)
    rb.add_argument("--data", type=Path, default=DATA)
    rb.add_argument("--operator", required=True)
    rb.add_argument("--current-link", type=Path, default=Path("ml/artifacts/current"))
    rb.add_argument("--active-policy", type=Path, default=Path("ml/configs/decision_policy.yaml"))

    args = p.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    factory = make_session_factory(make_engine(settings))

    with factory() as session:
        if args.cmd == "build-reference":
            model = load_artifact(args.artifact)
            policy = DecisionPolicy.load(args.policy)
            events, _ = load_canonical(args.data)
            dataset = json.loads((args.data.parent / MANIFEST_FILE).read_text())
            try:
                profile, start, end = build_profile(model, policy, events)
                ref, created = store_reference(
                    session, model, policy, dataset, profile, (start, end)
                )
            except MonitoringError as exc:
                sys.exit(f"reference not built: {exc}")
            print(
                json.dumps(
                    {
                        "reference_id": str(ref.id),
                        "created": created,
                        "model": f"{model.name}@{model.version}",
                        "window": [start.isoformat(), end.isoformat()],
                        "sample_size": ref.sample_size,
                    },
                    indent=2,
                )
            )

        elif args.cmd == "run":
            stop = threading.Event()

            if args.last_hours is None and (args.start is None or args.end is None):
                sys.exit("give --start and --end, or --last-hours")

            def once() -> int:
                if args.last_hours is not None:
                    # whole UTC hours, so repeated runs inside the same hour are idempotent
                    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
                    args.start, args.end = end - timedelta(hours=args.last_hours), end
                orgs: list[uuid.UUID | None] = []
                if args.org:
                    org = session.scalar(select(Organization).where(Organization.slug == args.org))
                    if org is None:
                        sys.exit(f"organization {args.org!r} not found")
                    orgs = [org.id]
                elif args.global_:
                    orgs = [None]
                else:
                    orgs = [
                        None,
                        *session.scalars(select(Organization.id).order_by(Organization.slug)),
                    ]
                cohort = None
                if args.label_cohort_start and args.label_cohort_end:
                    cohort = Window(
                        args.label_cohort_start,
                        args.label_cohort_end,
                        "event",
                        tuple(args.source or SOURCES),
                    )
                worst = 0
                for org_id in orgs:
                    try:
                        run = run_monitoring(
                            session,
                            organization_id=org_id,
                            model_version_id=_model_version(session, args.model_version),
                            window=_window(args),
                            labels_as_of=args.labels_as_of or datetime.now(UTC),
                            maturity_days=settings.label_maturity_days
                            if args.maturity_days is None
                            else args.maturity_days,
                            label_cohort=cohort,
                            include_probable=args.include_probable,
                        )
                    except MonitoringError as exc:
                        print(f"monitoring failed: {exc}", file=sys.stderr)
                        return 2
                    print(
                        json.dumps(
                            {
                                "run_id": str(run.id),
                                "scope": run.scope,
                                "status": run.status,
                                "recommendation": run.recommendation,
                                "assessments": run.summary.get("assessments"),
                                "alerts": run.summary.get("alerts"),
                                "warnings": run.summary.get("warnings"),
                            }
                        )
                    )
                    worst = max(worst, {"alert": 1, "failed": 2}.get(run.status, 0))
                return worst

            if args.every <= 0:
                sys.exit(once())
            while not stop.wait(0):
                code = once()
                log.info("monitoring_scheduled_run_done", exit_code=code)
                if stop.wait(args.every):
                    break

        elif args.cmd == "list":
            runs = session.scalars(
                select(MonitoringRun).order_by(MonitoringRun.finished_at.desc()).limit(args.limit)
            )
            for run in runs:
                print(
                    f"{run.id} {run.scope:12s} {run.window_start:%Y-%m-%dT%H:%MZ}.."
                    f"{run.window_end:%Y-%m-%dT%H:%MZ} {run.status:17s} {run.recommendation:17s}"
                    f" ack={'yes' if run.acknowledged_at else 'no'}"
                )

        elif args.cmd == "ack":
            user = session.scalar(select(User).where(User.email == args.user.lower()))
            if user is None or user.role != "admin":
                sys.exit("acknowledgement needs an existing admin user")
            try:
                run = acknowledge(session, args.run, user.id, args.note)
            except MonitoringError as exc:
                sys.exit(str(exc))
            print(f"acknowledged {run.id} ({run.status})")

        elif args.cmd in ("gates", "promote"):
            gates = check_all(
                session,
                args.artifact,
                args.policy,
                args.data,
                args.incumbent,
                _window(args),
                args.labels_as_of,
                args.maturity_days,
                args.include_probable,
                args.parity_sample,
            )
            passed = _print_gates(gates)
            if args.cmd == "gates" or not args.apply:
                print(
                    "all gates passed; nothing changed"
                    if passed
                    else "promotion blocked; nothing changed"
                )
                sys.exit(0 if passed else 1)
            if not passed:
                print("promotion blocked; nothing changed", file=sys.stderr)
                sys.exit(1)
            candidate = load_artifact(args.artifact)
            previous = load_artifact(args.current_link)
            message = apply_promotion(
                args.artifact, args.policy, args.current_link, args.active_policy, args.archive_dir
            )
            session.add(
                AuditLog(
                    action="model.promote",
                    resource_type="model_version",
                    resource_id=f"{candidate.name}@{candidate.version}",
                    details={
                        "operator": args.operator[:64],
                        "from": f"{previous.name}@{previous.version}",
                        "to": f"{candidate.name}@{candidate.version}",
                        "gates": {g.name: g.passed for g in gates},
                    },
                )
            )
            session.commit()
            print(message)
            print("restart API instances and stream workers to serve the promoted model")

        elif args.cmd == "rollback":
            # Rollback target must still pass the artifact and policy gates (fail closed).
            gates = check_promotion(args.artifact, args.policy, args.data, sample=50)
            if not _print_gates(gates[:3]) or len(gates) < 3:
                sys.exit("rollback target failed artifact/policy gates; nothing changed")
            target = load_artifact(args.artifact)
            previous = load_artifact(args.current_link)
            if args.artifact.resolve().parent != args.current_link.parent.resolve():
                sys.exit("target artifact must live next to the 'current' link")
            tmp = args.current_link.with_name(".current.tmp")
            tmp.unlink(missing_ok=True)
            tmp.symlink_to(args.artifact.name)
            tmp.replace(args.current_link)
            shutil.copyfile(args.policy, args.active_policy)
            session.add(
                AuditLog(
                    action="model.rollback",
                    resource_type="model_version",
                    resource_id=f"{target.name}@{target.version}",
                    details={
                        "operator": args.operator[:64],
                        "from": f"{previous.name}@{previous.version}",
                        "to": f"{target.name}@{target.version}",
                    },
                )
            )
            session.commit()
            print(
                f"serving link now points to {args.artifact.name}; policy restored. "
                "Restart API instances and workers."
            )


if __name__ == "__main__":
    main()
