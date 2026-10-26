"""Administrative commands.

    uv run python -m fraud_api.cli create-org --slug demo --name "Demo Bank (sandbox)"
    FRAUD_NEW_USER_PASSWORD=... uv run python -m fraud_api.cli create-user \
        --org demo --email svc@demo.example.com --role service
    uv run python -m fraud_api.cli load-history --org demo \
        --data ml/data/processed/card_transactions.parquet \
        --start 2020-05-22T12:14:00Z --end 2020-06-21T12:14:00Z

``load-history`` inserts historical events (source='backfill', unscored) so that online features
for replayed events have the same 30-day lookback the offline features had.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import insert, select

from fraud_api.core.config import get_settings
from fraud_api.core.security import Role, hash_password
from fraud_api.db.models import AuditLog, Organization, Transaction, User
from fraud_api.db.session import make_engine, make_session_factory

BATCH = 5_000


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
    p = sub.add_parser("load-history")
    p.add_argument("--org", required=True)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--start", type=datetime.fromisoformat, required=True)
    p.add_argument("--end", type=datetime.fromisoformat, required=True)
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
        elif args.cmd == "load-history":
            org = _org(session, args.org)
            df = pd.read_parquet(args.data)
            df = df[(df["event_time"] >= args.start) & (df["event_time"] < args.end)]
            rows = [
                {
                    "organization_id": org.id,
                    "external_id": r.event_id,
                    "customer_id": r.customer_id,
                    "merchant_id": r.merchant_id,
                    "amount": Decimal(str(r.amount)).quantize(Decimal("0.01")),
                    "currency": r.currency,
                    "category": r.category,
                    "channel": r.channel,
                    "region": r.customer_region,
                    "event_time": r.event_time.to_pydatetime(),
                    "source": "backfill",
                    "schema_version": "1.0",
                    "payload_sha256": "backfill",
                }
                for r in df.itertuples(index=False)
            ]
            for i in range(0, len(rows), BATCH):
                session.execute(insert(Transaction), rows[i : i + BATCH])
            session.add(
                AuditLog(
                    organization_id=org.id,
                    action="transactions.backfill",
                    resource_type="transaction",
                    details={
                        "rows": len(rows),
                        "start": args.start.isoformat(),
                        "end": args.end.isoformat(),
                    },
                )
            )
            session.commit()
            print(f"backfilled {len(rows)} transactions")


if __name__ == "__main__":
    main()
