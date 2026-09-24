from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    AuthSession,
    ConsentScope,
    ConsentShare,
    Role,
    UserRole,
)
from app.db.seed import seed_core_roles
from tests.factories import PersistenceFactory


def test_seed_core_roles_is_idempotent(db_session: Session):
    seed_core_roles(db_session)
    seed_core_roles(db_session)

    role_names = db_session.scalars(select(Role.name).order_by(Role.name)).all()

    assert role_names == ["caregiver", "clinician", "patient"]


def test_factory_builds_privacy_first_patient_graph(
    db_session: Session,
    persistence_factory: PersistenceFactory,
):
    factory = persistence_factory
    patient = factory.create_user(display_name="Patient One", roles=["patient"])
    caregiver = factory.create_user(display_name="Caregiver One", roles=["caregiver"])
    clinician = factory.create_user(display_name="Clinician One", roles=["clinician"])

    auth_session = factory.create_auth_session(user=patient)
    report = factory.create_report(subject=patient, created_by=caregiver)
    finding = factory.create_finding(
        report=report,
        patient=patient,
        biomarker_key="hemoglobin",
        display_name="Hemoglobin",
        value_numeric=13.2,
        unit="g/dL",
        reference_low=12.0,
        reference_high=15.5,
    )
    share = factory.create_share(patient=patient, grantee=clinician, report=report)
    thread = factory.create_thread(
        patient=patient,
        created_by=caregiver,
        report=report,
        participants=[patient, clinician],
    )
    message = factory.create_message(
        thread=thread, author=clinician, body="Please repeat CBC in 3 months."
    )
    notification = factory.create_notification(user=patient, thread=thread, report=report)
    template = factory.create_clinician_template(author=clinician)
    audit_event = factory.create_audit_event(
        actor=caregiver,
        subject=patient,
        resource_type="consent_share",
        resource_id=share.id,
        action="share.granted",
    )

    db_session.commit()

    assert auth_session.user == patient
    assert report.subject_user == patient
    assert report.created_by_user == caregiver
    assert report.sharing_mode.value == "private"
    assert finding.report == report
    assert finding.biomarker_observation.patient_user == patient
    assert share.scope is ConsentScope.REPORT
    assert share.access_level.value == "read"
    assert {participant.user_id for participant in thread.participants} == {
        patient.id,
        caregiver.id,
        clinician.id,
    }
    assert message.thread == thread
    assert notification.user == patient
    assert notification.read_at is None
    assert template.payload["sections"][0]["kind"] == "summary"
    assert audit_event.actor_user == caregiver


def test_duplicate_role_assignment_and_refresh_hash_violate_constraints(db_session: Session):
    factory = PersistenceFactory(db_session)
    patient = factory.create_user(roles=["patient"])
    factory.create_auth_session(user=patient, refresh_token_hash="refresh-token-hash")
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.execute(
            insert(UserRole).values(
                user_id=patient.id,
                role_id=patient.role_assignments[0].role_id,
            )
        )
        db_session.flush()

    db_session.rollback()

    duplicate_session = AuthSession(
        user_id=patient.id,
        refresh_token_hash="refresh-token-hash",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(duplicate_session)

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_report_scoped_share_requires_report_id(db_session: Session):
    factory = PersistenceFactory(db_session)
    patient = factory.create_user(roles=["patient"])
    clinician = factory.create_user(roles=["clinician"])

    db_session.add(
        ConsentShare(
            subject_user=patient,
            grantee_user=clinician,
            scope=ConsentScope.REPORT,
            granted_by_user=patient,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()
