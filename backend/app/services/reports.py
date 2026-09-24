from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    AuditEvent,
    ConsentAccessLevel,
    ConsentScope,
    ConsentShare,
    NotificationKind,
    Report,
    ReportFinding,
    ReportSharingMode,
    ReportSourceKind,
    ShareViewScope,
    User,
)
from app.services.notifications import emit_notification


class ReportServiceError(Exception):
    def __init__(self, detail: str, status_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


async def _create_audit_event(
    session: AsyncSession,
    *,
    actor_user_id: str | None,
    subject_user_id: str | None,
    resource_type: str,
    resource_id: str,
    action: str,
    context: dict | None = None,
) -> AuditEvent:
    """Create an audit event record synchronously within transaction."""
    audit = AuditEvent(
        actor_user_id=actor_user_id,
        subject_user_id=subject_user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        context=context or {},
        occurred_at=datetime.now(UTC),
    )
    session.add(audit)
    return audit


@dataclass(frozen=True)
class ReportFindingCreateInput:
    test_name: str
    value_numeric: float | None = None
    value_text: str | None = None
    unit: str | None = None
    reference_range: str | None = None
    flag: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class ReportShareResult:
    share: ConsentShare
    grantee: User


async def list_reports_for_user(session: AsyncSession, *, subject_user_id: str) -> list[Report]:
    reports = await session.execute(
        select(Report)
        .where(Report.subject_user_id == subject_user_id)
        .order_by(Report.created_at.desc())
        .options(selectinload(Report.findings))
    )
    return reports.scalars().all()


async def create_report_for_user(
    session: AsyncSession,
    *,
    subject_user_id: str,
    created_by_user_id: str,
    title: str | None,
    source_kind: ReportSourceKind,
    findings: list[ReportFindingCreateInput],
    observed_at: datetime | None = None,
) -> Report:
    report = Report(
        subject_user_id=subject_user_id,
        created_by_user_id=created_by_user_id,
        title=title,
        source_kind=source_kind,
        sharing_mode=ReportSharingMode.PRIVATE,
        observed_at=observed_at or datetime.now(UTC),
    )
    session.add(report)
    await session.flush()

    for idx, finding in enumerate(findings, start=1):
        session.add(
            ReportFinding(
                report_id=report.id,
                biomarker_key=finding.test_name,
                display_name=finding.test_name,
                value_numeric=finding.value_numeric,
                value_text=finding.value_text,
                unit=finding.unit,
                flag=finding.flag or "unknown",
                reference_range_text=finding.reference_range,
                position=idx,
            )
        )

    await session.commit()

    persisted = await session.scalar(
        select(Report).where(Report.id == report.id).options(selectinload(Report.findings))
    )
    assert persisted is not None
    return persisted


def _active_share_statement(*, subject_user_id: str):
    return select(ConsentShare).where(
        ConsentShare.subject_user_id == subject_user_id,
        ConsentShare.revoked_at.is_(None),
        ConsentShare.expires_at > datetime.now(UTC),
    )


async def sync_subject_report_sharing_modes(session: AsyncSession, *, subject_user_id: str) -> None:
    active_shares = (
        await session.scalars(_active_share_statement(subject_user_id=subject_user_id))
    ).all()
    reports = (
        await session.scalars(select(Report).where(Report.subject_user_id == subject_user_id))
    ).all()

    has_patient_scope = any(
        share.scope == ConsentScope.PATIENT and share.report_id is None for share in active_shares
    )
    report_scope_ids = {
        share.report_id
        for share in active_shares
        if share.scope == ConsentScope.REPORT and share.report_id is not None
    }

    for report in reports:
        target_mode = (
            ReportSharingMode.SHARED
            if has_patient_scope or report.id in report_scope_ids
            else ReportSharingMode.PRIVATE
        )
        if report.sharing_mode != target_mode:
            report.sharing_mode = target_mode
            session.add(report)


def _require_report_owner(*, report: Report, owner_user_id: str, action: str) -> None:
    if report.subject_user_id != owner_user_id:
        raise ReportServiceError(f"Only report owner may {action}", 403)


async def _load_grantee(session: AsyncSession, *, clinician_email: str) -> User:
    grantee = await session.scalar(
        select(User)
        .where(User.email == clinician_email)
        .options(selectinload(User.roles))  # Eagerly load roles to avoid lazy-load in async context
    )
    if grantee is None:
        raise ReportServiceError("Clinician user not found", 404)
    return grantee


async def share_report_with_user(
    session: AsyncSession,
    *,
    report: Report,
    owner_user_id: str,
    clinician_email: str,
    scope: ConsentScope,
    access_level: ConsentAccessLevel,
    expires_at: datetime,
    view_scope: ShareViewScope = ShareViewScope.SUMMARY_ONLY,
    include_doctor_summary: bool = False,
) -> ReportShareResult:
    _require_report_owner(report=report, owner_user_id=owner_user_id, action="grant sharing")

    if expires_at <= datetime.now(UTC):
        raise ReportServiceError("expires_at must be in the future", 400)

    grantee = await _load_grantee(session, clinician_email=clinician_email)
    if grantee.id == owner_user_id:
        raise ReportServiceError("Cannot share with yourself", 400)

    # Verify recipient is a clinician
    clinician_roles = {role.name for role in grantee.roles}
    if "clinician" not in clinician_roles:
        raise ReportServiceError("Recipient must have clinician role", 400)

    target_report_id = report.id if scope == ConsentScope.REPORT else None
    existing_share = await session.scalar(
        select(ConsentShare).where(
            ConsentShare.subject_user_id == report.subject_user_id,
            ConsentShare.grantee_user_id == grantee.id,
            ConsentShare.report_id == target_report_id,
            ConsentShare.scope == scope,
        )
    )

    if existing_share is None:
        share = ConsentShare(
            subject_user_id=report.subject_user_id,
            grantee_user_id=grantee.id,
            granted_by_user_id=owner_user_id,
            report_id=target_report_id,
            scope=scope,
            access_level=access_level,
            expires_at=expires_at,
            view_scope=view_scope,
            include_doctor_summary=include_doctor_summary,
        )
        session.add(share)
    else:
        existing_share.access_level = access_level
        existing_share.expires_at = expires_at
        existing_share.revoked_at = None
        existing_share.view_scope = view_scope
        existing_share.include_doctor_summary = include_doctor_summary
        share = existing_share

    await session.flush()

    # Create audit event for share creation
    await _create_audit_event(
        session,
        actor_user_id=owner_user_id,
        subject_user_id=grantee.id,
        resource_type="consent_share",
        resource_id=share.id,
        action="created",
        context={
            "report_id": report.id if scope == ConsentScope.REPORT else None,
            "scope": scope.value,
            "access_level": access_level.value,
            "expires_at": expires_at.isoformat(),
            "grantee_email": grantee.email,
        },
    )

    resource_type = "report" if scope == ConsentScope.REPORT else "patient"
    resource_id = report.id if scope == ConsentScope.REPORT else report.subject_user_id
    await emit_notification(
        session,
        recipient_user_id=report.subject_user_id,
        kind=NotificationKind.REPORT_SHARED_CONFIRMED,
        message="Report sharing confirmed.",
        resource_type=resource_type,
        resource_id=resource_id,
        report_id=report.id if scope == ConsentScope.REPORT else None,
        payload={"report_id": report.id, "grantee_email": grantee.email, "scope": scope.value},
    )
    await emit_notification(
        session,
        recipient_user_id=grantee.id,
        kind=NotificationKind.NEW_REPORT_SHARED,
        message="A report was shared with you.",
        resource_type=resource_type,
        resource_id=resource_id,
        report_id=report.id if scope == ConsentScope.REPORT else None,
        payload={
            "report_id": report.id,
            "subject_user_id": report.subject_user_id,
            "scope": scope.value,
        },
    )

    await sync_subject_report_sharing_modes(session, subject_user_id=report.subject_user_id)
    await session.commit()
    await session.refresh(share)
    return ReportShareResult(share=share, grantee=grantee)


async def revoke_report_share(
    session: AsyncSession,
    *,
    report: Report,
    owner_user_id: str,
    clinician_email: str,
) -> None:
    _require_report_owner(report=report, owner_user_id=owner_user_id, action="revoke sharing")
    grantee = await _load_grantee(session, clinician_email=clinician_email)

    share = await session.scalar(
        select(ConsentShare).where(
            ConsentShare.subject_user_id == report.subject_user_id,
            ConsentShare.grantee_user_id == grantee.id,
            ConsentShare.report_id == report.id,
            ConsentShare.scope == ConsentScope.REPORT,
            ConsentShare.revoked_at.is_(None),
        )
    )

    if share is None:
        share = await session.scalar(
            select(ConsentShare).where(
                ConsentShare.subject_user_id == report.subject_user_id,
                ConsentShare.grantee_user_id == grantee.id,
                ConsentShare.report_id.is_(None),
                ConsentShare.scope == ConsentScope.PATIENT,
                ConsentShare.revoked_at.is_(None),
            )
        )

    if share is None:
        raise ReportServiceError("No active share found", 404)

    share.revoked_at = datetime.now(UTC)
    await session.flush()

    # Create audit event for share revocation
    await _create_audit_event(
        session,
        actor_user_id=owner_user_id,
        subject_user_id=grantee.id,
        resource_type="consent_share",
        resource_id=share.id,
        action="revoked",
        context={
            "report_id": report.id if share.scope == ConsentScope.REPORT else None,
            "scope": share.scope.value,
            "access_level": share.access_level.value,
            "grantee_email": grantee.email,
        },
    )

    resource_type = "report" if share.scope == ConsentScope.REPORT else "patient"
    resource_id = report.id if share.scope == ConsentScope.REPORT else report.subject_user_id
    await emit_notification(
        session,
        recipient_user_id=report.subject_user_id,
        kind=NotificationKind.SHARE_REVOCATION_CONFIRMED,
        message="Report sharing revoked.",
        resource_type=resource_type,
        resource_id=resource_id,
        report_id=report.id if share.scope == ConsentScope.REPORT else None,
        payload={
            "report_id": report.id,
            "grantee_email": grantee.email,
            "scope": share.scope.value,
        },
    )
    await emit_notification(
        session,
        recipient_user_id=grantee.id,
        kind=NotificationKind.SHARE_REVOKED,
        message="Report sharing was revoked.",
        resource_type=resource_type,
        resource_id=resource_id,
        report_id=report.id if share.scope == ConsentScope.REPORT else None,
        payload={
            "report_id": report.id,
            "subject_user_id": report.subject_user_id,
            "scope": share.scope.value,
        },
    )

    await sync_subject_report_sharing_modes(session, subject_user_id=report.subject_user_id)
    await session.commit()


@dataclass(frozen=True)
class ClinicianSharedReportItem:
    """Clinician's view of a shared report with patient profile"""

    share_id: str
    report_id: str
    report: Report
    patient: User
    scope: str
    access_level: str
    shared_at: datetime
    expires_at: datetime


async def get_clinician_shared_reports(
    session: AsyncSession,
    *,
    clinician_user_id: str,
) -> list[ClinicianSharedReportItem]:
    """Get all reports actively shared with a clinician."""
    now = datetime.now(UTC)

    share_rows = await session.execute(
        select(ConsentShare, User)
        .join(User, ConsentShare.subject_user_id == User.id)
        .where(
            ConsentShare.grantee_user_id == clinician_user_id,
            ConsentShare.revoked_at.is_(None),
            ConsentShare.expires_at > now,
        )
        .order_by(ConsentShare.created_at.desc())
    )

    by_report: dict[str, ClinicianSharedReportItem] = {}

    for share, patient in share_rows.unique():
        if share.scope == ConsentScope.REPORT and share.report_id is not None:
            report = await session.scalar(
                select(Report)
                .where(Report.id == share.report_id)
                .options(selectinload(Report.findings))
            )
            if report is None:
                continue
            by_report[report.id] = ClinicianSharedReportItem(
                share_id=share.id,
                report_id=report.id,
                report=report,
                patient=patient,
                scope=share.scope.value,
                access_level=share.access_level.value,
                shared_at=share.created_at,
                expires_at=share.expires_at,
            )
            continue

        if share.scope == ConsentScope.PATIENT and share.report_id is None:
            reports = (
                await session.scalars(
                    select(Report)
                    .where(Report.subject_user_id == share.subject_user_id)
                    .options(selectinload(Report.findings))
                )
            ).all()

            for report in reports:
                existing = by_report.get(report.id)
                if existing is not None and existing.scope == ConsentScope.REPORT.value:
                    continue

                by_report[report.id] = ClinicianSharedReportItem(
                    share_id=share.id,
                    report_id=report.id,
                    report=report,
                    patient=patient,
                    scope=share.scope.value,
                    access_level=share.access_level.value,
                    shared_at=share.created_at,
                    expires_at=share.expires_at,
                )

    return sorted(by_report.values(), key=lambda item: item.shared_at, reverse=True)


@dataclass(frozen=True)
class AuditLogEntry:
    """Audit log entry for patient view"""

    event_id: str
    action: str
    occurred_at: datetime
    context: dict


async def get_report_audit_log(
    session: AsyncSession,
    *,
    report_id: str,
    owner_user_id: str,
    actions: list[str] | None = None,
) -> list[AuditLogEntry]:
    """Get audit log for a report owned by owner_user_id.

    Args:
        session: Database session
        report_id: Report ID to audit
        owner_user_id: Verify this user owns the report
        actions: Filter by actions (created, revoked, expired). None = all.

    Returns:
        List of audit events in DESC chronological order
    """
    # Verify ownership
    report = await session.scalar(select(Report).where(Report.id == report_id))
    if report is None:
        raise ReportServiceError("Report not found", 404)

    if report.subject_user_id != owner_user_id:
        raise ReportServiceError("Access denied", 403)

    share_ids = select(ConsentShare.id).where(
        ConsentShare.subject_user_id == report.subject_user_id,
        or_(
            and_(
                ConsentShare.scope == ConsentScope.REPORT,
                ConsentShare.report_id == report_id,
            ),
            and_(
                ConsentShare.scope == ConsentScope.PATIENT,
                ConsentShare.report_id.is_(None),
            ),
        ),
    )

    # Query audit events
    context_report_id = (AuditEvent.context["report_id"]).as_string()
    query = select(AuditEvent).where(
        AuditEvent.resource_type == "consent_share",
        AuditEvent.resource_id.in_(share_ids),
        or_(
            context_report_id.is_(None),
            context_report_id == report_id,
        ),
    )

    if actions:
        query = query.where(AuditEvent.action.in_(actions))

    result = await session.execute(query.order_by(AuditEvent.occurred_at.desc()))
    events = result.scalars().all()

    return [
        AuditLogEntry(
            event_id=event.id,
            action=event.action,
            occurred_at=event.occurred_at,
            context=event.context or {},
        )
        for event in events
    ]


async def cleanup_expired_shares(session: AsyncSession) -> int:
    """
    Background job to mark expired shares as revoked and create audit events.

    Finds all active (not revoked) shares where expires_at <= now, marks them revoked,
    and creates a SHARE_EXPIRED audit event for each.

    Returns the count of shares cleaned up.
    """
    now = datetime.now(UTC)

    # Find all active (not revoked) shares that have expired
    query = select(ConsentShare).where(
        (ConsentShare.revoked_at.is_(None)) & (ConsentShare.expires_at <= now)
    )
    result = await session.execute(query)
    expired_shares = result.scalars().all()

    cleaned_count = 0
    for share in expired_shares:
        share.revoked_at = now

        # Create audit event for the expiry
        grantee = await session.get(User, share.grantee_user_id)

        context = {
            "scope": share.scope.value,
            "access_level": share.access_level.value,
            "expired_at": now.isoformat(),
            "grantee_email": grantee.email if grantee else None,
        }
        if share.report_id:
            context["report_id"] = share.report_id

        await _create_audit_event(
            session,
            actor_user_id=None,  # System job, no user actor
            subject_user_id=share.grantee_user_id,
            resource_type="consent_share",
            resource_id=share.id,
            action="expired",
            context=context,
        )

        await emit_notification(
            session,
            recipient_user_id=share.grantee_user_id,
            kind=NotificationKind.SHARE_EXPIRED,
            message="A shared report has expired.",
            resource_type="report" if share.report_id else "patient",
            resource_id=share.report_id or share.subject_user_id,
            report_id=share.report_id,
            payload={"share_id": share.id, "subject_user_id": share.subject_user_id},
        )

        cleaned_count += 1

    await session.commit()
    return cleaned_count
