"""Integration tests for clinician shared-reports discovery endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import ConsentShare
from tests.support.consent_api import (
    ConsentApiHarness,
    auth_headers,
    login,
    seed_report,
    seed_user,
)


def test_harness_fixture_is_available(consent_api: ConsentApiHarness) -> None:
    assert consent_api.client is not None
    assert consent_api.session_factory is not None


def test_non_clinician_cannot_list_shared_reports(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-non-clinician@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=patient_email, role="patient")

    patient_token = login(consent_api, email=patient_email)
    response = consent_api.client.get(
        "/api/v1/reports/shared-reports",
        headers=auth_headers(patient_token),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Only clinicians may view shared reports"


def _share_report(
    consent_api: ConsentApiHarness,
    *,
    report_id: str,
    patient_token: str,
    clinician_email: str,
    scope: str = "report",
    access_level: str = "read",
    include_doctor_summary: bool = False,
) -> dict:
    response = consent_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        json={
            "clinician_email": clinician_email,
            "scope": scope,
            "access_level": access_level,
            "include_doctor_summary": include_doctor_summary,
            "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        },
        headers=auth_headers(patient_token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _list_shared_reports(consent_api: ConsentApiHarness, *, clinician_token: str) -> list[dict]:
    response = consent_api.client.get(
        "/api/v1/reports/shared-reports",
        headers=auth_headers(clinician_token),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload, list)
    return payload


def _get_shared_report_detail(
    consent_api: ConsentApiHarness,
    *,
    clinician_token: str,
    report_id: str,
) -> dict:
    response = consent_api.client.get(
        f"/api/v1/reports/shared-reports/{report_id}",
        headers=auth_headers(clinician_token),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_clinician_can_list_actively_shared_reports(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-list@example.com"
    clinician_email = "clinician-list@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report_1 = seed_report(
            session,
            subject_email=patient_email,
            created_by_email=patient_email,
        )
        report_2 = seed_report(
            session,
            subject_email=patient_email,
            created_by_email=patient_email,
        )

    patient_token = login(consent_api, email=patient_email)
    _share_report(
        consent_api,
        report_id=report_1.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        access_level="read",
    )
    _share_report(
        consent_api,
        report_id=report_2.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        access_level="comment",
    )

    clinician_token = login(consent_api, email=clinician_email)
    payload = _list_shared_reports(consent_api, clinician_token=clinician_token)

    report_ids = {item["report"]["id"] for item in payload}
    assert report_ids == {report_1.id, report_2.id}


def test_unshared_reports_are_not_visible_to_clinician(consent_api: ConsentApiHarness) -> None:
    patient_shared_email = "patient-shared@example.com"
    patient_private_email = "patient-private@example.com"
    clinician_email = "clinician-unshared@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=patient_shared_email, role="patient")
        seed_user(session, email=patient_private_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        shared_report = seed_report(
            session,
            subject_email=patient_shared_email,
            created_by_email=patient_shared_email,
        )
        unshared_report = seed_report(
            session,
            subject_email=patient_private_email,
            created_by_email=patient_private_email,
        )

    shared_patient_token = login(consent_api, email=patient_shared_email)
    _share_report(
        consent_api,
        report_id=shared_report.id,
        patient_token=shared_patient_token,
        clinician_email=clinician_email,
    )

    clinician_token = login(consent_api, email=clinician_email)
    payload = _list_shared_reports(consent_api, clinician_token=clinician_token)

    report_ids = {item["report"]["id"] for item in payload}
    assert report_ids == {shared_report.id}
    assert unshared_report.id not in report_ids


def test_expired_and_revoked_shares_are_not_listed(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-expired@example.com"
    clinician_email = "clinician-expired@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        active_report = seed_report(
            session,
            subject_email=patient_email,
            created_by_email=patient_email,
        )
        expired_report = seed_report(
            session,
            subject_email=patient_email,
            created_by_email=patient_email,
        )
        revoked_report = seed_report(
            session,
            subject_email=patient_email,
            created_by_email=patient_email,
        )

    patient_token = login(consent_api, email=patient_email)
    active_share = _share_report(
        consent_api,
        report_id=active_report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
    )
    expired_share = _share_report(
        consent_api,
        report_id=expired_report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
    )
    _share_report(
        consent_api,
        report_id=revoked_report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
    )

    revoke_response = consent_api.client.post(
        f"/api/v1/reports/{revoked_report.id}/share/revoke",
        json={"clinician_email": clinician_email},
        headers=auth_headers(patient_token),
    )
    assert revoke_response.status_code == 204, revoke_response.text

    with consent_api.session_factory() as session:
        expired_row = session.scalar(
            select(ConsentShare).where(ConsentShare.id == expired_share["id"])
        )
        assert expired_row is not None
        expired_row.expires_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()

    clinician_token = login(consent_api, email=clinician_email)
    payload = _list_shared_reports(consent_api, clinician_token=clinician_token)

    report_ids = {item["report"]["id"] for item in payload}
    assert active_report.id in report_ids
    assert expired_report.id not in report_ids
    assert revoked_report.id not in report_ids
    assert active_share["id"] in {item["share_id"] for item in payload}


def test_shared_reports_include_patient_profile_fields(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-profile@example.com"
    clinician_email = "clinician-profile@example.com"

    with consent_api.session_factory() as session:
        patient = seed_user(
            session,
            email=patient_email,
            role="patient",
            display_name="Jane Patient",
        )
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient_email,
            created_by_email=patient_email,
        )

    patient_token = login(consent_api, email=patient_email)
    _share_report(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
    )

    clinician_token = login(consent_api, email=clinician_email)
    payload = _list_shared_reports(consent_api, clinician_token=clinician_token)

    item = next(entry for entry in payload if entry["report"]["id"] == report.id)
    patient_payload = item["patient"]
    assert patient_payload["id"] == patient.id
    assert patient_payload["email"] == patient_email
    assert patient_payload["display_name"] == "Jane Patient"
    assert "preferred_language" in patient_payload


def test_patient_scope_share_lists_all_patient_reports(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-scope-list@example.com"
    clinician_email = "clinician-scope-list@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report_a = seed_report(
            session,
            subject_email=patient_email,
            created_by_email=patient_email,
        )
        report_b = seed_report(
            session,
            subject_email=patient_email,
            created_by_email=patient_email,
        )

    patient_token = login(consent_api, email=patient_email)
    _share_report(
        consent_api,
        report_id=report_a.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        scope="patient",
    )

    clinician_token = login(consent_api, email=clinician_email)
    payload = _list_shared_reports(consent_api, clinician_token=clinician_token)

    ids = {item["report"]["id"] for item in payload}
    assert ids == {report_a.id, report_b.id}


def test_overlap_patient_and_report_scope_is_deduped_by_report(
    consent_api: ConsentApiHarness,
) -> None:
    patient_email = "patient-overlap@example.com"
    clinician_email = "clinician-overlap@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report_a = seed_report(
            session,
            subject_email=patient_email,
            created_by_email=patient_email,
        )
        report_b = seed_report(
            session,
            subject_email=patient_email,
            created_by_email=patient_email,
        )

    patient_token = login(consent_api, email=patient_email)
    _share_report(
        consent_api,
        report_id=report_a.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        scope="patient",
    )
    _share_report(
        consent_api,
        report_id=report_a.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        scope="report",
    )

    clinician_token = login(consent_api, email=clinician_email)
    payload = _list_shared_reports(consent_api, clinician_token=clinician_token)

    ids = [item["report"]["id"] for item in payload]
    assert set(ids) == {report_a.id, report_b.id}
    assert ids.count(report_a.id) == 1
    report_a_row = next(item for item in payload if item["report"]["id"] == report_a.id)
    assert report_a_row["scope"] == "report"


def test_shared_report_detail_preserves_doctor_summary_flag(consent_api: ConsentApiHarness) -> None:
    patient_email = "patient-doctor-summary-detail@example.com"
    clinician_email = "clinician-doctor-summary-detail@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=patient_email, role="patient")
        seed_user(session, email=clinician_email, role="clinician")
        report = seed_report(
            session,
            subject_email=patient_email,
            created_by_email=patient_email,
        )

    patient_token = login(consent_api, email=patient_email)
    _share_report(
        consent_api,
        report_id=report.id,
        patient_token=patient_token,
        clinician_email=clinician_email,
        scope="patient",
        access_level="comment",
        include_doctor_summary=True,
    )

    clinician_token = login(consent_api, email=clinician_email)
    detail = _get_shared_report_detail(
        consent_api,
        clinician_token=clinician_token,
        report_id=report.id,
    )

    assert detail["share"]["scope"] == "patient"
    assert detail["share"]["access_level"] == "comment"
    assert detail["share"]["include_doctor_summary"] is True
