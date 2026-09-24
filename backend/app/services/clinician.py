from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    ConsentScope,
    ConsentShare,
    ConversationThread,
    Report,
    ReportFinding,
    ShareViewScope,
    User,
)
from app.services.trends import BiomarkerTrend, build_trends_for_patient


@dataclass(frozen=True)
class ClinicianDashboardItem:
    share_id: str
    report_id: str
    patient_name: str
    patient_id: str
    report_date: datetime
    panel_type: str
    view_scope: str
    include_doctor_summary: bool
    expires_at: datetime
    shared_at: datetime


@dataclass(frozen=True)
class ClinicianPatientSummary:
    patient: User
    shared_reports: list[ClinicianDashboardItem]
    trends: list[BiomarkerTrend] | None
    threads_with_report: list[tuple[str, ConversationThread]]


@dataclass(frozen=True)
class ClinicianReportView:
    report_id: str
    view_scope: str
    include_doctor_summary: bool
    patient: User
    report_date: datetime
    panel_type: str
    ai_summary: dict | None
    findings: list[ReportFinding] | None
    trends: list[BiomarkerTrend] | None
    threads: list[ConversationThread] | None
    doctor_summary: dict | None


def _active_clinician_share(*, clinician_user_id: str, report_id: str, now: datetime):
    """SQLAlchemy filter clause for a valid (non-expired, non-revoked) share."""
    return and_(
        ConsentShare.grantee_user_id == clinician_user_id,
        ConsentShare.revoked_at.is_(None),
        ConsentShare.expires_at > now,
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


async def list_clinician_dashboard(
    session: AsyncSession,
    *,
    clinician_user_id: str,
) -> list[ClinicianDashboardItem]:
    """Return all actively shared report summaries for a clinician's dashboard."""
    now = datetime.now(UTC)

    rows = await session.execute(
        select(ConsentShare, User, Report)
        .join(User, ConsentShare.subject_user_id == User.id)
        .join(Report, ConsentShare.report_id == Report.id)
        .where(
            ConsentShare.grantee_user_id == clinician_user_id,
            ConsentShare.scope == ConsentScope.REPORT,
            ConsentShare.report_id.is_not(None),
            ConsentShare.revoked_at.is_(None),
            ConsentShare.expires_at > now,
        )
        .order_by(ConsentShare.created_at.desc())
    )

    items: list[ClinicianDashboardItem] = []
    seen_report_ids: set[str] = set()

    for share, patient, report in rows.unique():
        if report.id in seen_report_ids:
            continue
        seen_report_ids.add(report.id)
        items.append(
            ClinicianDashboardItem(
                share_id=share.id,
                report_id=report.id,
                patient_name=patient.display_name,
                patient_id=patient.id,
                report_date=report.observed_at,
                panel_type=report.title or report.source_kind.value,
                view_scope=share.view_scope.value,
                include_doctor_summary=share.include_doctor_summary,
                expires_at=share.expires_at,
                shared_at=share.created_at,
            )
        )

    # Also handle PATIENT-scope shares (covers all patient reports)
    patient_scope_rows = await session.execute(
        select(ConsentShare, User)
        .join(User, ConsentShare.subject_user_id == User.id)
        .where(
            ConsentShare.grantee_user_id == clinician_user_id,
            ConsentShare.scope == ConsentScope.PATIENT,
            ConsentShare.report_id.is_(None),
            ConsentShare.revoked_at.is_(None),
            ConsentShare.expires_at > now,
        )
        .order_by(ConsentShare.created_at.desc())
    )

    for share, patient in patient_scope_rows.unique():
        reports = (
            await session.scalars(
                select(Report)
                .where(Report.subject_user_id == share.subject_user_id)
                .options(selectinload(Report.findings))
            )
        ).all()

        for report in reports:
            if report.id in seen_report_ids:
                continue
            seen_report_ids.add(report.id)
            items.append(
                ClinicianDashboardItem(
                    share_id=share.id,
                    report_id=report.id,
                    patient_name=patient.display_name,
                    patient_id=patient.id,
                    report_date=report.observed_at,
                    panel_type=report.title or report.source_kind.value,
                    view_scope=share.view_scope.value,
                    include_doctor_summary=share.include_doctor_summary,
                    expires_at=share.expires_at,
                    shared_at=share.created_at,
                )
            )

    items.sort(key=lambda i: i.shared_at, reverse=True)
    return items


async def get_clinician_report_scoped(
    session: AsyncSession,
    *,
    clinician_user_id: str,
    report_id: str,
) -> ClinicianReportView:
    """
    Return report data gated by the share's view_scope.

    Raises ValueError with a message suitable for 403 if no active share exists.
    Always re-validates the share; never uses cached state.
    """
    now = datetime.now(UTC)

    share = await session.scalar(
        select(ConsentShare)
        .where(
            _active_clinician_share(
                clinician_user_id=clinician_user_id, report_id=report_id, now=now
            )
        )
        .order_by(ConsentShare.created_at.desc())
        .limit(1)
    )

    if share is None:
        raise ValueError("No active share found for this report and clinician")

    report = await session.scalar(
        select(Report).where(Report.id == report_id).options(selectinload(Report.findings))
    )
    if report is None:
        raise ValueError("Report not found")

    patient = await session.scalar(select(User).where(User.id == report.subject_user_id))
    assert patient is not None

    view_scope = share.view_scope

    findings: list[ReportFinding] | None = None
    trends: list[BiomarkerTrend] | None = None
    threads: list[ConversationThread] | None = None
    doctor_summary: dict | None = None

    if view_scope in (ShareViewScope.FULL_REPORT, ShareViewScope.FULL_REPORT_WITH_THREADS):
        findings = sorted(report.findings, key=lambda f: (f.position, f.display_name.lower()))
        trends = await build_trends_for_patient(session, subject_user_id=report.subject_user_id)

    if view_scope == ShareViewScope.FULL_REPORT_WITH_THREADS:
        thread_rows = await session.scalars(
            select(ConversationThread)
            .where(ConversationThread.report_id == report_id)
            .options(
                selectinload(ConversationThread.messages),
                selectinload(ConversationThread.participants),
            )
        )
        threads = list(thread_rows.all())

    if share.include_doctor_summary:
        doctor_summary = report.interpretation_json or {}

    return ClinicianReportView(
        report_id=report.id,
        view_scope=view_scope.value,
        include_doctor_summary=share.include_doctor_summary,
        patient=patient,
        report_date=report.observed_at,
        panel_type=report.title or report.source_kind.value,
        ai_summary=report.interpretation_json,
        findings=findings,
        trends=trends,
        threads=threads,
        doctor_summary=doctor_summary,
    )


async def get_clinician_patient_summary(
    session: AsyncSession,
    *,
    clinician_user_id: str,
    patient_id: str,
) -> ClinicianPatientSummary:
    """
    Return a full patient summary visible to the clinician.

    Validates at least one active share exists. Gathers all shared reports,
    trends (if any full_report+ share exists), and threads (from
    full_report_with_threads shares only).
    Raises ValueError if access is denied.
    """
    now = datetime.now(UTC)

    patient = await session.scalar(select(User).where(User.id == patient_id))
    if patient is None:
        raise ValueError("Patient not found")

    # Verify at least one active share with this patient
    active_shares = list(
        (
            await session.scalars(
                select(ConsentShare).where(
                    ConsentShare.grantee_user_id == clinician_user_id,
                    ConsentShare.subject_user_id == patient_id,
                    ConsentShare.revoked_at.is_(None),
                    ConsentShare.expires_at > now,
                )
            )
        ).all()
    )
    if not active_shares:
        raise ValueError("No active share found for this patient")

    # Reuse dashboard to get all actively shared reports for this patient
    all_items = await list_clinician_dashboard(session, clinician_user_id=clinician_user_id)
    patient_reports = [item for item in all_items if item.patient_id == patient_id]

    # Trends only if at least one share grants full_report+
    has_full_access = any(
        s.view_scope in (ShareViewScope.FULL_REPORT, ShareViewScope.FULL_REPORT_WITH_THREADS)
        for s in active_shares
    )
    trends: list[BiomarkerTrend] | None = None
    if has_full_access:
        trends = await build_trends_for_patient(session, subject_user_id=patient_id)

    # Threads only from full_report_with_threads report-scope shares
    threads_with_report: list[tuple[str, ConversationThread]] = []
    thread_enabled_report_ids: set[str] = {
        s.report_id
        for s in active_shares
        if s.view_scope == ShareViewScope.FULL_REPORT_WITH_THREADS and s.report_id is not None
    }
    if thread_enabled_report_ids:
        thread_rows = await session.scalars(
            select(ConversationThread)
            .where(ConversationThread.report_id.in_(thread_enabled_report_ids))
            .options(selectinload(ConversationThread.messages))
        )
        for thread in thread_rows.all():
            threads_with_report.append((thread.report_id, thread))

    return ClinicianPatientSummary(
        patient=patient,
        shared_reports=patient_reports,
        trends=trends,
        threads_with_report=threads_with_report,
    )
