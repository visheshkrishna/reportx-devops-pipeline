from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    AuditEvent,
    AuthSession,
    BiomarkerObservation,
    ClinicianResponseTemplate,
    ConsentAccessLevel,
    ConsentScope,
    ConsentShare,
    ConversationThread,
    MessageKind,
    Notification,
    NotificationKind,
    Report,
    ReportFinding,
    ReportSourceKind,
    Role,
    ThreadMessage,
    ThreadParticipant,
    User,
)
from app.db.seed import seed_core_roles


class PersistenceFactory:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._sequence = count(1)

    def _next_email(self, prefix: str) -> str:
        return f"{prefix}-{next(self._sequence)}@example.com"

    def create_user(
        self,
        *,
        email: str | None = None,
        display_name: str | None = None,
        password_hash: str = "hashed-password",
        roles: list[str] | None = None,
    ) -> User:
        seed_core_roles(self.session)
        user = User(
            email=email or self._next_email("user"),
            display_name=display_name or "Test User",
            password_hash=password_hash,
        )
        self.session.add(user)
        self.session.flush()
        if roles:
            available_roles = {role.name: role for role in self.session.query(Role).all()}
            for role_name in roles:
                user.assign_role(available_roles[role_name])
        self.session.flush()
        return user

    def create_auth_session(
        self,
        *,
        user: User,
        refresh_token_hash: str | None = None,
        expires_in_days: int = 30,
    ) -> AuthSession:
        auth_session = AuthSession(
            user=user,
            refresh_token_hash=refresh_token_hash or f"refresh-{next(self._sequence)}",
            expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
        )
        self.session.add(auth_session)
        self.session.flush()
        return auth_session

    def create_report(
        self,
        *,
        subject: User,
        created_by: User,
        source_kind: ReportSourceKind = ReportSourceKind.PDF,
        observed_at: datetime | None = None,
    ) -> Report:
        report = Report(
            subject_user=subject,
            created_by_user=created_by,
            source_kind=source_kind,
            observed_at=observed_at or datetime.now(UTC),
            title="CBC Panel",
        )
        self.session.add(report)
        self.session.flush()
        return report

    def create_finding(
        self,
        *,
        report: Report,
        patient: User,
        biomarker_key: str,
        display_name: str,
        value_numeric: float | None = None,
        value_text: str | None = None,
        unit: str | None = None,
        flag: str = "normal",
        reference_low: float | None = None,
        reference_high: float | None = None,
    ) -> ReportFinding:
        finding = ReportFinding(
            report=report,
            biomarker_key=biomarker_key,
            display_name=display_name,
            value_numeric=value_numeric,
            value_text=value_text,
            unit=unit,
            flag=flag,
            reference_low=reference_low,
            reference_high=reference_high,
            reference_range_text=(
                f"{reference_low}-{reference_high}"
                if reference_low is not None and reference_high is not None
                else None
            ),
        )
        self.session.add(finding)
        self.session.flush()

        observation = BiomarkerObservation(
            patient_user=patient,
            report=report,
            finding=finding,
            biomarker_key=biomarker_key,
            observed_at=report.observed_at,
            value_numeric=value_numeric,
            value_text=value_text,
            unit=unit,
            flag=flag,
        )
        self.session.add(observation)
        self.session.flush()
        return finding

    def create_share(
        self,
        *,
        patient: User,
        grantee: User,
        report: Report,
        access_level: ConsentAccessLevel = ConsentAccessLevel.READ,
    ) -> ConsentShare:
        share = ConsentShare(
            subject_user=patient,
            grantee_user=grantee,
            report=report,
            scope=ConsentScope.REPORT,
            access_level=access_level,
            granted_by_user=patient,
            expires_at=datetime.now(UTC) + timedelta(days=14),
        )
        self.session.add(share)
        self.session.flush()
        return share

    def create_thread(
        self,
        *,
        patient: User,
        created_by: User,
        report: Report,
        participants: list[User],
    ) -> ConversationThread:
        thread = ConversationThread(
            subject_user=patient,
            created_by_user=created_by,
            report=report,
            title="Lab follow-up",
        )
        self.session.add(thread)
        self.session.flush()
        unique_participants = {
            participant.id: participant for participant in [created_by, *participants]
        }
        for participant in unique_participants.values():
            self.session.add(ThreadParticipant(thread=thread, user=participant))
        self.session.flush()
        return thread

    def create_message(
        self,
        *,
        thread: ConversationThread,
        author: User,
        body: str,
        kind: MessageKind = MessageKind.TEXT,
    ) -> ThreadMessage:
        message = ThreadMessage(
            thread=thread,
            author_user=author,
            body=body,
            kind=kind,
        )
        self.session.add(message)
        self.session.flush()
        return message

    def create_notification(
        self,
        *,
        user: User,
        thread: ConversationThread,
        report: Report,
        kind: NotificationKind = NotificationKind.THREAD_REPLY,
        payload: dict[str, Any] | None = None,
    ) -> Notification:
        notification = Notification(
            user=user,
            thread=thread,
            report=report,
            kind=kind,
            title="New activity",
            resource_type="thread",
            resource_id=thread.id,
            payload=payload or {"thread_id": thread.id},
        )
        self.session.add(notification)
        self.session.flush()
        return notification

    def create_clinician_template(
        self,
        *,
        author: User,
        title: str = "Follow-up card",
        payload: dict[str, Any] | None = None,
    ) -> ClinicianResponseTemplate:
        template = ClinicianResponseTemplate(
            author_user=author,
            title=title,
            slug=f"card-{next(self._sequence)}",
            payload=payload or {"sections": [{"kind": "summary", "text": "Follow up soon."}]},
        )
        self.session.add(template)
        self.session.flush()
        return template

    def create_audit_event(
        self,
        *,
        actor: User,
        subject: User,
        resource_type: str,
        resource_id: str,
        action: str,
    ) -> AuditEvent:
        event = AuditEvent(
            actor_user=actor,
            subject_user=subject,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            context={"source": "tests"},
        )
        self.session.add(event)
        self.session.flush()
        return event
