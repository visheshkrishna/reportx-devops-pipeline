"""Integration tests for audit events emitted by share and revoke operations."""

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
    report_id: str,
    patient_token: str,
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
    report_id: str,
    patient_token: str,
    clinician_email: str,
) -> None:
    response = consent_api.client.post(
        f"/api/v1/reports/{report_id}/share/revoke",
        headers=auth_headers(patient_token),
        json={"clinician_email": clinician_email},
    )
    assert response.status_code == 204, response.text


def _audit_rows(
    consent_api: ConsentApiHarness,
    *,
    report_id: str,
    patient_token: str,
) -> list[dict]:
    response = consent_api.client.get(
        f"/api/v1/audit/reports/{report_id}",
        headers=auth_headers(patient_token),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, list)
    return payload


def _parse_occurred_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def test_share_creation_emits_created_audit_with_required_context(
    consent_api: ConsentApiHarness,
) -> None:
    patient_email = "patient-audit-created@example.com"
    clinician_email = "clinician-audit-created@example.com"

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
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        scope="report",
        access_level="comment",
    )

    rows = _audit_rows(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
    )
    created = next((row for row in rows if row["action"] == "created"), None)
    assert created is not None

    context = created["context"]
    assert context["scope"] == "report"
    assert context["access_level"] == "comment"
    assert context["grantee_email"] == clinician_email
    assert context["report_id"] == report.id
    assert "expires_at" in context


def test_patient_scope_share_audit_context_marks_scope_patient(
    consent_api: ConsentApiHarness,
) -> None:
    patient_email = "patient-audit-patient-scope@example.com"
    clinician_email = "clinician-audit-patient-scope@example.com"

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
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        scope="patient",
    )

    rows = _audit_rows(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
    )
    created = next((row for row in rows if row["action"] == "created"), None)
    assert created is not None
    assert created["context"]["scope"] == "patient"
    assert created["context"]["report_id"] is None


def test_revoke_emits_revoked_audit_with_required_context(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-audit-revoke@example.com"
    clinician_email = "clinician-audit-revoke@example.com"

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
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        access_level="comment",
    )
    _revoke_share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
    )

    rows = _audit_rows(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
    )
    assert any(row["action"] == "created" for row in rows)
    assert any(row["action"] == "revoked" for row in rows)

    revoked = next((row for row in rows if row["action"] == "revoked"), None)
    assert revoked is not None
    context = revoked["context"]
    assert context["scope"] == "report"
    assert context["access_level"] == "comment"
    assert context["grantee_email"] == clinician_email
    assert context["report_id"] == report.id


def test_audit_occurred_at_for_created_event_is_recent(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-audit-timestamp@example.com"
    clinician_email = "clinician-audit-timestamp@example.com"

    with consent_api.session_factory() as session:
        patient = seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient.email,
            created_by_email=patient.email,
        )

    patient_token = login(consent_api, email=patient_email)
    before = datetime.now(UTC)
    _share_report(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
    )
    after = datetime.now(UTC)

    rows = _audit_rows(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
    )
    created = next((row for row in rows if row["action"] == "created"), None)
    assert created is not None

    occurred_at = _parse_occurred_at(created["occurred_at"])
    assert before <= occurred_at <= after
