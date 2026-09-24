from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import ConsentShare
from app.services.reports import cleanup_expired_shares
from tests.support.consent_api import auth_headers, login, seed_report, seed_user


def _future_expiry_iso(days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def test_share_creation_emits_patient_and_clinician_notifications(consent_api) -> None:
    with consent_api.session_factory() as session:
        patient = seed_user(session, email="patient-share@example.com", role="patient")
        clinician = seed_user(session, email="clinician-share@example.com", role="clinician")
        report = seed_report(session, subject_email=patient.email, created_by_email=patient.email)

    patient_token = login(consent_api, email=patient.email)
    share_resp = consent_api.client.post(
        f"/api/v1/reports/{report.id}/share",
        headers=auth_headers(patient_token),
        json={
            "clinician_email": clinician.email,
            "scope": "report",
            "access_level": "comment",
            "expires_at": _future_expiry_iso(),
        },
    )
    assert share_resp.status_code == 201, share_resp.text

    patient_notifications = consent_api.client.get(
        "/api/v1/notifications",
        headers=auth_headers(patient_token),
    )
    assert patient_notifications.status_code == 200, patient_notifications.text
    assert any(
        item["type"] == "report_shared_confirmed" for item in patient_notifications.json()["items"]
    )

    clinician_token = login(consent_api, email=clinician.email)
    clinician_notifications = consent_api.client.get(
        "/api/v1/notifications",
        headers=auth_headers(clinician_token),
    )
    assert clinician_notifications.status_code == 200, clinician_notifications.text
    assert any(
        item["type"] == "new_report_shared" for item in clinician_notifications.json()["items"]
    )


def test_clinician_reply_emits_patient_notification(consent_api) -> None:
    with consent_api.session_factory() as session:
        patient = seed_user(session, email="patient-thread@example.com", role="patient")
        clinician = seed_user(session, email="clinician-thread@example.com", role="clinician")
        report = seed_report(session, subject_email=patient.email, created_by_email=patient.email)

    patient_token = login(consent_api, email=patient.email)
    consent_api.client.post(
        f"/api/v1/reports/{report.id}/share",
        headers=auth_headers(patient_token),
        json={
            "clinician_email": clinician.email,
            "scope": "report",
            "access_level": "comment",
            "expires_at": _future_expiry_iso(),
            "view_scope": "full_report_with_threads",
        },
    )
    thread_resp = consent_api.client.post(
        f"/api/v1/reports/{report.id}/threads",
        headers=auth_headers(patient_token),
        json={"initial_message": "What does this mean?"},
    )
    assert thread_resp.status_code == 201, thread_resp.text
    thread_id = thread_resp.json()["id"]

    clinician_token = login(consent_api, email=clinician.email)
    reply_resp = consent_api.client.post(
        f"/api/v1/threads/{thread_id}/messages",
        headers=auth_headers(clinician_token),
        json={"body": "Please recheck in a few weeks."},
    )
    assert reply_resp.status_code == 201, reply_resp.text

    patient_notifications = consent_api.client.get(
        "/api/v1/notifications",
        headers=auth_headers(patient_token),
    )
    assert patient_notifications.status_code == 200, patient_notifications.text
    assert any(
        item["type"] == "clinician_replied_in_thread"
        for item in patient_notifications.json()["items"]
    )


def test_report_access_emits_view_notification(consent_api) -> None:
    with consent_api.session_factory() as session:
        patient = seed_user(session, email="patient-view@example.com", role="patient")
        clinician = seed_user(session, email="clinician-view@example.com", role="clinician")
        report = seed_report(session, subject_email=patient.email, created_by_email=patient.email)

    patient_token = login(consent_api, email=patient.email)
    consent_api.client.post(
        f"/api/v1/reports/{report.id}/share",
        headers=auth_headers(patient_token),
        json={
            "clinician_email": clinician.email,
            "scope": "report",
            "access_level": "comment",
            "expires_at": _future_expiry_iso(),
        },
    )

    clinician_token = login(consent_api, email=clinician.email)
    response = consent_api.client.get(
        f"/api/v1/reports/{report.id}",
        headers=auth_headers(clinician_token),
    )
    assert response.status_code == 200, response.text

    patient_notifications = consent_api.client.get(
        "/api/v1/notifications",
        headers=auth_headers(patient_token),
    )
    assert patient_notifications.status_code == 200, patient_notifications.text
    assert any(
        item["type"] == "clinician_viewed_report" for item in patient_notifications.json()["items"]
    )


def test_cleanup_emits_expiry_notifications(consent_api) -> None:
    with consent_api.session_factory() as session:
        patient = seed_user(session, email="patient-expiry@example.com", role="patient")
        clinician = seed_user(session, email="clinician-expiry@example.com", role="clinician")
        report = seed_report(session, subject_email=patient.email, created_by_email=patient.email)

    patient_token = login(consent_api, email=patient.email)
    consent_api.client.post(
        f"/api/v1/reports/{report.id}/share",
        headers=auth_headers(patient_token),
        json={
            "clinician_email": clinician.email,
            "scope": "report",
            "access_level": "comment",
            "expires_at": _future_expiry_iso(),
        },
    )

    with consent_api.session_factory() as session:
        share = session.scalar(
            select(ConsentShare).where(
                ConsentShare.subject_user_id == patient.id,
                ConsentShare.grantee_user_id == clinician.id,
                ConsentShare.report_id == report.id,
            )
        )
        assert share is not None
        share.expires_at = datetime.now(UTC) - timedelta(days=1)
        session.commit()

    asyncio.run(_run_cleanup(consent_api))

    clinician_token = login(consent_api, email=clinician.email)
    notifications = consent_api.client.get(
        "/api/v1/notifications",
        headers=auth_headers(clinician_token),
    )
    assert notifications.status_code == 200, notifications.text
    assert any(item["type"] == "share_expired" for item in notifications.json()["items"])


async def _run_cleanup(consent_api) -> None:
    async with consent_api.client.app.state.database.session_factory() as session:
        await cleanup_expired_shares(session)
