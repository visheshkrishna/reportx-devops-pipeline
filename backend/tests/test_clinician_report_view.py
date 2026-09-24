"""TDD tests for T17 clinician scoped report view endpoints.

Covers:
  GET /clinician/shared-reports          — dashboard
  GET /clinician/shared-reports/{id}     — scoped report view

All scope enforcement is server-side; the tests drive the implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import ConsentShare, ShareViewScope
from tests.support.consent_api import (
    ConsentApiHarness,
    auth_headers,
    consent_api,
    login,
    seed_report,
    seed_user,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _share(
    harness: ConsentApiHarness,
    *,
    report_id: str,
    patient_token: str,
    clinician_email: str,
    view_scope: str = "summary_only",
    include_doctor_summary: bool = False,
    expires_in_days: int = 7,
) -> dict:
    resp = harness.client.post(
        f"/api/v1/reports/{report_id}/share",
        json={
            "clinician_email": clinician_email,
            "scope": "report",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=expires_in_days)).isoformat(),
            "view_scope": view_scope,
            "include_doctor_summary": include_doctor_summary,
        },
        headers=auth_headers(patient_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _dashboard(harness: ConsentApiHarness, *, clinician_token: str) -> list[dict]:
    resp = harness.client.get(
        "/api/v1/clinician/shared-reports",
        headers=auth_headers(clinician_token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _report_view(
    harness: ConsentApiHarness,
    *,
    clinician_token: str,
    report_id: str,
    expect_status: int = 200,
) -> dict:
    resp = harness.client.get(
        f"/api/v1/clinician/shared-reports/{report_id}",
        headers=auth_headers(clinician_token),
    )
    assert resp.status_code == expect_status, resp.text
    return resp.json()


# ── dashboard tests ───────────────────────────────────────────────────────────


def test_non_clinician_cannot_access_clinician_dashboard(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-nodash@example.com", role="patient")

    token = login(consent_api, email="patient-nodash@example.com")
    resp = consent_api.client.get(
        "/api/v1/clinician/shared-reports",
        headers=auth_headers(token),
    )
    assert resp.status_code == 403


def test_dashboard_returns_only_active_shares(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-dash-act@example.com", role="patient")
        seed_user(session, email="clinician-dash-act@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-dash-act@example.com",
            created_by_email="patient-dash-act@example.com",
        )

    patient_token = login(consent_api, email="patient-dash-act@example.com")
    clinician_token = login(consent_api, email="clinician-dash-act@example.com")

    _share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email="clinician-dash-act@example.com",
    )

    items = _dashboard(consent_api, clinician_token=clinician_token)
    assert len(items) == 1
    item = items[0]
    assert item["report_id"] == report.id
    assert "patient_name" in item
    assert "report_date" in item
    assert "panel_type" in item
    assert "view_scope" in item
    assert "expires_at" in item
    assert "include_doctor_summary" in item


def test_dashboard_excludes_expired_shares(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-expired-dash@example.com", role="patient")
        seed_user(session, email="clinician-expired-dash@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-expired-dash@example.com",
            created_by_email="patient-expired-dash@example.com",
        )

    patient_token = login(consent_api, email="patient-expired-dash@example.com")
    share_data = _share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email="clinician-expired-dash@example.com",
    )

    with consent_api.session_factory() as session:
        row = session.scalar(select(ConsentShare).where(ConsentShare.id == share_data["id"]))
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()

    clinician_token = login(consent_api, email="clinician-expired-dash@example.com")
    items = _dashboard(consent_api, clinician_token=clinician_token)
    assert all(item["report_id"] != report.id for item in items)


def test_dashboard_excludes_revoked_shares(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-revoked-dash@example.com", role="patient")
        seed_user(session, email="clinician-revoked-dash@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-revoked-dash@example.com",
            created_by_email="patient-revoked-dash@example.com",
        )

    patient_token = login(consent_api, email="patient-revoked-dash@example.com")
    _share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email="clinician-revoked-dash@example.com",
    )

    consent_api.client.post(
        f"/api/v1/reports/{report.id}/share/revoke",
        json={"clinician_email": "clinician-revoked-dash@example.com"},
        headers=auth_headers(patient_token),
    )

    clinician_token = login(consent_api, email="clinician-revoked-dash@example.com")
    items = _dashboard(consent_api, clinician_token=clinician_token)
    assert all(item["report_id"] != report.id for item in items)


def test_dashboard_item_includes_patient_profile(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(
            session,
            email="patient-profile-dash@example.com",
            role="patient",
            display_name="Alice Patient",
        )
        seed_user(session, email="clinician-profile-dash@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-profile-dash@example.com",
            created_by_email="patient-profile-dash@example.com",
        )

    patient_token = login(consent_api, email="patient-profile-dash@example.com")
    _share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email="clinician-profile-dash@example.com",
    )

    clinician_token = login(consent_api, email="clinician-profile-dash@example.com")
    items = _dashboard(consent_api, clinician_token=clinician_token)
    assert len(items) == 1
    assert items[0]["patient_name"] == "Alice Patient"


# ── scoped report view tests ──────────────────────────────────────────────────


def test_unshared_report_returns_403_not_404(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-403@example.com", role="patient")
        seed_user(session, email="clinician-403@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-403@example.com",
            created_by_email="patient-403@example.com",
        )

    clinician_token = login(consent_api, email="clinician-403@example.com")
    resp = consent_api.client.get(
        f"/api/v1/clinician/shared-reports/{report.id}",
        headers=auth_headers(clinician_token),
    )
    assert resp.status_code == 403
    body = resp.json()
    assert "access" in body["detail"].lower() or "share" in body["detail"].lower()


def test_summary_only_response_has_no_findings_or_trends(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-sumonly@example.com", role="patient")
        seed_user(session, email="clinician-sumonly@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-sumonly@example.com",
            created_by_email="patient-sumonly@example.com",
        )

    patient_token = login(consent_api, email="patient-sumonly@example.com")
    _share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email="clinician-sumonly@example.com",
        view_scope="summary_only",
    )

    clinician_token = login(consent_api, email="clinician-sumonly@example.com")
    body = _report_view(consent_api, clinician_token=clinician_token, report_id=report.id)

    assert body["view_scope"] == "summary_only"
    assert "findings" not in body or body["findings"] is None
    assert "trends" not in body or body["trends"] is None
    assert "threads" not in body or body["threads"] is None
    assert "patient" in body
    assert "report_date" in body


def test_full_report_response_includes_findings_and_trends(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-fullrep@example.com", role="patient")
        seed_user(session, email="clinician-fullrep@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-fullrep@example.com",
            created_by_email="patient-fullrep@example.com",
        )

    patient_token = login(consent_api, email="patient-fullrep@example.com")
    _share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email="clinician-fullrep@example.com",
        view_scope="full_report",
    )

    clinician_token = login(consent_api, email="clinician-fullrep@example.com")
    body = _report_view(consent_api, clinician_token=clinician_token, report_id=report.id)

    assert body["view_scope"] == "full_report"
    assert "findings" in body
    assert isinstance(body["findings"], list)
    assert "trends" in body
    assert "threads" not in body or body["threads"] is None


def test_full_report_with_threads_includes_thread_list(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-threads@example.com", role="patient")
        seed_user(session, email="clinician-threads@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-threads@example.com",
            created_by_email="patient-threads@example.com",
        )

    patient_token = login(consent_api, email="patient-threads@example.com")
    _share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email="clinician-threads@example.com",
        view_scope="full_report_with_threads",
    )

    clinician_token = login(consent_api, email="clinician-threads@example.com")
    body = _report_view(consent_api, clinician_token=clinician_token, report_id=report.id)

    assert body["view_scope"] == "full_report_with_threads"
    assert "findings" in body
    assert "trends" in body
    assert "threads" in body
    assert isinstance(body["threads"], list)


def test_doctor_summary_included_when_flag_set(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-docsum@example.com", role="patient")
        seed_user(session, email="clinician-docsum@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-docsum@example.com",
            created_by_email="patient-docsum@example.com",
        )

    patient_token = login(consent_api, email="patient-docsum@example.com")
    _share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email="clinician-docsum@example.com",
        view_scope="full_report",
        include_doctor_summary=True,
    )

    clinician_token = login(consent_api, email="clinician-docsum@example.com")
    body = _report_view(consent_api, clinician_token=clinician_token, report_id=report.id)

    assert body["include_doctor_summary"] is True
    assert "doctor_summary" in body


def test_doctor_summary_absent_when_flag_false(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-nods@example.com", role="patient")
        seed_user(session, email="clinician-nods@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-nods@example.com",
            created_by_email="patient-nods@example.com",
        )

    patient_token = login(consent_api, email="patient-nods@example.com")
    _share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email="clinician-nods@example.com",
        view_scope="full_report",
        include_doctor_summary=False,
    )

    clinician_token = login(consent_api, email="clinician-nods@example.com")
    body = _report_view(consent_api, clinician_token=clinician_token, report_id=report.id)

    assert body["include_doctor_summary"] is False
    assert body.get("doctor_summary") is None


# ── authorization boundary tests ──────────────────────────────────────────────


def test_clinician_cannot_access_audit_log(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-auditck@example.com", role="patient")
        seed_user(session, email="clinician-auditck@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-auditck@example.com",
            created_by_email="patient-auditck@example.com",
        )

    clinician_token = login(consent_api, email="clinician-auditck@example.com")
    resp = consent_api.client.get(
        f"/api/v1/audit/reports/{report.id}",
        headers=auth_headers(clinician_token),
    )
    assert resp.status_code == 403


def test_clinician_cannot_create_share(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-shareck@example.com", role="patient")
        seed_user(session, email="clinician-shareck@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-shareck@example.com",
            created_by_email="patient-shareck@example.com",
        )

    clinician_token = login(consent_api, email="clinician-shareck@example.com")
    resp = consent_api.client.post(
        f"/api/v1/reports/{report.id}/share",
        json={
            "clinician_email": "other@example.com",
            "scope": "report",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        },
        headers=auth_headers(clinician_token),
    )
    assert resp.status_code == 403


def test_clinician_cannot_revoke_share(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-revokeck@example.com", role="patient")
        seed_user(session, email="clinician-revokeck@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-revokeck@example.com",
            created_by_email="patient-revokeck@example.com",
        )

    clinician_token = login(consent_api, email="clinician-revokeck@example.com")
    resp = consent_api.client.post(
        f"/api/v1/reports/{report.id}/share/revoke",
        json={"clinician_email": "clinician-revokeck@example.com"},
        headers=auth_headers(clinician_token),
    )
    assert resp.status_code == 403


def test_non_clinician_cannot_access_scoped_report_view(consent_api: ConsentApiHarness) -> None:
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-scopeck@example.com", role="patient")
        report = seed_report(
            session,
            subject_email="patient-scopeck@example.com",
            created_by_email="patient-scopeck@example.com",
        )

    patient_token = login(consent_api, email="patient-scopeck@example.com")
    resp = consent_api.client.get(
        f"/api/v1/clinician/shared-reports/{report.id}",
        headers=auth_headers(patient_token),
    )
    assert resp.status_code == 403


def test_share_revalidated_on_every_request(consent_api: ConsentApiHarness) -> None:
    """Accessing report after share is revoked must return 403 — never stale-cached 200."""
    with consent_api.session_factory() as session:
        seed_user(session, email="patient-revalid@example.com", role="patient")
        seed_user(session, email="clinician-revalid@example.com", role="clinician")
        report = seed_report(
            session,
            subject_email="patient-revalid@example.com",
            created_by_email="patient-revalid@example.com",
        )

    patient_token = login(consent_api, email="patient-revalid@example.com")
    _share(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email="clinician-revalid@example.com",
        view_scope="full_report",
    )

    clinician_token = login(consent_api, email="clinician-revalid@example.com")
    # First access should succeed
    _report_view(
        consent_api, clinician_token=clinician_token, report_id=report.id, expect_status=200
    )

    # Revoke the share
    consent_api.client.post(
        f"/api/v1/reports/{report.id}/share/revoke",
        json={"clinician_email": "clinician-revalid@example.com"},
        headers=auth_headers(patient_token),
    )

    # Subsequent access must be denied
    _report_view(
        consent_api, clinician_token=clinician_token, report_id=report.id, expect_status=403
    )
