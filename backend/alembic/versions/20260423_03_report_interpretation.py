"""Add interpretation_json to reports table.

Revision ID: 20260423_03
Revises: 20260418_02
Create Date: 2026-04-23 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260423_03"
down_revision = "20260418_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reports") as batch_op:
        batch_op.add_column(sa.Column("interpretation_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reports") as batch_op:
        batch_op.drop_column("interpretation_json")
