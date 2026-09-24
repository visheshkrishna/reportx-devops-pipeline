"""T7 — thread anchors, notifications, and access control.

These tests cover the gaps identified in the T6/T7 delivery sweep:
- threads can be anchored to a specific finding on a report
- unauthorized users cannot post into a thread they don't participate in
- new messages create Notification rows for the *other* participants
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import Notification, User
from tests.factories import PersistenceFactory
from tests.support.consent_api import (
    ConsentApiHarness,
    auth_headers,
    login,
    seed_report,
    seed_user,
)


def _future_expiry_iso(days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _seed_finding(
    consent_api: ConsentApiHarness,
    *,
    report_id: str,
    patient_email: str,
    biomarker: str,
) -> str:
    with consent_api.session_factory() as session:
        factory = PersistenceFactory(session)
        patient = session.scalar(select(User).where(User.email == patient_email))
        assert patient is not None
        report = session.get_one = None  # noqa: F841 — dummy to keep lint quiet if used
        from app.db.models import Report as _Report  # local import to avoid cycles above

        report_obj = session.scalar(select(_Report).where(_Report.id == report_id))
        assert report_obj is not None
        finding = factory.create_finding(
            report=report_obj,
            patient=patient,
            biomarker_key=biomarker,
            display_name=biomarker.title(),
            value_numeric=6.2,
            unit="mmol/L",
            flag="high",
            reference_low=3.9,
            reference_high=5.5,
        )
        session.commit()
        return finding.id


def _create_thread(
    consent_api: ConsentApiHarness,
    *,
    report_id: str,
    token: str,
    initial_message: str = "What does this result mean?",
    finding_id: str | None = None,
    title: str | None = None,
) -> dict:
    body: dict = {"initial_message": initial_message}
    if finding_id is not None:
        body["finding_id"] = finding_id
    if title is not None:
        body["title"] = title
    response = consent_api.client.post(
        f"/api/v1/reports/{report_id}/threads",
        headers=auth_headers(token),
        json=body,
    )
    return response


def test_thread_can_be_anchored_to_a_finding(consent_api: ConsentApiHarness) -> None:
    patient_email = "anchor-patient@example.com"
    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    finding_id = _seed_finding(
        consent_api,
        report_id=report.id,
        patient_email=patient_email,
        biomarker="glucose",
    )
    patient_token = login(consent_api, email=patient_email)

    response = _create_thread(
        consent_api,
        report_id=report.id,
        token=patient_token,
        finding_id=finding_id,
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["finding_id"] == finding_id
    assert data["finding_label"] and "Glucose" in data["finding_label"]


def test_thread_rejects_unknown_finding(consent_api: ConsentApiHarness) -> None:
    patient_email = "anchor-bad-patient@example.com"
    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    response = _create_thread(
        consent_api,
        report_id=report.id,
        token=patient_token,
        finding_id="00000000-0000-0000-0000-000000000000",
    )
    assert response.status_code == 400, response.text


def test_thread_creation_notifies_authorized_clinician(consent_api: ConsentApiHarness) -> None:
    patient_email = "notify-patient@example.com"
    clinician_email = "notify-clinician@example.com"
    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )
        patient_id = patient.id

    patient_token = login(consent_api, email=patient_email)

    # Patient grants the clinician full thread access, then creates a thread.
    share_resp = consent_api.client.post(
        f"/api/v1/reports/{report.id}/share",
        headers=auth_headers(patient_token),
        json={
            "clinician_email": clinician_email,
            "scope": "report",
            "access_level": "comment",
            "expires_at": _future_expiry_iso(),
            "view_scope": "full_report_with_threads",
        },
    )
    assert share_resp.status_code == 201, share_resp.text

    thread_resp = _create_thread(
        consent_api,
        report_id=report.id,
        token=patient_token,
        title="Glucose question",
    )
    assert thread_resp.status_code == 201, thread_resp.text
    thread_id = thread_resp.json()["id"]

    # Clinician reply should land as a notification on the patient.
    clinician_token = login(consent_api, email=clinician_email)
    reply = consent_api.client.post(
        f"/api/v1/threads/{thread_id}/messages",
        headers=auth_headers(clinician_token),
        json={"body": "Slight elevation, recheck in 3 months."},
    )
    assert reply.status_code == 201, reply.text

    # The patient now has a notification for the clinician reply.
    notifications = consent_api.client.get(
        "/api/v1/notifications",
        headers=auth_headers(patient_token),
    )
    assert notifications.status_code == 200, notifications.text
    payload = notifications.json()
    assert payload["total_unread"] >= 1
    assert any(item.get("thread_id") == thread_id for item in payload["items"])

    unread = consent_api.client.get(
        "/api/v1/notifications/unread-count",
        headers=auth_headers(patient_token),
    )
    assert unread.status_code == 200
    assert unread.json()["unread"] >= 1

    # Verify row was really written against the patient.
    with consent_api.session_factory() as session:
        rows = session.scalars(select(Notification).where(Notification.user_id == patient_id)).all()
        assert len(rows) >= 1


def test_message_endpoint_rejects_non_participants(consent_api: ConsentApiHarness) -> None:
    patient_email = "access-patient@example.com"
    stranger_email = "access-stranger@example.com"
    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=stranger_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    thread_resp = _create_thread(consent_api, report_id=report.id, token=patient_token)
    assert thread_resp.status_code == 201, thread_resp.text
    thread_id = thread_resp.json()["id"]

    stranger_token = login(consent_api, email=stranger_email)
    reply = consent_api.client.post(
        f"/api/v1/threads/{thread_id}/messages",
        headers=auth_headers(stranger_token),
        json={"body": "I should not be able to post."},
    )
    assert reply.status_code == 403, reply.text


def test_messages_are_returned_in_chronological_order(consent_api: ConsentApiHarness) -> None:
    patient_email = "ordering-patient@example.com"
    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    thread_resp = _create_thread(
        consent_api,
        report_id=report.id,
        token=patient_token,
        initial_message="first",
    )
    thread_id = thread_resp.json()["id"]

    for idx, body in enumerate(["second", "third"], start=2):
        reply = consent_api.client.post(
            f"/api/v1/threads/{thread_id}/messages",
            headers=auth_headers(patient_token),
            json={"body": body},
        )
        assert reply.status_code == 201, reply.text
        assert reply.json()["author_role"] == "patient"

    listing = consent_api.client.get(
        f"/api/v1/reports/{report.id}/threads",
        headers=auth_headers(patient_token),
    )
    assert listing.status_code == 200
    bodies = [m["body"] for m in listing.json()[0]["messages"]]
    assert bodies == ["first", "second", "third"]


def test_mark_notification_as_read(consent_api: ConsentApiHarness) -> None:
    patient_email = "read-patient@example.com"
    clinician_email = "read-clinician@example.com"
    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    consent_api.client.post(
        f"/api/v1/reports/{report.id}/share",
        headers=auth_headers(patient_token),
        json={
            "clinician_email": clinician_email,
            "scope": "report",
            "access_level": "comment",
            "expires_at": _future_expiry_iso(),
            "view_scope": "full_report_with_threads",
        },
    )

    thread_resp = _create_thread(consent_api, report_id=report.id, token=patient_token)
    thread_id = thread_resp.json()["id"]

    clinician_token = login(consent_api, email=clinician_email)
    consent_api.client.post(
        f"/api/v1/threads/{thread_id}/messages",
        headers=auth_headers(clinician_token),
        json={"body": "Reply from clinician."},
    )

    listing = consent_api.client.get(
        "/api/v1/notifications",
        headers=auth_headers(patient_token),
    )
    thread_notification = next(
        item for item in listing.json()["items"] if item.get("thread_id") == thread_id
    )
    notif_id = thread_notification["id"]

    read_resp = consent_api.client.post(
        f"/api/v1/notifications/{notif_id}/read",
        headers=auth_headers(patient_token),
    )
    assert read_resp.status_code == 204

    unread = consent_api.client.get(
        "/api/v1/notifications/unread-count",
        headers=auth_headers(patient_token),
    )
    assert unread.json()["unread"] == listing.json()["total_unread"] - 1
