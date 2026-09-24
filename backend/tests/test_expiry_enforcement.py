"""Integration tests for expired-share access enforcement and audit emission."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import AuditEvent, ConsentShare
from tests.support.consent_api import (
    ConsentApiHarness,
    auth_headers,
    login,
    seed_report,
    seed_user,
)


def _future_expiry_iso(*, days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _create_share(
    consent_api: ConsentApiHarness,
    *,
    report_id: str,
    patient_token: str,
    clinician_email: str,
) -> str:
    response = consent_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        headers=auth_headers(patient_token),
        json={
            "clinician_email": clinician_email,
            "scope": "report",
            "access_level": "read",
            "expires_at": _future_expiry_iso(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _expire_share(consent_api: ConsentApiHarness, *, share_id: str) -> None:
    with consent_api.session_factory() as session:
        share = session.scalar(select(ConsentShare).where(ConsentShare.id == share_id))
        assert share is not None
        share.expires_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()


def _fetch_expiry_events(consent_api: ConsentApiHarness, *, share_id: str) -> list[AuditEvent]:
    with consent_api.session_factory() as session:
        return session.scalars(
            select(AuditEvent)
            .where(AuditEvent.resource_type == "consent_share")
            .where(AuditEvent.resource_id == share_id)
            .where(AuditEvent.action == "expired")
        ).all()


def test_expired_share_denies_access_with_expired_message(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-expiry-deny@example.com"
    clinician_email = "clinician-expiry-deny@example.com"

    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    share_id = _create_share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
    )
    _expire_share(consent_api, share_id=share_id)

    clinician_token = login(consent_api, email=clinician_email)
    denied = consent_api.client.get(
        f"/api/v1/reports/{report.id}",
        headers=auth_headers(clinician_token),
    )

    assert denied.status_code == 403
    assert "expired" in denied.text.lower()


def test_first_expired_access_creates_single_expired_audit_event(
    consent_api: ConsentApiHarness,
) -> None:
    patient_email = "patient-expiry-audit@example.com"
    clinician_email = "clinician-expiry-audit@example.com"

    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    share_id = _create_share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
    )
    _expire_share(consent_api, share_id=share_id)

    clinician_token = login(consent_api, email=clinician_email)
    first_denied = consent_api.client.get(
        f"/api/v1/reports/{report.id}",
        headers=auth_headers(clinician_token),
    )
    second_denied = consent_api.client.get(
        f"/api/v1/reports/{report.id}",
        headers=auth_headers(clinician_token),
    )

    assert first_denied.status_code == 403
    assert second_denied.status_code == 403

    events = _fetch_expiry_events(consent_api, share_id=share_id)
    assert len(events) == 1
    event = events[0]
    assert event.resource_type == "consent_share"
    assert event.resource_id == share_id

    context = event.context or {}
    assert context.get("scope") == "report"
    assert context.get("access_level") == "read"
    assert "expired_at" in context
    assert "expires_at" in context


def test_expired_access_emits_share_expired_notification(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-expiry-notification@example.com"
    clinician_email = "clinician-expiry-notification@example.com"

    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    share_id = _create_share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
    )
    _expire_share(consent_api, share_id=share_id)

    clinician_token = login(consent_api, email=clinician_email)
    denied = consent_api.client.get(
        f"/api/v1/reports/{report.id}",
        headers=auth_headers(clinician_token),
    )

    assert denied.status_code == 403

    notifications = consent_api.client.get(
        "/api/v1/notifications",
        headers=auth_headers(clinician_token),
    )
    assert notifications.status_code == 200, notifications.text
    assert any(item["type"] == "share_expired" for item in notifications.json()["items"])
