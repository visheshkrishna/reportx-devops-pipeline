from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.db.models import Report
from tests.support.consent_api import ConsentApiHarness, auth_headers, login, seed_user


def test_create_report_persists_observed_at_and_returns_created_at(
    consent_api: ConsentApiHarness,
) -> None:
    email = "patient-create-report@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=email, role="patient")

    token = login(consent_api, email=email)
    observed_at = datetime(2026, 4, 15, 9, 30, tzinfo=UTC)
    response = consent_api.client.post(
        "/api/v1/reports",
        headers=auth_headers(token),
        json={
            "title": "Report from Parse",
            "source_kind": "text",
            "observed_at": observed_at.isoformat(),
            "findings": [
                {
                    "test_name": "Hemoglobin",
                    "value_numeric": 13.5,
                    "unit": "g/dL",
                    "reference_range": "11.0-15.0",
                    "flag": "normal",
                }
            ],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body.get("id"), str)
    assert body.get("created_at")
    assert body.get("observed_at")

    with consent_api.session_factory() as session:
        report = session.scalar(select(Report).where(Report.id == body["id"]))

    assert report is not None
    assert report.observed_at.replace(tzinfo=UTC) == observed_at


def test_create_report_defaults_observed_at_when_missing(consent_api: ConsentApiHarness) -> None:
    email = "patient-create-default-observed@example.com"

    with consent_api.session_factory() as session:
        seed_user(session, email=email, role="patient")

    token = login(consent_api, email=email)
    response = consent_api.client.post(
        "/api/v1/reports",
        headers=auth_headers(token),
        json={
            "title": "No explicit observed date",
            "source_kind": "text",
            "findings": [
                {
                    "test_name": "Glucose",
                    "value_numeric": 92,
                    "unit": "mg/dL",
                    "reference_range": "70-99",
                    "flag": "normal",
                }
            ],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body.get("observed_at")
    assert body.get("created_at")
