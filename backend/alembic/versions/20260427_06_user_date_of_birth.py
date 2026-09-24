"""Add date_of_birth to users table.

Revision ID: 20260427_06
Revises: 20260427_05
Create Date: 2026-04-27 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260427_06"
down_revision = "20260427_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("date_of_birth", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("date_of_birth")
