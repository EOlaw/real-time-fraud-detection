"""Integration fixtures: a real PostgreSQL database (``fraud_test``) migrated with Alembic.

Start it with ``docker compose up -d postgres``. Tests are skipped (and reported as skipped) if
the database is unreachable.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from fraud_api.core.config import FailureMode, Settings
from fraud_api.core.security import Role, hash_password
from fraud_api.db.models import Organization, User
from fraud_api.main import create_app

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "integration-test-password"


def _resolve_url() -> str | None:
    url = os.environ.get("FRAUD_TEST_DATABASE_URL")
    if url:
        return url
    base = os.environ.get("FRAUD_DATABASE_URL")
    if not base and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("FRAUD_DATABASE_URL="):
                base = line.split("=", 1)[1].strip()
    if not base:
        return None
    return make_url(base).set(database="fraud_test").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def db_url() -> str:
    url = _resolve_url()
    if url is None:
        pytest.skip("No database configured (set FRAUD_TEST_DATABASE_URL)")
    try:
        with create_engine(url).connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL not reachable: {type(exc).__name__}")
    return url


@pytest.fixture(scope="session")
def engine(db_url: str) -> Iterator[Engine]:
    cfg = Config(str(ROOT / "apps/api/alembic.ini"))
    os.environ["FRAUD_DATABASE_URL"] = db_url  # read by migrations/env.py
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = create_engine(db_url)
    yield eng
    eng.dispose()
    command.downgrade(cfg, "base")


@pytest.fixture(autouse=True)
def clean_tables(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE risk_assessments, transactions, audit_logs, users, organizations, "
                "model_versions CASCADE"
            )
        )


def make_settings(db_url: str, model_dir: Path, policy_path: Path, **overrides: Any) -> Settings:
    return Settings(
        database_url=db_url,  # type: ignore[arg-type]
        jwt_secret=secrets.token_urlsafe(48),  # type: ignore[arg-type]
        model_dir=model_dir,
        policy_path=policy_path,
        failure_mode=overrides.pop("failure_mode", FailureMode.MANUAL_REVIEW),
        # Settings also read .env; never let a developer's FRAUD_REDIS_URL leak into tests.
        # Streaming tests opt in explicitly (Redis DB 15, unique key prefix).
        redis_url=overrides.pop("redis_url", None),
        **overrides,
    )


@dataclass
class Tenant:
    org_id: uuid.UUID
    tokens: dict[Role, str]

    def headers(self, role: Role = Role.SERVICE) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[role]}"}


def create_tenant(engine: Engine, client: TestClient, slug: str) -> Tenant:
    with Session(engine) as s:
        org = Organization(slug=slug, name=slug)
        s.add(org)
        s.flush()
        for role in Role:
            s.add(
                User(
                    organization_id=org.id,
                    email=f"{role.value}@{slug}.example.com",
                    password_hash=hash_password(PASSWORD),
                    role=role.value,
                )
            )
        s.commit()
        org_id = org.id
    tokens = {}
    for role in Role:
        r = client.post(
            "/api/v1/auth/token",
            json={"email": f"{role.value}@{slug}.example.com", "password": PASSWORD},
        )
        assert r.status_code == 200, r.text
        tokens[role] = r.json()["access_token"]
    return Tenant(org_id, tokens)


AppFactory = Callable[..., TestClient]


@pytest.fixture
def app_factory(
    db_url: str, engine: Engine, trained_artifact: Path, policy_path: Path
) -> Iterator[AppFactory]:
    clients: list[TestClient] = []

    def factory(model_dir: Path | None = None, **overrides: Any) -> TestClient:
        settings = make_settings(
            db_url,
            model_dir or trained_artifact,
            overrides.pop("policy_path", policy_path),
            **overrides,
        )
        client = TestClient(create_app(settings))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(app_factory: AppFactory) -> TestClient:
    return app_factory()


@pytest.fixture
def tenant(engine: Engine, client: TestClient) -> Tenant:
    return create_tenant(engine, client, "acme")
