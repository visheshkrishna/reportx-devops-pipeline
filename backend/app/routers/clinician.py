"""Clinician-scoped report endpoints (T17).

All endpoints:
  - Require the authenticated user to have the 'clinician' role.
  - Re-validate the active share on every request (no frontend-state trust).
  - Return 403 (never 404/500) when access is denied.
  - Never expose audit log, share creation, revocation, or export controls.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.dependencies.auth import AuthContext, get_current_auth_context
from app.services.clinician import (
    get_clinician_patient_summary,
    get_clinician_report_scoped,
    list_clinician_dashboard,
)

router = APIRouter(prefix="/clinician", tags=["clinician"])


def _require_clinician(auth: AuthContext) -> None:
    if "clinician" not in auth.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only clinicians may access this resource",
        )


# ── response schemas ──────────────────────────────────────────────────────────


class ClinicianDashboardItemOut(BaseModel):
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


class PatientProfileOut(BaseModel):
    id: str
    display_name: str
    email: str = ""
    date_of_birth: date | None = None


class FindingOut(BaseModel):
    id: str
    biomarker_key: str
    display_name: str
    value_numeric: float | None
    value_text: str | None
    unit: str | None
    flag: str
    reference_range_text: str | None


class TrendPointOut(BaseModel):
    report_id: str
    observed_at: datetime
    value: float
    unit: str | None
    flag: str


class TrendOut(BaseModel):
    biomarker_key: str
    display_name: str
    unit: str | None
    direction: str
    trend_note: str
    sparkline: list[TrendPointOut]


class ThreadMessageOut(BaseModel):
    id: str
    author_user_id: str
    body: str
    created_at: datetime


class ThreadOut(BaseModel):
    id: str
    title: str | None
    status: str
    messages: list[ThreadMessageOut]


class ClinicianReportViewOut(BaseModel):
    report_id: str
    view_scope: str
    include_doctor_summary: bool
    patient: PatientProfileOut
    report_date: datetime
    panel_type: str
    ai_summary: dict | None = None
    findings: list[FindingOut] | None = None
    trends: list[TrendOut] | None = None
    threads: list[ThreadOut] | None = None
    doctor_summary: dict | None = None


class SharedReportSummaryOut(BaseModel):
    share_id: str
    report_id: str
    panel_type: str
    report_date: datetime
    view_scope: str
    include_doctor_summary: bool
    expires_at: datetime
    shared_at: datetime


class PatientSummaryThreadOut(BaseModel):
    report_id: str
    thread_id: str
    title: str | None
    status: str
    messages: list[ThreadMessageOut]


class ClinicianPatientSummaryOut(BaseModel):
    patient: PatientProfileOut
    shared_reports: list[SharedReportSummaryOut]
    trends: list[TrendOut] | None = None
    threads: list[PatientSummaryThreadOut] = []


# ── endpoints ─────────────────────────────────────────────────────────────────


@router.get("/shared-reports", response_model=list[ClinicianDashboardItemOut])
async def clinician_shared_reports_dashboard(
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[ClinicianDashboardItemOut]:
    """List all reports actively shared with the authenticated clinician."""
    _require_clinician(auth)

    items = await list_clinician_dashboard(session, clinician_user_id=auth.user.id)
    return [
        ClinicianDashboardItemOut(
            share_id=item.share_id,
            report_id=item.report_id,
            patient_name=item.patient_name,
            patient_id=item.patient_id,
            report_date=item.report_date,
            panel_type=item.panel_type,
            view_scope=item.view_scope,
            include_doctor_summary=item.include_doctor_summary,
            expires_at=item.expires_at,
            shared_at=item.shared_at,
        )
        for item in items
    ]


@router.get("/shared-reports/{report_id}", response_model=ClinicianReportViewOut)
async def clinician_scoped_report_view(
    report_id: str,
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> ClinicianReportViewOut:
    """Return report content gated by the clinician's share scope.

    Always re-validates the share. Returns 403 if no active share exists.
    """
    _require_clinician(auth)

    try:
        view = await get_clinician_report_scoped(
            session,
            clinician_user_id=auth.user.id,
            report_id=report_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    findings_out: list[FindingOut] | None = None
    if view.findings is not None:
        findings_out = [
            FindingOut(
                id=f.id,
                biomarker_key=f.biomarker_key,
                display_name=f.display_name,
                value_numeric=f.value_numeric,
                value_text=f.value_text,
                unit=f.unit,
                flag=f.flag.value,
                reference_range_text=f.reference_range_text,
            )
            for f in view.findings
        ]

    trends_out: list[TrendOut] | None = None
    if view.trends is not None:
        trends_out = [
            TrendOut(
                biomarker_key=t.biomarker_key,
                display_name=t.display_name,
                unit=t.unit,
                direction=t.direction,
                trend_note=t.trend_note,
                sparkline=[
                    TrendPointOut(
                        report_id=p.report_id,
                        observed_at=p.observed_at,
                        value=p.value,
                        unit=p.unit,
                        flag=p.flag.value,
                    )
                    for p in t.points
                ],
            )
            for t in view.trends
        ]

    threads_out: list[ThreadOut] | None = None
    if view.threads is not None:
        threads_out = [
            ThreadOut(
                id=thread.id,
                title=thread.title,
                status=thread.status.value,
                messages=[
                    ThreadMessageOut(
                        id=msg.id,
                        author_user_id=msg.author_user_id,
                        body=msg.body,
                        created_at=msg.created_at,
                    )
                    for msg in thread.messages
                ],
            )
            for thread in view.threads
        ]

    return ClinicianReportViewOut(
        report_id=view.report_id,
        view_scope=view.view_scope,
        include_doctor_summary=view.include_doctor_summary,
        patient=PatientProfileOut(
            id=view.patient.id,
            display_name=view.patient.display_name,
            email=view.patient.email,
            date_of_birth=view.patient.date_of_birth,
        ),
        report_date=view.report_date,
        panel_type=view.panel_type,
        ai_summary=view.ai_summary,
        findings=findings_out,
        trends=trends_out,
        threads=threads_out,
        doctor_summary=view.doctor_summary,
    )


@router.get("/patients/{patient_id}", response_model=ClinicianPatientSummaryOut)
async def clinician_patient_summary(
    patient_id: str,
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> ClinicianPatientSummaryOut:
    """Return a patient's full profile visible to the authenticated clinician.

    Validates active share access. Trends only returned when a full_report+ share
    exists. Threads only returned from full_report_with_threads shares.
    """
    _require_clinician(auth)

    try:
        summary = await get_clinician_patient_summary(
            session,
            clinician_user_id=auth.user.id,
            patient_id=patient_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    trends_out: list[TrendOut] | None = None
    if summary.trends is not None:
        trends_out = [
            TrendOut(
                biomarker_key=t.biomarker_key,
                display_name=t.display_name,
                unit=t.unit,
                direction=t.direction,
                trend_note=t.trend_note,
                sparkline=[
                    TrendPointOut(
                        report_id=p.report_id,
                        observed_at=p.observed_at,
                        value=p.value,
                        unit=p.unit,
                        flag=p.flag.value,
                    )
                    for p in t.points
                ],
            )
            for t in summary.trends
        ]

    threads_out: list[PatientSummaryThreadOut] = [
        PatientSummaryThreadOut(
            report_id=report_id,
            thread_id=thread.id,
            title=thread.title,
            status=thread.status.value,
            messages=[
                ThreadMessageOut(
                    id=msg.id,
                    author_user_id=msg.author_user_id,
                    body=msg.body,
                    created_at=msg.created_at,
                )
                for msg in thread.messages
            ],
        )
        for report_id, thread in summary.threads_with_report
    ]

    return ClinicianPatientSummaryOut(
        patient=PatientProfileOut(
            id=summary.patient.id,
            display_name=summary.patient.display_name,
            email=summary.patient.email,
            date_of_birth=summary.patient.date_of_birth,
        ),
        shared_reports=[
            SharedReportSummaryOut(
                share_id=item.share_id,
                report_id=item.report_id,
                panel_type=item.panel_type,
                report_date=item.report_date,
                view_scope=item.view_scope,
                include_doctor_summary=item.include_doctor_summary,
                expires_at=item.expires_at,
                shared_at=item.shared_at,
            )
            for item in summary.shared_reports
        ],
        trends=trends_out,
        threads=threads_out,
    )
