"""persistence foundation

Revision ID: 20260326_01
Revises:
Create Date: 2026-03-26 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260326_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )

    op.create_table(
        "users",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("preferred_language", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "audit_events",
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("subject_user_id", sa.String(length=36), nullable=True),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=255), nullable=False),
        sa.Column("session_family", sa.String(length=36), nullable=False),
        sa.Column("device_label", sa.String(length=120), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("ip_address_hash", sa.String(length=255), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_auth_sessions"),
        sa.UniqueConstraint("refresh_token_hash", name="uq_auth_sessions_refresh_token_hash"),
    )

    op.create_table(
        "clinician_response_templates",
        sa.Column("author_user_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_clinician_response_templates"),
        sa.UniqueConstraint("slug", name="uq_clinician_response_templates_slug"),
    )

    op.create_table(
        "reports",
        sa.Column("subject_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column(
            "source_kind",
            sa.Enum("pdf", "text", "image", "manual", name="report_source_kind", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "sharing_mode",
            sa.Enum("private", "shared", name="report_sharing_mode", native_enum=False),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_reports"),
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role_id", sa.String(length=36), nullable=False),
        sa.Column("assigned_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assigned_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_id", name="pk_user_roles"),
        sa.UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_id_role_id"),
    )

    op.create_table(
        "consent_shares",
        sa.Column("subject_user_id", sa.String(length=36), nullable=False),
        sa.Column("grantee_user_id", sa.String(length=36), nullable=False),
        sa.Column("granted_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("report_id", sa.String(length=36), nullable=True),
        sa.Column(
            "scope",
            sa.Enum("report", "patient", name="consent_scope", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "access_level",
            sa.Enum("read", "comment", "manage", name="consent_access_level", native_enum=False),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "((scope = 'report' AND report_id IS NOT NULL) OR (scope = 'patient' AND report_id IS NULL))",
            name="ck_consent_shares_share_scope_matches_report",
        ),
        sa.CheckConstraint(
            "subject_user_id <> grantee_user_id",
            name="ck_consent_shares_share_not_self_grant",
        ),
        sa.ForeignKeyConstraint(["granted_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["grantee_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_consent_shares"),
        sa.UniqueConstraint(
            "subject_user_id",
            "grantee_user_id",
            "report_id",
            "scope",
            name="uq_consent_shares_scope_target",
        ),
    )

    op.create_table(
        "conversation_threads",
        sa.Column("subject_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("report_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum("open", "closed", name="thread_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_threads"),
    )

    op.create_table(
        "notifications",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=True),
        sa.Column("report_id", sa.String(length=36), nullable=True),
        sa.Column(
            "kind",
            sa.Enum(
                "thread_reply",
                "share_granted",
                "report_ready",
                "system",
                name="notification_kind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["conversation_threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
    )

    op.create_table(
        "report_findings",
        sa.Column("report_id", sa.String(length=36), nullable=False),
        sa.Column("biomarker_key", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("value_numeric", sa.Float(), nullable=True),
        sa.Column("value_text", sa.String(length=255), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column("comparator", sa.String(length=8), nullable=True),
        sa.Column(
            "flag",
            sa.Enum(
                "low",
                "high",
                "normal",
                "abnormal",
                "unknown",
                name="finding_flag",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("reference_low", sa.Float(), nullable=True),
        sa.Column("reference_high", sa.Float(), nullable=True),
        sa.Column("reference_range_text", sa.String(length=255), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "value_numeric IS NOT NULL OR value_text IS NOT NULL",
            name="ck_report_findings_finding_has_value",
        ),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_report_findings"),
    )

    op.create_table(
        "thread_messages",
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("author_user_id", sa.String(length=36), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=True),
        sa.Column(
            "kind",
            sa.Enum("text", "template", "system", name="message_kind", native_enum=False),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["template_id"], ["clinician_response_templates.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["thread_id"], ["conversation_threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_thread_messages"),
    )

    op.create_table(
        "thread_participants",
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["conversation_threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("thread_id", "user_id", name="pk_thread_participants"),
        sa.UniqueConstraint(
            "thread_id", "user_id", name="uq_thread_participants_thread_id_user_id"
        ),
    )

    op.create_table(
        "biomarker_observations",
        sa.Column("patient_user_id", sa.String(length=36), nullable=False),
        sa.Column("report_id", sa.String(length=36), nullable=False),
        sa.Column("finding_id", sa.String(length=36), nullable=False),
        sa.Column("biomarker_key", sa.String(length=120), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value_numeric", sa.Float(), nullable=True),
        sa.Column("value_text", sa.String(length=255), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column(
            "flag",
            sa.Enum(
                "low",
                "high",
                "normal",
                "abnormal",
                "unknown",
                name="observation_flag",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["finding_id"], ["report_findings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["patient_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_biomarker_observations"),
        sa.UniqueConstraint("finding_id", name="uq_biomarker_observations_finding_id"),
    )

    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"], unique=False)
    op.create_index("ix_reports_subject_user_id", "reports", ["subject_user_id"], unique=False)
    op.create_index(
        "ix_reports_created_by_user_id", "reports", ["created_by_user_id"], unique=False
    )
    op.create_index(
        "ix_report_findings_report_id",
        "report_findings",
        ["report_id", "biomarker_key"],
        unique=False,
    )
    op.create_index(
        "ix_biomarker_observations_patient_user_id",
        "biomarker_observations",
        ["patient_user_id", "biomarker_key", "observed_at"],
        unique=False,
    )
    op.create_index(
        "ix_consent_shares_grantee_user_id",
        "consent_shares",
        ["grantee_user_id", "scope", "report_id"],
        unique=False,
    )
    op.create_index(
        "ix_thread_messages_thread_id",
        "thread_messages",
        ["thread_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_user_id", "notifications", ["user_id", "read_at"], unique=False
    )
    op.create_index(
        "ix_audit_events_resource_id",
        "audit_events",
        ["resource_type", "resource_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_resource_id", table_name="audit_events")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_index("ix_thread_messages_thread_id", table_name="thread_messages")
    op.drop_index("ix_consent_shares_grantee_user_id", table_name="consent_shares")
    op.drop_index(
        "ix_biomarker_observations_patient_user_id",
        table_name="biomarker_observations",
    )
    op.drop_index("ix_report_findings_report_id", table_name="report_findings")
    op.drop_index("ix_reports_created_by_user_id", table_name="reports")
    op.drop_index("ix_reports_subject_user_id", table_name="reports")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")

    op.drop_table("biomarker_observations")
    op.drop_table("thread_participants")
    op.drop_table("thread_messages")
    op.drop_table("report_findings")
    op.drop_table("notifications")
    op.drop_table("conversation_threads")
    op.drop_table("consent_shares")
    op.drop_table("user_roles")
    op.drop_table("reports")
    op.drop_table("clinician_response_templates")
    op.drop_table("auth_sessions")
    op.drop_table("audit_events")
    op.drop_table("users")
    op.drop_table("roles")
