"""rules engine: immutable rule versions, atomic activations, per-assessment evaluations

Existing assessments predate rules: ``policy_decision`` is backfilled from ``decision`` and
``rules_status`` defaults to ``none``, so their stored decisions are unchanged.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27 03:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORDER = "ARRAY['allow','challenge','review','decline']::varchar[]"
_DEFINITION_FK = [
    "rule_definitions.id",
    "rule_definitions.organization_id",
    "rule_definitions.rule_key",
]


def upgrade() -> None:
    op.create_table(
        "rule_definitions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("rule_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("condition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("condition_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('flag','challenge','review','decline')", name="ck_rule_definitions_action"
        ),
        sa.CheckConstraint("priority BETWEEN 0 AND 1000", name="ck_rule_definitions_priority"),
        sa.CheckConstraint("version > 0", name="ck_rule_definitions_version_positive"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "organization_id", "rule_key", name="uq_rule_definitions_id_org_key"
        ),
        sa.UniqueConstraint(
            "organization_id", "rule_key", "version", name="uq_rule_definitions_org_key_version"
        ),
    )
    op.create_table(
        "rule_activations",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("rule_key", sa.String(length=64), nullable=False),
        sa.Column("rule_definition_id", sa.UUID(), nullable=False),
        sa.Column("activated_by", sa.UUID(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["activated_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["rule_definition_id", "organization_id", "rule_key"],
            _DEFINITION_FK,
            name="fk_rule_activations_definition",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("organization_id", "rule_key"),
    )
    op.create_table(
        "rule_evaluations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("risk_assessment_id", sa.UUID(), nullable=False),
        sa.Column("rule_definition_id", sa.UUID(), nullable=False),
        sa.Column("rule_key", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("influenced_decision", sa.Boolean(), nullable=False),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "NOT influenced_decision OR outcome IN ('matched','error')",
            name="ck_rule_evaluations_influence_needs_match",
        ),
        sa.CheckConstraint(
            "outcome IN ('matched','not_matched','unknown','error')",
            name="ck_rule_evaluations_outcome",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["risk_assessment_id"], ["risk_assessments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["rule_definition_id", "organization_id", "rule_key"],
            _DEFINITION_FK,
            name="fk_rule_evaluations_definition",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "risk_assessment_id", "rule_definition_id", name="uq_rule_evaluations_assessment_rule"
        ),
    )
    op.create_index(
        "ix_rule_evaluations_definition", "rule_evaluations", ["rule_definition_id"], unique=False
    )

    op.add_column(
        "risk_assessments", sa.Column("policy_decision", sa.String(length=16), nullable=True)
    )
    op.execute("UPDATE risk_assessments SET policy_decision = decision")
    op.alter_column("risk_assessments", "policy_decision", nullable=False)
    op.add_column(
        "risk_assessments",
        sa.Column("rules_status", sa.String(length=16), server_default="none", nullable=False),
    )
    for name, sql in (
        (
            "ck_risk_assessments_policy_decision",
            "policy_decision IN ('allow','challenge','review','decline')",
        ),
        ("ck_risk_assessments_rules_status", "rules_status IN ('none','evaluated','error')"),
        (
            "ck_risk_assessments_rules_never_relax",
            f"array_position({_ORDER}, decision) >= array_position({_ORDER}, policy_decision)",
        ),
        (
            "ck_risk_assessments_no_rules_no_change",
            "rules_status <> 'none' OR decision = policy_decision",
        ),
        (
            "ck_risk_assessments_rule_error_not_lenient",
            "rules_status <> 'error' OR decision IN ('review','decline')",
        ),
    ):
        op.create_check_constraint(name, "risk_assessments", sql)

    # Rule versions and evaluations are append-only, like audit_logs.
    op.execute("""
        CREATE FUNCTION rules_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END $$;
        CREATE TRIGGER rule_definitions_no_update_delete BEFORE UPDATE OR DELETE
            ON rule_definitions FOR EACH ROW EXECUTE FUNCTION rules_append_only();
        CREATE TRIGGER rule_evaluations_no_update_delete BEFORE UPDATE OR DELETE
            ON rule_evaluations FOR EACH ROW EXECUTE FUNCTION rules_append_only();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS rule_evaluations_no_update_delete ON rule_evaluations")
    op.execute("DROP TRIGGER IF EXISTS rule_definitions_no_update_delete ON rule_definitions")
    op.execute("DROP FUNCTION IF EXISTS rules_append_only()")
    for name in (
        "ck_risk_assessments_rule_error_not_lenient",
        "ck_risk_assessments_no_rules_no_change",
        "ck_risk_assessments_rules_never_relax",
        "ck_risk_assessments_rules_status",
        "ck_risk_assessments_policy_decision",
    ):
        op.drop_constraint(name, "risk_assessments", type_="check")
    op.drop_column("risk_assessments", "rules_status")
    op.drop_column("risk_assessments", "policy_decision")
    op.drop_index("ix_rule_evaluations_definition", table_name="rule_evaluations")
    op.drop_table("rule_evaluations")
    op.drop_table("rule_activations")
    op.drop_table("rule_definitions")
