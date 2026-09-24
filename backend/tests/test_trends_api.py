from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import User
from app.main import create_app
from tests.factories import PersistenceFactory


@dataclass
class TrendsApiHarness:
    client: TestClient
    session_factory: sessionmaker


@pytest.fixture()
def trends_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TrendsApiHarness:
    db_path = tmp_path / "trends-api.sqlite3"
    sync_database_url = f"sqlite:///{db_path.resolve().as_posix()}"
    async_database_url = f"sqlite+aiosqlite:///{db_path.resolve().as_posix()}"

    monkeypatch.setenv("DATABASE_URL", async_database_url)
    monkeypatch.setenv("AUTH_SECRET_KEY", "reportx-test-auth-secret-key-with-32-bytes")
    monkeypatch.setenv("ACCESS_TOKEN_TTL_MINUTES", "15")
    monkeypatch.setenv("REFRESH_SESSION_TTL_DAYS", "30")

    engine = create_engine(sync_database_url, future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    Base.metadata.create_all(engine)

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

        async def wait(self):
            return None

        def kill(self):
            return None

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    app = create_app()
    with TestClient(app) as client:
        yield TrendsApiHarness(client=client, session_factory=session_factory)

    Base.metadata.drop_all(engine)
    engine.dispose()


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def register_user(
    trends_api: TrendsApiHarness,
    *,
    email: str,
    password: str,
    role: str,
    display_name: str,
) -> dict:
    response = trends_api.client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": password,
            "role": role,
            "display_name": display_name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def login_user(trends_api: TrendsApiHarness, *, email: str, password: str) -> dict:
    response = trends_api.client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_report_with_findings(
    session_factory: sessionmaker,
    *,
    subject_email: str,
    created_by_email: str,
    observed_at: datetime,
    hemoglobin_value: float,
    hemoglobin_flag: str,
    hemoglobin_unit: str = "g/dL",
    include_singleton_biomarker: bool = False,
    include_report_date_numeric_finding: bool = False,
) -> str:
    with session_factory() as session:
        factory = PersistenceFactory(session)
        subject = session.scalar(select(User).where(User.email == subject_email))
        created_by = session.scalar(select(User).where(User.email == created_by_email))
        assert subject is not None
        assert created_by is not None

        report = factory.create_report(
            subject=subject,
            created_by=created_by,
            observed_at=observed_at,
        )
        factory.create_finding(
            report=report,
            patient=subject,
            biomarker_key="hemoglobin",
            display_name="Hemoglobin",
            value_numeric=hemoglobin_value,
            unit=hemoglobin_unit,
            flag=hemoglobin_flag,
            reference_low=12.0,
            reference_high=15.5,
        )
        if include_singleton_biomarker:
            factory.create_finding(
                report=report,
                patient=subject,
                biomarker_key="single-marker",
                display_name="Single Marker",
                value_numeric=3.0,
                unit="mg/L",
                flag="normal",
                reference_low=1.0,
                reference_high=5.0,
            )

        if include_report_date_numeric_finding:
            factory.create_finding(
                report=report,
                patient=subject,
                biomarker_key="report-date",
                display_name="Report Date",
                value_numeric=float(observed_at.strftime("%Y%m%d")),
                unit=None,
                flag="normal",
            )

        session.commit()
        return report.id


def test_patient_trends_return_series_and_skip_singletons(trends_api: TrendsApiHarness):
    """Verify that clinicians with full_report scope can access trends, which include multiple series points and exclude singleton biomarkers."""
    register_user(
        trends_api,
        email="patient.trends@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Trends",
    )
    register_user(
        trends_api,
        email="clinician.trends@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician Trends",
    )

    older_report_id = create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.trends@example.com",
        created_by_email="patient.trends@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=14),
        hemoglobin_value=17.2,
        hemoglobin_flag="high",
    )
    newer_report_id = create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.trends@example.com",
        created_by_email="patient.trends@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=2),
        hemoglobin_value=14.0,
        hemoglobin_flag="normal",
        include_singleton_biomarker=True,
    )

    patient_login = login_user(
        trends_api, email="patient.trends@example.com", password="Password123!"
    )
    share_response = trends_api.client.post(
        f"/api/v1/reports/{newer_report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.trends@example.com",
            "scope": "patient",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            "view_scope": "full_report",
        },
    )
    assert share_response.status_code == 201, share_response.text

    clinician_login = login_user(
        trends_api, email="clinician.trends@example.com", password="Password123!"
    )
    response = trends_api.client.get(
        f"/api/v1/reports/{newer_report_id}/trends",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert response.status_code == 200, response.text

    payload = response.json()
    assert payload["report_id"] == newer_report_id
    assert payload["trends"]
    assert len(payload["trends"]) == 1

    hemoglobin = payload["trends"][0]
    assert hemoglobin["biomarker_key"] == "hemoglobin"
    assert hemoglobin["direction"] == "improving"
    assert "improving" in hemoglobin["trend_note"].lower()
    assert len(hemoglobin["sparkline"]) == 2
    assert hemoglobin["sparkline"][0]["report_id"] == older_report_id
    assert hemoglobin["sparkline"][1]["report_id"] == newer_report_id


def test_single_report_returns_empty_trends(trends_api: TrendsApiHarness):
    """Verify that a clinician accessing a report with only one data point gets empty trends (no trend without multiple points)."""
    register_user(
        trends_api,
        email="patient.single@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Single",
    )
    register_user(
        trends_api,
        email="clinician.single@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician Single",
    )

    report_id = create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.single@example.com",
        created_by_email="patient.single@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=3),
        hemoglobin_value=13.4,
        hemoglobin_flag="normal",
    )

    patient_login = login_user(
        trends_api, email="patient.single@example.com", password="Password123!"
    )
    share_response = trends_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.single@example.com",
            "scope": "patient",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            "view_scope": "full_report",
        },
    )
    assert share_response.status_code == 201, share_response.text

    clinician_login = login_user(
        trends_api, email="clinician.single@example.com", password="Password123!"
    )
    response = trends_api.client.get(
        f"/api/v1/reports/{report_id}/trends",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["trends"] == []


def test_trends_exclude_date_like_numeric_findings(trends_api: TrendsApiHarness):
    """Verify that clinician-accessible trends exclude date-like numeric findings (e.g., report date as numeric value)."""
    register_user(
        trends_api,
        email="patient.numericfilter@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Numeric Filter",
    )
    register_user(
        trends_api,
        email="clinician.numericfilter@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician Numeric Filter",
    )

    create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.numericfilter@example.com",
        created_by_email="patient.numericfilter@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=10),
        hemoglobin_value=15.8,
        hemoglobin_flag="high",
        include_report_date_numeric_finding=True,
    )
    newer_report_id = create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.numericfilter@example.com",
        created_by_email="patient.numericfilter@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=1),
        hemoglobin_value=14.3,
        hemoglobin_flag="normal",
        include_report_date_numeric_finding=True,
    )

    patient_login = login_user(
        trends_api, email="patient.numericfilter@example.com", password="Password123!"
    )
    share_response = trends_api.client.post(
        f"/api/v1/reports/{newer_report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.numericfilter@example.com",
            "scope": "patient",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            "view_scope": "full_report",
        },
    )
    assert share_response.status_code == 201, share_response.text

    clinician_login = login_user(
        trends_api, email="clinician.numericfilter@example.com", password="Password123!"
    )
    response = trends_api.client.get(
        f"/api/v1/reports/{newer_report_id}/trends",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert response.status_code == 200, response.text

    trend_names = {item["display_name"] for item in response.json()["trends"]}
    assert "Hemoglobin" in trend_names
    assert "Report Date" not in trend_names


def test_trends_skip_mixed_unit_series(trends_api: TrendsApiHarness):
    register_user(
        trends_api,
        email="patient.mixedunits@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Mixed Units",
    )
    register_user(
        trends_api,
        email="clinician.mixedunits@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician Mixed Units",
    )

    create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.mixedunits@example.com",
        created_by_email="patient.mixedunits@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=12),
        hemoglobin_value=14.2,
        hemoglobin_flag="normal",
        hemoglobin_unit="g/dL",
    )
    newer_report_id = create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.mixedunits@example.com",
        created_by_email="patient.mixedunits@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=1),
        hemoglobin_value=8.8,
        hemoglobin_flag="normal",
        hemoglobin_unit="mmol/L",
    )

    patient_login = login_user(
        trends_api, email="patient.mixedunits@example.com", password="Password123!"
    )
    share_response = trends_api.client.post(
        f"/api/v1/reports/{newer_report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.mixedunits@example.com",
            "scope": "patient",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            "view_scope": "full_report",
        },
    )
    assert share_response.status_code == 201, share_response.text

    clinician_login = login_user(
        trends_api, email="clinician.mixedunits@example.com", password="Password123!"
    )
    response = trends_api.client.get(
        f"/api/v1/reports/{newer_report_id}/trends",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["trends"] == []


def test_clinician_trends_require_full_report_access(trends_api: TrendsApiHarness):
    register_user(
        trends_api,
        email="patient.access@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Access",
    )
    register_user(
        trends_api,
        email="clinician.access@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician Access",
    )

    report_id = create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.access@example.com",
        created_by_email="patient.access@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=7),
        hemoglobin_value=16.1,
        hemoglobin_flag="high",
    )
    create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.access@example.com",
        created_by_email="patient.access@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=1),
        hemoglobin_value=14.6,
        hemoglobin_flag="normal",
    )

    patient_login = login_user(
        trends_api, email="patient.access@example.com", password="Password123!"
    )
    report_scope_share = trends_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.access@example.com",
            "scope": "report",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            "view_scope": "full_report",
        },
    )
    assert report_scope_share.status_code == 201, report_scope_share.text

    clinician_login = login_user(
        trends_api, email="clinician.access@example.com", password="Password123!"
    )
    denied = trends_api.client.get(
        f"/api/v1/reports/{report_id}/trends",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert denied.status_code == 403
    assert "full-report access" in denied.json()["detail"].lower()

    patient_scope_share = trends_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.access@example.com",
            "scope": "patient",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            "view_scope": "full_report",
        },
    )
    assert patient_scope_share.status_code == 201, patient_scope_share.text

    allowed = trends_api.client.get(
        f"/api/v1/reports/{report_id}/trends",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["trends"]


def test_patient_cannot_access_trends_endpoint(trends_api: TrendsApiHarness):
    """Patients should not be able to access the trends endpoint, even on their own reports."""
    register_user(
        trends_api,
        email="patient.notrendsaccess@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient No Trends",
    )

    report_id = create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.notrendsaccess@example.com",
        created_by_email="patient.notrendsaccess@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=7),
        hemoglobin_value=16.1,
        hemoglobin_flag="high",
    )
    create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.notrendsaccess@example.com",
        created_by_email="patient.notrendsaccess@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=1),
        hemoglobin_value=14.6,
        hemoglobin_flag="normal",
    )

    patient_login = login_user(
        trends_api, email="patient.notrendsaccess@example.com", password="Password123!"
    )
    response = trends_api.client.get(
        f"/api/v1/reports/{report_id}/trends",
        headers=auth_headers(patient_login["access_token"]),
    )
    assert response.status_code == 403
    assert (
        "clinician" in response.json()["detail"].lower()
        or "forbidden" in response.json()["detail"].lower()
    )


def test_caregiver_cannot_access_trends_endpoint(trends_api: TrendsApiHarness):
    """Caregivers should not be able to access the trends endpoint."""
    register_user(
        trends_api,
        email="patient.caregiver@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Caregiver",
    )
    register_user(
        trends_api,
        email="caregiver.notrendsaccess@example.com",
        password="Password123!",
        role="caregiver",
        display_name="Caregiver No Trends",
    )

    report_id = create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.caregiver@example.com",
        created_by_email="patient.caregiver@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=7),
        hemoglobin_value=16.1,
        hemoglobin_flag="high",
    )

    caregiver_login = login_user(
        trends_api, email="caregiver.notrendsaccess@example.com", password="Password123!"
    )
    response = trends_api.client.get(
        f"/api/v1/reports/{report_id}/trends",
        headers=auth_headers(caregiver_login["access_token"]),
    )
    assert response.status_code == 403


def test_clinician_summary_only_scope_denied_trends(trends_api: TrendsApiHarness):
    """Clinicians with summary_only scope should not access trends."""
    register_user(
        trends_api,
        email="patient.scopetest@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Scope Test",
    )
    register_user(
        trends_api,
        email="clinician.scopetest@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician Scope Test",
    )

    report_id = create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.scopetest@example.com",
        created_by_email="patient.scopetest@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=7),
        hemoglobin_value=16.1,
        hemoglobin_flag="high",
    )
    create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.scopetest@example.com",
        created_by_email="patient.scopetest@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=1),
        hemoglobin_value=14.6,
        hemoglobin_flag="normal",
    )

    patient_login = login_user(
        trends_api, email="patient.scopetest@example.com", password="Password123!"
    )
    share_response = trends_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.scopetest@example.com",
            "scope": "patient",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            "view_scope": "summary_only",
        },
    )
    assert share_response.status_code == 201, share_response.text

    clinician_login = login_user(
        trends_api, email="clinician.scopetest@example.com", password="Password123!"
    )
    response = trends_api.client.get(
        f"/api/v1/reports/{report_id}/trends",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert response.status_code == 403
    assert (
        "summary_only" in response.json()["detail"].lower()
        or "scope" in response.json()["detail"].lower()
    )


def test_clinician_full_report_scope_allowed_trends(trends_api: TrendsApiHarness):
    """Clinicians with full_report scope should access trends."""
    register_user(
        trends_api,
        email="patient.fullscope@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Full Scope",
    )
    register_user(
        trends_api,
        email="clinician.fullscope@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician Full Scope",
    )

    report_id = create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.fullscope@example.com",
        created_by_email="patient.fullscope@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=7),
        hemoglobin_value=16.1,
        hemoglobin_flag="high",
    )
    create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.fullscope@example.com",
        created_by_email="patient.fullscope@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=1),
        hemoglobin_value=14.6,
        hemoglobin_flag="normal",
    )

    patient_login = login_user(
        trends_api, email="patient.fullscope@example.com", password="Password123!"
    )
    share_response = trends_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.fullscope@example.com",
            "scope": "patient",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            "view_scope": "full_report",
        },
    )
    assert share_response.status_code == 201, share_response.text

    clinician_login = login_user(
        trends_api, email="clinician.fullscope@example.com", password="Password123!"
    )
    response = trends_api.client.get(
        f"/api/v1/reports/{report_id}/trends",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["trends"]


def test_clinician_full_report_with_threads_scope_allowed_trends(trends_api: TrendsApiHarness):
    """Clinicians with full_report_with_threads scope should access trends."""
    register_user(
        trends_api,
        email="patient.threadsscope@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Threads Scope",
    )
    register_user(
        trends_api,
        email="clinician.threadsscope@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician Threads Scope",
    )

    report_id = create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.threadsscope@example.com",
        created_by_email="patient.threadsscope@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=7),
        hemoglobin_value=16.1,
        hemoglobin_flag="high",
    )
    create_report_with_findings(
        trends_api.session_factory,
        subject_email="patient.threadsscope@example.com",
        created_by_email="patient.threadsscope@example.com",
        observed_at=datetime.now(UTC) - timedelta(days=1),
        hemoglobin_value=14.6,
        hemoglobin_flag="normal",
    )

    patient_login = login_user(
        trends_api, email="patient.threadsscope@example.com", password="Password123!"
    )
    share_response = trends_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.threadsscope@example.com",
            "scope": "patient",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
            "view_scope": "full_report_with_threads",
        },
    )
    assert share_response.status_code == 201, share_response.text

    clinician_login = login_user(
        trends_api, email="clinician.threadsscope@example.com", password="Password123!"
    )
    response = trends_api.client.get(
        f"/api/v1/reports/{report_id}/trends",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["trends"]
