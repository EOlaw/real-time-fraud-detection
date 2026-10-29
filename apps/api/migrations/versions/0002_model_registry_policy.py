"""model registry: bound policy and activation time

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26 17:24:08.055317
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_versions",
        sa.Column("bound_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "model_versions", sa.Column("last_activated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("model_versions", "last_activated_at")
    op.drop_column("model_versions", "bound_policy")
