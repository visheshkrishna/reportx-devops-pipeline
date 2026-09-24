"""Integration tests for background cleanup of expired consent shares."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import AuditEvent, ConsentAccessLevel, ConsentScope, ConsentShare
from app.services.reports import cleanup_expired_shares
from tests.support.consent_api import ConsentApiHarness, seed_report, seed_user


def _run_cleanup(consent_api: ConsentApiHarness) -> int:
    async def _execute() -> int:
        async with consent_api.client.app.state.database.session_factory() as async_session:
            return await cleanup_expired_shares(async_session)

    return asyncio.run(_execute())


def test_cleanup_revokes_only_active_expired_shares(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        patient = seed_user(session, email="patient-cleanup-count@example.com", role="patient")
        clinician = seed_user(
            session, email="clinician-cleanup-count@example.com", role="clinician"
        )
        clinician_alt = seed_user(
            session, email="clinician-cleanup-count-alt@example.com", role="clinician"
        )
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

        expired_share = ConsentShare(
            subject_user_id=patient.id,
            grantee_user_id=clinician.id,
            granted_by_user_id=patient.id,
            report_id=report.id,
            scope=ConsentScope.REPORT,
            access_level=ConsentAccessLevel.READ,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        active_share = ConsentShare(
            subject_user_id=patient.id,
            grantee_user_id=clinician.id,
            granted_by_user_id=patient.id,
            report_id=None,
            scope=ConsentScope.PATIENT,
            access_level=ConsentAccessLevel.READ,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        already_revoked_share = ConsentShare(
            subject_user_id=patient.id,
            grantee_user_id=clinician_alt.id,
            granted_by_user_id=patient.id,
            report_id=report.id,
            scope=ConsentScope.REPORT,
            access_level=ConsentAccessLevel.READ,
            expires_at=datetime.now(UTC) - timedelta(hours=2),
            revoked_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        session.add_all([expired_share, active_share, already_revoked_share])
        session.commit()

        expired_share_id = expired_share.id
        active_share_id = active_share.id
        already_revoked_share_id = already_revoked_share.id

    cleaned_count = _run_cleanup(consent_api)
    assert cleaned_count == 1

    with consent_api.session_factory() as session:
        expired_row = session.scalar(
            select(ConsentShare).where(ConsentShare.id == expired_share_id)
        )
        active_row = session.scalar(select(ConsentShare).where(ConsentShare.id == active_share_id))
        already_revoked_row = session.scalar(
            select(ConsentShare).where(ConsentShare.id == already_revoked_share_id)
        )

        assert expired_row is not None
        assert active_row is not None
        assert already_revoked_row is not None

        assert expired_row.revoked_at is not None
        assert active_row.revoked_at is None
        assert already_revoked_row.revoked_at is not None


def test_cleanup_writes_consent_share_expired_audit_event(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        patient = seed_user(session, email="patient-cleanup-audit@example.com", role="patient")
        clinician = seed_user(
            session, email="clinician-cleanup-audit@example.com", role="clinician"
        )
        clinician_id = clinician.id
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )
        expired_share = ConsentShare(
            subject_user_id=patient.id,
            grantee_user_id=clinician.id,
            granted_by_user_id=patient.id,
            report_id=report.id,
            scope=ConsentScope.REPORT,
            access_level=ConsentAccessLevel.COMMENT,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(expired_share)
        session.commit()
        expired_share_id = expired_share.id

    cleaned_count = _run_cleanup(consent_api)
    assert cleaned_count == 1

    with consent_api.session_factory() as session:
        event = session.scalar(
            select(AuditEvent)
            .where(AuditEvent.resource_type == "consent_share")
            .where(AuditEvent.resource_id == expired_share_id)
            .where(AuditEvent.action == "expired")
        )

    assert event is not None
    assert event.resource_id == expired_share_id
    assert event.resource_type == "consent_share"
    assert event.subject_user_id == clinician_id

    context = event.context or {}
    assert context.get("scope") == "report"
    assert context.get("access_level") == "comment"
    assert context.get("report_id") is not None
    assert context.get("grantee_email") == "clinician-cleanup-audit@example.com"
    assert "expired_at" in context


def test_cleanup_handles_multiple_expired_shares_in_one_run(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        patient = seed_user(session, email="patient-cleanup-multi@example.com", role="patient")
        clinician_a = seed_user(session, email="clinician-cleanup-a@example.com", role="clinician")
        clinician_b = seed_user(session, email="clinician-cleanup-b@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

        share_a = ConsentShare(
            subject_user_id=patient.id,
            grantee_user_id=clinician_a.id,
            granted_by_user_id=patient.id,
            report_id=report.id,
            scope=ConsentScope.REPORT,
            access_level=ConsentAccessLevel.READ,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        share_b = ConsentShare(
            subject_user_id=patient.id,
            grantee_user_id=clinician_b.id,
            granted_by_user_id=patient.id,
            report_id=report.id,
            scope=ConsentScope.REPORT,
            access_level=ConsentAccessLevel.READ,
            expires_at=datetime.now(UTC) - timedelta(hours=2),
        )
        session.add_all([share_a, share_b])
        session.commit()
        share_ids = [share_a.id, share_b.id]

    cleaned_count = _run_cleanup(consent_api)
    assert cleaned_count == 2

    with consent_api.session_factory() as session:
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.resource_type == "consent_share")
            .where(AuditEvent.action == "expired")
            .where(AuditEvent.resource_id.in_(share_ids))
        ).all()

    assert len(events) == 2
