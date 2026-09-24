"""Add finding anchor to conversation_threads for T7.

Revision ID: 20260418_02
Revises: 20260326_01
Create Date: 2026-04-18 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260418_02"
down_revision = "20260326_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversation_threads") as batch_op:
        batch_op.add_column(sa.Column("finding_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_conversation_threads_finding_id_report_findings",
            "report_findings",
            ["finding_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("conversation_threads") as batch_op:
        batch_op.drop_constraint(
            "fk_conversation_threads_finding_id_report_findings",
            type_="foreignkey",
        )
        batch_op.drop_column("finding_id")
