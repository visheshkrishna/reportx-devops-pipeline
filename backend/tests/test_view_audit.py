from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import AuditEvent
from tests.support.consent_api import (
    ConsentApiHarness,
    auth_headers,
    login,
    seed_report,
    seed_user,
)


def _future_expiry_iso(days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def test_non_owner_report_access_creates_view_audit_event(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        patient = seed_user(session, email="patient-view@example.com", role="patient")
        clinician = seed_user(session, email="clinician-view@example.com", role="clinician")
        report = seed_report(session, subject_email=patient.email, created_by_email=patient.email)

    patient_token = login(consent_api, email="patient-view@example.com")
    share = consent_api.client.post(
        f"/api/v1/reports/{report.id}/share",
        headers=auth_headers(patient_token),
        json={
            "clinician_email": "clinician-view@example.com",
            "scope": "report",
            "access_level": "read",
            "expires_at": _future_expiry_iso(),
        },
    )
    assert share.status_code == 201, share.text
    share_id = share.json()["id"]

    clinician_token = login(consent_api, email="clinician-view@example.com")
    read = consent_api.client.get(
        f"/api/v1/reports/{report.id}",
        headers=auth_headers(clinician_token),
    )
    assert read.status_code == 200, read.text

    with consent_api.session_factory() as session:
        rows = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.resource_type == "consent_share")
            .where(AuditEvent.resource_id == share_id)
            .where(AuditEvent.action == "view")
        ).all()

    assert len(rows) == 1
    event = rows[0]
    assert event.actor_user_id == clinician.id
    assert event.subject_user_id == clinician.id


def test_owner_report_access_does_not_create_view_audit_event(
    consent_api: ConsentApiHarness,
) -> None:
    with consent_api.session_factory() as session:
        patient = seed_user(session, email="patient-owner-view@example.com", role="patient")
        report = seed_report(session, subject_email=patient.email, created_by_email=patient.email)

    patient_token = login(consent_api, email="patient-owner-view@example.com")
    read = consent_api.client.get(
        f"/api/v1/reports/{report.id}",
        headers=auth_headers(patient_token),
    )
    assert read.status_code == 200, read.text

    with consent_api.session_factory() as session:
        rows = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.resource_type == "consent_share")
            .where(AuditEvent.action == "view")
        ).all()

    assert all((row.context or {}).get("report_id") != report.id for row in rows)
