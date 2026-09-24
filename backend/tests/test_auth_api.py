from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from passlib.context import CryptContext
from sqlalchemy import create_engine, select
from sqlalchemy.orm import selectinload, sessionmaker

import app.main as main_mod
from app.db.models import AuditEvent, AuthSession, ConsentShare, Report, User, UserRole
from app.main import create_app
from app.services.auth import hash_password, verify_password
from tests.factories import PersistenceFactory


@dataclass
class AuthApiHarness:
    client: TestClient
    session_factory: sessionmaker


@pytest.fixture()
def auth_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthApiHarness:
    db_path = tmp_path / "auth-api.sqlite3"
    sync_database_url = f"sqlite:///{db_path.resolve().as_posix()}"
    async_database_url = f"sqlite+aiosqlite:///{db_path.resolve().as_posix()}"

    monkeypatch.setenv("DATABASE_URL", async_database_url)
    monkeypatch.setenv("AUTH_SECRET_KEY", "reportx-test-auth-secret-key-with-32-bytes")
    monkeypatch.setenv("ACCESS_TOKEN_TTL_MINUTES", "15")
    monkeypatch.setenv("REFRESH_SESSION_TTL_DAYS", "30")

    engine = create_engine(sync_database_url, future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    app = create_app()
    with TestClient(app) as client:
        yield AuthApiHarness(client=client, session_factory=session_factory)

    engine.dispose()


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def register_user(
    auth_api: AuthApiHarness,
    *,
    email: str,
    password: str,
    role: str,
    display_name: str,
) -> dict:
    response = auth_api.client.post(
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


def login_user(auth_api: AuthApiHarness, *, email: str, password: str) -> dict:
    response = auth_api.client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def lookup_user(session_factory: sessionmaker, email: str) -> User:
    with session_factory() as session:
        user = session.scalar(
            select(User)
            .options(selectinload(User.role_assignments).selectinload(UserRole.role))
            .options(selectinload(User.roles))
            .where(User.email == email)
        )
        assert user is not None
        return user


def create_report_for_users(
    session_factory: sessionmaker,
    *,
    subject_email: str,
    created_by_email: str,
) -> str:
    with session_factory() as session:
        factory = PersistenceFactory(session)
        subject = session.scalar(select(User).where(User.email == subject_email))
        created_by = session.scalar(select(User).where(User.email == created_by_email))
        assert subject is not None
        assert created_by is not None
        report = factory.create_report(subject=subject, created_by=created_by)
        factory.create_finding(
            report=report,
            patient=subject,
            biomarker_key="hemoglobin",
            display_name="Hemoglobin",
            value_numeric=13.2,
            unit="g/dL",
            reference_low=12.0,
            reference_high=15.5,
        )
        session.commit()
        return report.id


def create_report_share(
    session_factory: sessionmaker,
    *,
    patient_email: str,
    grantee_email: str,
    report_id: str,
) -> None:
    with session_factory() as session:
        factory = PersistenceFactory(session)
        patient = session.scalar(select(User).where(User.email == patient_email))
        grantee = session.scalar(select(User).where(User.email == grantee_email))
        report = session.get(Report, report_id)
        assert patient is not None
        assert grantee is not None
        assert report is not None
        factory.create_share(patient=patient, grantee=grantee, report=report)
        session.commit()


def expire_latest_session(session_factory: sessionmaker, *, email: str) -> None:
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        auth_session = session.scalars(
            select(AuthSession)
            .where(AuthSession.user_id == user.id)
            .order_by(AuthSession.created_at.desc())
        ).first()
        assert auth_session is not None
        auth_session.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()


def expire_share_for_grantee(
    session_factory: sessionmaker,
    *,
    patient_email: str,
    grantee_email: str,
    report_id: str,
) -> None:
    with session_factory() as session:
        patient = session.scalar(select(User).where(User.email == patient_email))
        grantee = session.scalar(select(User).where(User.email == grantee_email))
        assert patient is not None
        assert grantee is not None
        share = session.scalar(
            select(ConsentShare).where(
                ConsentShare.subject_user_id == patient.id,
                ConsentShare.grantee_user_id == grantee.id,
                ConsentShare.report_id == report_id,
                ConsentShare.revoked_at.is_(None),
            )
        )
        assert share is not None
        share.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()


def audit_actions_for_resource(
    session_factory: sessionmaker,
    *,
    resource_type: str,
    resource_id: str,
) -> list[str]:
    with session_factory() as session:
        events = session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.resource_type == resource_type,
                AuditEvent.resource_id == resource_id,
            )
            .order_by(AuditEvent.occurred_at.asc())
        ).all()
        return [event.action for event in events]


@pytest.mark.parametrize(
    ("role", "email"),
    [
        ("patient", "patient.register@example.com"),
        ("caregiver", "caregiver.register@example.com"),
        ("clinician", "clinician.register@example.com"),
    ],
)
def test_register_assigns_selected_role(auth_api: AuthApiHarness, role: str, email: str):
    password = "Password123!"
    payload = register_user(
        auth_api,
        email=email,
        password=password,
        role=role,
        display_name=f"{role.title()} Example",
    )

    assert payload["user"]["email"] == email
    assert payload["user"]["roles"] == [role]

    persisted_user = lookup_user(auth_api.session_factory, email)
    assert persisted_user.password_hash != password
    assert sorted(role.name for role in persisted_user.roles) == [role]


def test_login_success_and_failure(auth_api: AuthApiHarness):
    register_user(
        auth_api,
        email="login.user@example.com",
        password="Password123!",
        role="patient",
        display_name="Login User",
    )

    success = auth_api.client.post(
        "/api/v1/auth/login",
        json={"email": "login.user@example.com", "password": "Password123!"},
    )
    assert success.status_code == 200
    body = success.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["roles"] == ["patient"]

    with auth_api.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "login.user@example.com"))
        assert user is not None
        auth_session = session.scalars(
            select(AuthSession).where(AuthSession.user_id == user.id)
        ).first()
        assert auth_session is not None
        assert auth_session.refresh_token_hash != body["refresh_token"]

    failure = auth_api.client.post(
        "/api/v1/auth/login",
        json={"email": "login.user@example.com", "password": "WrongPassword123!"},
    )
    assert failure.status_code == 401


def test_verify_legacy_bcrypt_hash(auth_api: AuthApiHarness):
    password = "Password123!"
    legacy_ctx = CryptContext(schemes=["bcrypt"])
    try:
        legacy_hash = legacy_ctx.hash(password)
    except ValueError as exc:
        pytest.skip(f"bcrypt backend not fully available in this environment: {exc}")

    # Can verify old bcrypt hashes after switching default to pbkdf2_sha256
    assert verify_password(password, legacy_hash) is True

    # New hashes should still use pbkdf2_sha256 and verify correctly
    new_hash = hash_password(password)
    assert new_hash.startswith("$pbkdf2-sha256$")
    assert verify_password(password, new_hash) is True


def test_refresh_rotates_refresh_token_and_rejects_the_old_one(auth_api: AuthApiHarness):
    register_user(
        auth_api,
        email="rotate.user@example.com",
        password="Password123!",
        role="patient",
        display_name="Rotate User",
    )
    login = login_user(auth_api, email="rotate.user@example.com", password="Password123!")

    refreshed = auth_api.client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": login["refresh_token"]},
    )
    assert refreshed.status_code == 200
    refreshed_body = refreshed.json()
    assert refreshed_body["refresh_token"] != login["refresh_token"]
    assert refreshed_body["access_token"] != login["access_token"]

    stale_refresh = auth_api.client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": login["refresh_token"]},
    )
    assert stale_refresh.status_code == 401


def test_unauthenticated_access_to_protected_endpoints(auth_api: AuthApiHarness):
    register_user(
        auth_api,
        email="owner@example.com",
        password="Password123!",
        role="patient",
        display_name="Owner User",
    )
    report_id = create_report_for_users(
        auth_api.session_factory,
        subject_email="owner@example.com",
        created_by_email="owner@example.com",
    )

    me_response = auth_api.client.get("/api/v1/auth/me")
    assert me_response.status_code == 401

    report_response = auth_api.client.get(f"/api/v1/reports/{report_id}")
    assert report_response.status_code == 401


def test_patient_is_denied_access_to_another_patients_report(auth_api: AuthApiHarness):
    register_user(
        auth_api,
        email="patient.one@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient One",
    )
    register_user(
        auth_api,
        email="patient.two@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Two",
    )
    report_id = create_report_for_users(
        auth_api.session_factory,
        subject_email="patient.one@example.com",
        created_by_email="patient.one@example.com",
    )
    other_patient_login = login_user(
        auth_api,
        email="patient.two@example.com",
        password="Password123!",
    )

    response = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(other_patient_login["access_token"]),
    )
    assert response.status_code == 403


def test_caregiver_is_denied_without_explicit_permission(auth_api: AuthApiHarness):
    register_user(
        auth_api,
        email="patient.owner@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Owner",
    )
    register_user(
        auth_api,
        email="caregiver.user@example.com",
        password="Password123!",
        role="caregiver",
        display_name="Caregiver User",
    )
    report_id = create_report_for_users(
        auth_api.session_factory,
        subject_email="patient.owner@example.com",
        created_by_email="caregiver.user@example.com",
    )
    caregiver_login = login_user(
        auth_api,
        email="caregiver.user@example.com",
        password="Password123!",
    )

    response = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(caregiver_login["access_token"]),
    )
    assert response.status_code == 403


def test_clinician_is_denied_until_a_patient_shares_the_report(auth_api: AuthApiHarness):
    register_user(
        auth_api,
        email="patient.share@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Share",
    )
    register_user(
        auth_api,
        email="clinician.user@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician User",
    )
    report_id = create_report_for_users(
        auth_api.session_factory,
        subject_email="patient.share@example.com",
        created_by_email="patient.share@example.com",
    )
    clinician_login = login_user(
        auth_api,
        email="clinician.user@example.com",
        password="Password123!",
    )

    denied = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert denied.status_code == 403

    create_report_share(
        auth_api.session_factory,
        patient_email="patient.share@example.com",
        grantee_email="clinician.user@example.com",
        report_id=report_id,
    )

    allowed = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert allowed.status_code == 200
    assert allowed.json()["report"]["id"] == report_id


def test_patient_can_share_report_via_api(auth_api: AuthApiHarness):
    from datetime import UTC, datetime, timedelta

    register_user(
        auth_api,
        email="patient.share2@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Share 2",
    )
    register_user(
        auth_api,
        email="clinician.user2@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician User 2",
    )

    report_id = create_report_for_users(
        auth_api.session_factory,
        subject_email="patient.share2@example.com",
        created_by_email="patient.share2@example.com",
    )

    patient_login = login_user(
        auth_api, email="patient.share2@example.com", password="Password123!"
    )
    share_response = auth_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.user2@example.com",
            "scope": "report",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        },
    )
    assert share_response.status_code == 201, share_response.text

    clinician_login = login_user(
        auth_api, email="clinician.user2@example.com", password="Password123!"
    )
    allowed = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert allowed.status_code == 200


def test_patient_can_create_report_with_ordered_findings_via_api(auth_api: AuthApiHarness):
    register_user(
        auth_api,
        email="patient.create@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Create",
    )
    patient_login = login_user(
        auth_api, email="patient.create@example.com", password="Password123!"
    )

    create_response = auth_api.client.post(
        "/api/v1/reports",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "title": "CMP Follow Up",
            "source_kind": "manual",
            "findings": [
                {
                    "test_name": "Glucose",
                    "value_numeric": 92,
                    "unit": "mg/dL",
                    "reference_range": "70-99",
                    "flag": "normal",
                },
                {
                    "test_name": "ALT",
                    "value_numeric": 61,
                    "unit": "U/L",
                    "reference_range": "0-55",
                    "flag": "high",
                },
            ],
        },
    )
    assert create_response.status_code == 201, create_response.text
    created = create_response.json()
    assert created["title"] == "CMP Follow Up"
    assert created["sharing_mode"] == "private"

    detail_response = auth_api.client.get(
        f"/api/v1/reports/{created['id']}",
        headers=auth_headers(patient_login["access_token"]),
    )
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["report"]
    assert detail["title"] == "CMP Follow Up"
    assert [finding["display_name"] for finding in detail["findings"]] == ["Glucose", "ALT"]
    assert detail["findings"][0]["flag"] == "normal"
    assert detail["findings"][1]["flag"] == "high"


def test_revoking_last_share_removes_access_and_resets_sharing_mode(auth_api: AuthApiHarness):
    from datetime import UTC, datetime, timedelta

    register_user(
        auth_api,
        email="patient.revoke@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Revoke",
    )
    register_user(
        auth_api,
        email="clinician.revoke@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician Revoke",
    )

    report_id = create_report_for_users(
        auth_api.session_factory,
        subject_email="patient.revoke@example.com",
        created_by_email="patient.revoke@example.com",
    )

    patient_login = login_user(
        auth_api, email="patient.revoke@example.com", password="Password123!"
    )
    clinician_login = login_user(
        auth_api, email="clinician.revoke@example.com", password="Password123!"
    )

    share_response = auth_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.revoke@example.com",
            "scope": "report",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        },
    )
    assert share_response.status_code == 201, share_response.text

    shared_report = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(patient_login["access_token"]),
    )
    assert shared_report.status_code == 200
    assert shared_report.json()["report"]["sharing_mode"] == "shared"

    revoke_response = auth_api.client.post(
        f"/api/v1/reports/{report_id}/share/revoke",
        headers=auth_headers(patient_login["access_token"]),
        json={"clinician_email": "clinician.revoke@example.com"},
    )
    assert revoke_response.status_code == 204, revoke_response.text

    revoked_access = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert revoked_access.status_code == 403

    patient_view = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(patient_login["access_token"]),
    )
    assert patient_view.status_code == 200
    assert patient_view.json()["report"]["sharing_mode"] == "private"


def test_revoking_patient_scoped_share_removes_access_to_all_patient_reports(
    auth_api: AuthApiHarness,
):
    from datetime import UTC, datetime, timedelta

    register_user(
        auth_api,
        email="patient.scope@example.com",
        password="Password123!",
        role="patient",
        display_name="Patient Scope",
    )
    register_user(
        auth_api,
        email="clinician.scope@example.com",
        password="Password123!",
        role="clinician",
        display_name="Clinician Scope",
    )

    report_id = create_report_for_users(
        auth_api.session_factory,
        subject_email="patient.scope@example.com",
        created_by_email="patient.scope@example.com",
    )
    second_report_id = create_report_for_users(
        auth_api.session_factory,
        subject_email="patient.scope@example.com",
        created_by_email="patient.scope@example.com",
    )

    patient_login = login_user(auth_api, email="patient.scope@example.com", password="Password123!")
    clinician_login = login_user(
        auth_api, email="clinician.scope@example.com", password="Password123!"
    )

    share_response = auth_api.client.post(
        f"/api/v1/reports/{report_id}/share",
        headers=auth_headers(patient_login["access_token"]),
        json={
            "clinician_email": "clinician.scope@example.com",
            "scope": "patient",
            "access_level": "read",
            "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        },
    )
    assert share_response.status_code == 201, share_response.text

    owner_first_view = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(patient_login["access_token"]),
    )
    assert owner_first_view.status_code == 200
    assert owner_first_view.json()["report"]["sharing_mode"] == "shared"

    owner_second_view = auth_api.client.get(
        f"/api/v1/reports/{second_report_id}",
        headers=auth_headers(patient_login["access_token"]),
    )
    assert owner_second_view.status_code == 200
    assert owner_second_view.json()["report"]["sharing_mode"] == "shared"

    first_allowed = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert first_allowed.status_code == 200

    second_allowed = auth_api.client.get(
        f"/api/v1/reports/{second_report_id}",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert second_allowed.status_code == 200

    revoke_response = auth_api.client.post(
        f"/api/v1/reports/{report_id}/share/revoke",
        headers=auth_headers(patient_login["access_token"]),
        json={"clinician_email": "clinician.scope@example.com"},
    )
    assert revoke_response.status_code == 204, revoke_response.text

    first_revoked = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert first_revoked.status_code == 403

    second_revoked = auth_api.client.get(
        f"/api/v1/reports/{second_report_id}",
        headers=auth_headers(clinician_login["access_token"]),
    )
    assert second_revoked.status_code == 403

    owner_first_after_revoke = auth_api.client.get(
        f"/api/v1/reports/{report_id}",
        headers=auth_headers(patient_login["access_token"]),
    )
    assert owner_first_after_revoke.status_code == 200
    assert owner_first_after_revoke.json()["report"]["sharing_mode"] == "private"

    owner_second_after_revoke = auth_api.client.get(
        f"/api/v1/reports/{second_report_id}",
        headers=auth_headers(patient_login["access_token"]),
    )
    assert owner_second_after_revoke.status_code == 200
    assert owner_second_after_revoke.json()["report"]["sharing_mode"] == "private"


def test_logout_revokes_the_session_and_expired_sessions_are_rejected(auth_api: AuthApiHarness):
    register_user(
        auth_api,
        email="logout.user@example.com",
        password="Password123!",
        role="patient",
        display_name="Logout User",
    )
    login = login_user(auth_api, email="logout.user@example.com", password="Password123!")

    me_response = auth_api.client.get(
        "/api/v1/auth/me",
        headers=auth_headers(login["access_token"]),
    )
    assert me_response.status_code == 200

    logout_response = auth_api.client.post(
        "/api/v1/auth/logout",
        headers=auth_headers(login["access_token"]),
    )
    assert logout_response.status_code == 204

    revoked_me = auth_api.client.get(
        "/api/v1/auth/me",
        headers=auth_headers(login["access_token"]),
    )
    assert revoked_me.status_code == 401

    revoked_refresh = auth_api.client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": login["refresh_token"]},
    )
    assert revoked_refresh.status_code == 401


def test_app_lifespan_runs_alembic_with_async_subprocess(auth_api: AuthApiHarness, monkeypatch):
    called = {"count": 0}

    class FakeProcess:
        def __init__(self):
            self.returncode = 0

        async def communicate(self):
            return b"ok", b""

        async def wait(self):
            return None

    async def fake_create_subprocess_exec(*args, **kwargs):
        called["count"] += 1
        assert args[:5] == (sys.executable, "-m", "alembic", "upgrade", "head")
        return FakeProcess()

    monkeypatch.setenv("ALEMBIC_STARTUP_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    app = main_mod.create_app()
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 204

    assert called["count"] == 1

    register_user(
        auth_api,
        email="logout.user@example.com",
        password="Password123!",
        role="patient",
        display_name="Logout User",
    )
    relogin = login_user(auth_api, email="logout.user@example.com", password="Password123!")
    expire_latest_session(auth_api.session_factory, email="logout.user@example.com")

    expired_me = auth_api.client.get(
        "/api/v1/auth/me",
        headers=auth_headers(relogin["access_token"]),
    )
    assert expired_me.status_code == 401

    expired_refresh = auth_api.client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": relogin["refresh_token"]},
    )
    assert expired_refresh.status_code == 401
