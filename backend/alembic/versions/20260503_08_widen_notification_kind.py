"""Widen notifications.kind column to accommodate longer NotificationKind values added in T18.

Original column was varchar(13) sized to the original four values
(thread_reply, share_granted, report_ready, system). T18 added values up
to 27 chars (e.g. clinician_replied_in_thread), causing StringDataRightTruncationError.

Revision ID: 20260503_08
Revises: 20260503_07
Create Date: 2026-05-03 00:00:00

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260503_08"
down_revision = "20260503_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.String(13),
            type_=sa.String(64),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=sa.String(64),
            type_=sa.String(13),
            existing_nullable=False,
        )
