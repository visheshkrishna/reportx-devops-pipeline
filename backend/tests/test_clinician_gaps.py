"""TDD tests for the T17 gap-fill:

Gap 1 — Patient DOB in profile (User.date_of_birth, registration, clinician view)
Gap 2 — Thread message scope enforcement for clinicians
         (full_report_with_threads required to post; summary_only / full_report → 403)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.support.consent_api import (
    ConsentApiHarness,
    auth_headers,
    consent_api,
    login,
    seed_report,
    seed_user,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _register(harness: ConsentApiHarness, *, email: str, role: str, dob: str | None = None) -> dict:
    body: dict = {
        "email": email,
        "password": "Password123!",
        "role": role,
        "display_name": "Test User",
    }
    if dob is not None:
        body["date_of_birth"] = dob
    resp = harness.client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _share_with_view_scope(
    harness: ConsentApiHarness,
    *,
    report_id: str,
    patient_token: str,
    clinician_email: str,
    view_scope: str,
) -> dict:
    resp = harness.client.post(
        f"/api/v1/reports/{report_id}/share",
        json={
            "clinician_email": clinician_email,
            "scope": "report",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
            "view_scope": view_scope,
            "include_doctor_summary": False,
        },
        headers=auth_headers(patient_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _open_thread(harness: ConsentApiHarness, *, report_id: str, token: str) -> str:
    """Create a thread as the patient; returns thread_id."""
    resp = harness.client.post(
        f"/api/v1/reports/{report_id}/threads",
        json={"initial_message": "Hello, doctor.", "title": "Check-in"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _post_message(
    harness: ConsentApiHarness, *, thread_id: str, token: str, body: str = "Reply text"
) -> int:
    resp = harness.client.post(
        f"/api/v1/threads/{thread_id}/messages",
        json={"body": body},
        headers=auth_headers(token),
    )
    return resp.status_code


# ── Gap 1: DOB ────────────────────────────────────────────────────────────────


def test_register_accepts_date_of_birth(consent_api: ConsentApiHarness) -> None:
    result = _register(
        consent_api,
        email="patient-dob-reg@example.com",
        role="patient",
        dob="1990-06-15",
    )
    assert result["user"]["date_of_birth"] == "1990-06-15"


def test_register_without_dob_returns_null(consent_api: ConsentApiHarness) -> None:
    result = _register(
        consent_api,
        email="patient-no-dob@example.com",
        role="patient",
    )
    assert result["user"]["date_of_birth"] is None


def test_clinician_report_view_includes_patient_dob(consent_api: ConsentApiHarness) -> None:
    # Register patient with DOB, then share a report and confirm DOB appears in the
    # clinician's scoped report view response.
    patient_email = "patient-dob-view@example.com"
    clinician_email = "clinician-dob-view@example.com"

    _register(consent_api, email=patient_email, role="patient", dob="1985-03-22")
    _register(consent_api, email=clinician_email, role="clinician")

    patient_token = login(consent_api, email=patient_email)
    clinician_token = login(consent_api, email=clinician_email)

    with consent_api.session_factory() as session:
        report = seed_report(session, subject_email=patient_email, created_by_email=patient_email)

    _share_with_view_scope(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        view_scope="summary_only",
    )

    resp = consent_api.client.get(
        f"/api/v1/clinician/shared-reports/{report.id}",
        headers=auth_headers(clinician_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "patient" in body
    assert body["patient"]["date_of_birth"] == "1985-03-22"


def test_clinician_dashboard_includes_dob_in_patient_profile(
    consent_api: ConsentApiHarness,
) -> None:
    patient_email = "patient-dob-dash@example.com"
    clinician_email = "clinician-dob-dash@example.com"

    _register(consent_api, email=patient_email, role="patient", dob="2000-01-01")
    _register(consent_api, email=clinician_email, role="clinician")

    patient_token = login(consent_api, email=patient_email)
    clinician_token = login(consent_api, email=clinician_email)

    with consent_api.session_factory() as session:
        report = seed_report(session, subject_email=patient_email, created_by_email=patient_email)

    _share_with_view_scope(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        view_scope="full_report",
    )

    resp = consent_api.client.get(
        "/api/v1/clinician/shared-reports",
        headers=auth_headers(clinician_token),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert any(item["report_id"] == report.id for item in items)


# ── Gap 2: Thread scope enforcement ──────────────────────────────────────────


def test_clinician_summary_only_cannot_post_to_thread(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-thread-sum@example.com"
    clinician_email = "clinician-thread-sum@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(session, subject_email=patient_email, created_by_email=patient_email)

    patient_token = login(consent_api, email=patient_email)
    clinician_token = login(consent_api, email=clinician_email)

    _share_with_view_scope(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        view_scope="summary_only",
    )

    thread_id = _open_thread(consent_api, report_id=report.id, token=patient_token)
    status_code = _post_message(consent_api, thread_id=thread_id, token=clinician_token)
    assert status_code == 403


def test_clinician_full_report_cannot_post_to_thread(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-thread-full@example.com"
    clinician_email = "clinician-thread-full@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(session, subject_email=patient_email, created_by_email=patient_email)

    patient_token = login(consent_api, email=patient_email)
    clinician_token = login(consent_api, email=clinician_email)

    _share_with_view_scope(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        view_scope="full_report",
    )

    thread_id = _open_thread(consent_api, report_id=report.id, token=patient_token)
    status_code = _post_message(consent_api, thread_id=thread_id, token=clinician_token)
    assert status_code == 403


def test_clinician_full_report_with_threads_can_post(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-thread-threads@example.com"
    clinician_email = "clinician-thread-threads@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(session, subject_email=patient_email, created_by_email=patient_email)

    patient_token = login(consent_api, email=patient_email)
    clinician_token = login(consent_api, email=clinician_email)

    _share_with_view_scope(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        view_scope="full_report_with_threads",
    )

    thread_id = _open_thread(consent_api, report_id=report.id, token=patient_token)
    status_code = _post_message(consent_api, thread_id=thread_id, token=clinician_token)
    assert status_code == 201


def test_patient_can_always_post_to_own_thread(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-own-thread@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=patient_email, role="patient")
        report = seed_report(session, subject_email=patient_email, created_by_email=patient_email)

    patient_token = login(consent_api, email=patient_email)
    thread_id = _open_thread(consent_api, report_id=report.id, token=patient_token)
    status_code = _post_message(consent_api, thread_id=thread_id, token=patient_token)
    assert status_code == 201
