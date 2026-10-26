"""Administrative commands.

    uv run python -m fraud_api.cli create-org --slug demo --name "Demo Bank (sandbox)"
    FRAUD_NEW_USER_PASSWORD=... uv run python -m fraud_api.cli create-user \
        --org demo --email svc@demo.example.com --role service
    uv run python -m fraud_api.cli import-transactions --org demo \
        --data ml/data/processed/card_transactions.parquet --source backfill \
        [--start 2020-05-22T12:14:00+00:00] [--end 2020-06-21T12:14:00+00:00]
    uv run python -m fraud_api.cli load-history --org demo --data ... --start ... --end ...

``import-transactions`` loads canonical Parquet into ``transactions`` for one organization, as
unscored events labeled with ``--source``. It is idempotent (re-running reports rows as
``already_present``), rejects ids that exist with different content, and is all-or-nothing (see
``fraud_api.db.importer``). ``load-history`` is the Phase 1 command, now an alias for
``import-transactions --source backfill`` with a required window: it gives replayed events the
same 30-day lookback the offline features had.

Exit status: 0 on success, 1 when the import is refused or rolled back (nothing written).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from fraud_api.core.config import get_settings
from fraud_api.core.security import Role, hash_password
from fraud_api.db.importer import (
    IMPORTABLE_SOURCES,
    TransactionImportError,
    import_transactions,
    load_canonical,
)
from fraud_api.db.models import AuditLog, Organization, User
from fraud_api.db.session import make_engine, make_session_factory


def _org(session, slug: str) -> Organization:  # type: ignore[no-untyped-def]
    org = session.scalar(select(Organization).where(Organization.slug == slug))
    if org is None:
        sys.exit(f"organization {slug!r} not found")
    return org  # type: ignore[no-any-return]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fraud_api.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-org")
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p = sub.add_parser("create-user")
    p.add_argument("--org", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--role", required=True, choices=[r.value for r in Role])
    p = sub.add_parser("import-transactions", help="import canonical Parquet (idempotent)")
    p.add_argument("--org", required=True)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--source", required=True, choices=IMPORTABLE_SOURCES)
    p.add_argument("--start", type=datetime.fromisoformat, help="inclusive, timezone required")
    p.add_argument("--end", type=datetime.fromisoformat, help="exclusive, timezone required")
    p.add_argument("--statement-timeout-s", type=int, default=600)
    p = sub.add_parser("load-history", help="alias: import-transactions --source backfill")
    p.add_argument("--org", required=True)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--start", type=datetime.fromisoformat, required=True)
    p.add_argument("--end", type=datetime.fromisoformat, required=True)
    p.add_argument("--statement-timeout-s", type=int, default=600)
    p.set_defaults(source="backfill")
    args = parser.parse_args(argv)

    factory = make_session_factory(make_engine(get_settings()))
    with factory() as session:
        if args.cmd == "create-org":
            org = Organization(slug=args.slug, name=args.name)
            session.add(org)
            session.commit()
            print(f"created organization {org.slug} ({org.id})")
        elif args.cmd == "create-user":
            org = _org(session, args.org)
            password = os.environ.get("FRAUD_NEW_USER_PASSWORD") or getpass.getpass("Password: ")
            user = User(
                organization_id=org.id,
                email=args.email.lower(),
                password_hash=hash_password(password),
                role=args.role,
            )
            session.add(user)
            session.flush()
            session.add(
                AuditLog(
                    organization_id=org.id,
                    action="user.create",
                    resource_type="user",
                    resource_id=str(user.id),
                    details={"role": args.role},
                )
            )
            session.commit()
            print(f"created {args.role} user {user.email} ({user.id})")
        elif args.cmd in ("import-transactions", "load-history"):
            try:
                df, manifest = load_canonical(args.data, args.start, args.end)
                result = import_transactions(
                    session,
                    args.org,
                    df,
                    manifest,
                    args.source,
                    window=(args.start, args.end),
                    statement_timeout_ms=args.statement_timeout_s * 1000,
                )
            except TransactionImportError as exc:
                print(f"import failed: {exc}", file=sys.stderr)
                sys.exit(1)
            except SQLAlchemyError as exc:
                # First line only: PostgreSQL CONTEXT lines can echo row contents.
                first = str(getattr(exc, "orig", exc)).splitlines()[0]
                print(
                    f"import failed and was rolled back: {type(exc).__name__}: {first}",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    main()
