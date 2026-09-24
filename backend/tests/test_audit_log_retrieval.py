"""Integration tests for patient audit log retrieval endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.support.consent_api import (
    ConsentApiHarness,
    auth_headers,
    login,
    seed_report,
    seed_user,
)


def _future_expiry_iso(*, days: int = 7) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _share_report(
    consent_api: ConsentApiHarness,
    *,
    patient_token: str,
    report_id: str,
    clinician_email: str,
    scope: str = "report",
    access_level: str = "read",
) -> dict:
    response = consent_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        headers=auth_headers(patient_token),
        json={
            "clinician_email": clinician_email,
            "scope": scope,
            "access_level": access_level,
            "expires_at": _future_expiry_iso(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _revoke_share(
    consent_api: ConsentApiHarness,
    *,
    patient_token: str,
    report_id: str,
    clinician_email: str,
) -> None:
    response = consent_api.client.post(
        f"/api/v1/reports/{report_id}/share/revoke",
        headers=auth_headers(patient_token),
        json={"clinician_email": clinician_email},
    )
    assert response.status_code == 204, response.text


def _get_audit_log(
    consent_api: ConsentApiHarness,
    *,
    patient_token: str,
    report_id: str,
    action: str | None = None,
):
    suffix = f"?action={action}" if action else ""
    return consent_api.client.get(
        f"/api/v1/audit/reports/{report_id}{suffix}",
        headers=auth_headers(patient_token),
    )


def _parse_occurred_at(value: str) -> datetime:
    # FastAPI may emit UTC timestamps with a trailing Z.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def test_patient_can_retrieve_audit_log_for_own_report(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-own@example.com"
    clinician_email = "clinician-own@example.com"

    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    _share_report(
        consent_api,
        patient_token=patient_token,
        report_id=report.id,
        clinician_email=clinician_email,
    )

    response = _get_audit_log(
        consent_api,
        patient_token=patient_token,
        report_id=report.id,
    )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert isinstance(rows, list)
    assert len(rows) >= 1
    assert any(row["action"] == "created" for row in rows)


def test_audit_events_are_ordered_newest_first(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-order@example.com"
    clinician_email = "clinician-order@example.com"

    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    _share_report(
        consent_api,
        patient_token=patient_token,
        report_id=report.id,
        clinician_email=clinician_email,
    )
    _revoke_share(
        consent_api,
        patient_token=patient_token,
        report_id=report.id,
        clinician_email=clinician_email,
    )

    response = _get_audit_log(
        consent_api,
        patient_token=patient_token,
        report_id=report.id,
    )
    assert response.status_code == 200, response.text

    rows = response.json()
    assert len(rows) >= 2
    for idx in range(len(rows) - 1):
        assert _parse_occurred_at(rows[idx]["occurred_at"]) >= _parse_occurred_at(
            rows[idx + 1]["occurred_at"]
        )


def test_created_and_revoked_events_include_context_fields(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-context@example.com"
    clinician_email = "clinician-context@example.com"

    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    _share_report(
        consent_api,
        patient_token=patient_token,
        report_id=report.id,
        clinician_email=clinician_email,
        access_level="comment",
    )
    _revoke_share(
        consent_api,
        patient_token=patient_token,
        report_id=report.id,
        clinician_email=clinician_email,
    )

    response = _get_audit_log(
        consent_api,
        patient_token=patient_token,
        report_id=report.id,
    )
    assert response.status_code == 200, response.text

    rows = response.json()
    created_event = next((row for row in rows if row["action"] == "created"), None)
    revoked_event = next((row for row in rows if row["action"] == "revoked"), None)
    assert created_event is not None
    assert revoked_event is not None

    created_context = created_event["context"]
    assert created_context["scope"] == "report"
    assert created_context["access_level"] == "comment"
    assert created_context["grantee_email"] == clinician_email
    assert created_context["report_id"] == report.id
    assert "expires_at" in created_context

    revoked_context = revoked_event["context"]
    assert revoked_context["scope"] == "report"
    assert revoked_context["access_level"] == "comment"
    assert revoked_context["grantee_email"] == clinician_email
    assert revoked_context["report_id"] == report.id


def test_non_owner_access_to_audit_log_is_denied(consent_api: ConsentApiHarness) -> None:
    owner_email = "patient-owner@example.com"
    other_patient_email = "patient-other@example.com"

    with consent_api.session_factory() as session:
        owner = seed_user(session, email=owner_email, role="patient")
        seed_user(session, email=other_patient_email, role="patient")
        report = seed_report(
            session,
            subject_email=owner.email,
            created_by_email=owner.email,
        )

    other_token = login(consent_api, email=other_patient_email)
    forbidden = _get_audit_log(
        consent_api,
        patient_token=other_token,
        report_id=report.id,
    )

    assert forbidden.status_code == 403


def test_audit_log_action_filter_returns_only_requested_action(
    consent_api: ConsentApiHarness,
) -> None:
    patient_email = "patient-filter@example.com"
    clinician_email = "clinician-filter@example.com"

    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    _share_report(
        consent_api,
        patient_token=patient_token,
        report_id=report.id,
        clinician_email=clinician_email,
    )
    _revoke_share(
        consent_api,
        patient_token=patient_token,
        report_id=report.id,
        clinician_email=clinician_email,
    )

    response = _get_audit_log(
        consent_api,
        patient_token=patient_token,
        report_id=report.id,
        action="revoked",
    )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) >= 1
    assert all(row["action"] == "revoked" for row in rows)


def test_audit_log_excludes_events_from_other_reports(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-scope@example.com"
    clinician_email = "clinician-scope@example.com"

    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report_a = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )
        report_b = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    _share_report(
        consent_api,
        patient_token=patient_token,
        report_id=report_a.id,
        clinician_email=clinician_email,
    )
    _share_report(
        consent_api,
        patient_token=patient_token,
        report_id=report_b.id,
        clinician_email=clinician_email,
    )

    response = _get_audit_log(
        consent_api,
        patient_token=patient_token,
        report_id=report_a.id,
    )
    assert response.status_code == 200, response.text

    created_rows = [row for row in response.json() if row["action"] == "created"]
    assert any(row["context"].get("report_id") == report_a.id for row in created_rows)

    unrelated_events = [
        row for row in created_rows if row["context"].get("report_id") != report_a.id
    ]
    assert unrelated_events == []


def test_patient_scope_view_events_do_not_leak_across_reports(
    consent_api: ConsentApiHarness,
) -> None:
    patient_email = "patient-view-leak@example.com"
    clinician_email = "clinician-view-leak@example.com"

    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report_a = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )
        report_b = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    share = _share_report(
        consent_api,
        patient_token=patient_token,
        report_id=report_a.id,
        clinician_email=clinician_email,
        scope="patient",
    )
    assert share["scope"] == "patient"

    clinician_token = login(consent_api, email=clinician_email)
    access_report_b = consent_api.client.get(
        f"/api/v1/reports/{report_b.id}",
        headers=auth_headers(clinician_token),
    )
    assert access_report_b.status_code == 200, access_report_b.text

    response = _get_audit_log(
        consent_api,
        patient_token=patient_token,
        report_id=report_a.id,
    )
    assert response.status_code == 200, response.text

    leaked_view_events = [
        row
        for row in response.json()
        if row["action"] == "view" and row["context"].get("report_id") == report_b.id
    ]
    assert leaked_view_events == []
