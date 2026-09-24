"""Add chat_history_json to reports table.

Revision ID: 20260503_05
Revises: 20260429_04
Create Date: 2026-05-03 00:00:00
"""

from __future__ import annotations

revision = "20260503_05"
down_revision = "20260429_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # chat_history_json was already added by 20260427_04 on the T17/T21 branch.
    # This revision exists solely as a chain pointer for the merge migration.
    pass


def downgrade() -> None:
    pass
