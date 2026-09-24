from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ConsentAccessLevel,
    ConsentScope,
    ConsentShare,
    Report,
    ReportFinding,
    ReportSourceKind,
    ShareViewScope,
    User,
)
from app.db.session import get_db_session
from app.dependencies.auth import AuthContext, get_current_auth_context
from app.dependencies.reports import get_accessible_report
from app.services.reports import (
    ReportFindingCreateInput,
    ReportServiceError,
    create_report_for_user,
    get_clinician_shared_reports,
    list_reports_for_user,
    revoke_report_share,
    share_report_with_user,
)
from app.services.trends import BiomarkerTrend, build_trends_for_patient

router = APIRouter(prefix="/reports", tags=["reports"])


class ReportShareRequest(BaseModel):
    clinician_email: EmailStr
    scope: ConsentScope = ConsentScope.REPORT
    access_level: ConsentAccessLevel = ConsentAccessLevel.READ
    expires_at: datetime
    view_scope: ShareViewScope = ShareViewScope.SUMMARY_ONLY
    include_doctor_summary: bool = False


class ReportCreateFinding(BaseModel):
    test_name: str
    value_numeric: float | None = None
    value_text: str | None = None
    unit: str | None = None
    reference_range: str | None = None
    flag: str | None = None
    confidence: float | None = None


class ReportCreateRequest(BaseModel):
    title: str | None = None
    source_kind: ReportSourceKind = ReportSourceKind.MANUAL
    observed_at: datetime | None = None
    findings: list[ReportCreateFinding] = Field(default_factory=list)


class ReportCreateResponse(BaseModel):
    id: str
    title: str | None
    source_kind: str
    sharing_mode: str
    created_at: datetime
    observed_at: datetime


class ReportFindingOut(BaseModel):
    id: str
    biomarker_key: str
    display_name: str
    value_numeric: float | None
    value_text: str | None
    unit: str | None
    flag: str
    reference_range_text: str | None


class ReportOut(BaseModel):
    id: str
    subject_user_id: str
    created_by_user_id: str
    title: str | None
    source_kind: str
    sharing_mode: str
    created_at: datetime
    observed_at: datetime
    findings: list[ReportFindingOut]
    interpretation: dict | None = None


class ReportDetailResponse(BaseModel):
    report: ReportOut


class TrendPointOut(BaseModel):
    report_id: str
    observed_at: datetime
    value: float
    unit: str | None
    flag: str
    reference_low: float | None
    reference_high: float | None


class BiomarkerTrendOut(BaseModel):
    biomarker_key: str
    display_name: str
    unit: str | None
    direction: str
    trend_note: str
    sparkline: list[TrendPointOut]


class ReportTrendsResponse(BaseModel):
    report_id: str
    subject_user_id: str
    trends: list[BiomarkerTrendOut]


def _raise_report_http_error(exc: ReportServiceError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("", response_model=list[ReportOut])
async def list_reports(
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[ReportOut]:
    rows = await list_reports_for_user(session, subject_user_id=auth.user.id)
    return [_report_out(report) for report in rows]


@router.post("", response_model=ReportCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_report(
    payload: ReportCreateRequest,
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> ReportCreateResponse:
    report = await create_report_for_user(
        session,
        subject_user_id=auth.user.id,
        created_by_user_id=auth.user.id,
        title=payload.title,
        source_kind=payload.source_kind,
        observed_at=payload.observed_at or datetime.now(UTC),
        findings=[
            ReportFindingCreateInput(
                test_name=finding.test_name,
                value_numeric=finding.value_numeric,
                value_text=finding.value_text,
                unit=finding.unit,
                reference_range=finding.reference_range,
                flag=finding.flag,
                confidence=finding.confidence,
            )
            for finding in payload.findings
        ],
    )

    return ReportCreateResponse(
        id=report.id,
        title=report.title,
        source_kind=report.source_kind.value,
        sharing_mode=report.sharing_mode.value,
        created_at=report.created_at,
        observed_at=report.observed_at,
    )


class ReportShareResponse(BaseModel):
    id: str
    clinician_email: str
    scope: str
    access_level: str
    expires_at: datetime


@router.post(
    "/{report_id}/share", response_model=ReportShareResponse, status_code=status.HTTP_201_CREATED
)
async def share_report(
    payload: ReportShareRequest,
    report: Report = Depends(get_accessible_report),
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> ReportShareResponse:
    if "clinician" in auth.roles and "patient" not in auth.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Clinicians may not create report shares",
        )
    try:
        result = await share_report_with_user(
            session,
            report=report,
            owner_user_id=auth.user.id,
            clinician_email=str(payload.clinician_email),
            scope=payload.scope,
            access_level=payload.access_level,
            expires_at=payload.expires_at,
            view_scope=payload.view_scope,
            include_doctor_summary=payload.include_doctor_summary,
        )
    except ReportServiceError as exc:
        _raise_report_http_error(exc)

    return ReportShareResponse(
        id=result.share.id,
        clinician_email=result.grantee.email,
        scope=result.share.scope.value,
        access_level=result.share.access_level.value,
        expires_at=result.share.expires_at,
    )


class ReportRevokeRequest(BaseModel):
    clinician_email: EmailStr


@router.post("/{report_id}/share/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share(
    payload: ReportRevokeRequest,
    report: Report = Depends(get_accessible_report),
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    if "clinician" in auth.roles and "patient" not in auth.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Clinicians may not revoke report shares",
        )
    try:
        await revoke_report_share(
            session,
            report=report,
            owner_user_id=auth.user.id,
            clinician_email=str(payload.clinician_email),
        )
    except ReportServiceError as exc:
        _raise_report_http_error(exc)


def _finding_out(finding: ReportFinding) -> ReportFindingOut:
    return ReportFindingOut(
        id=finding.id,
        biomarker_key=finding.biomarker_key,
        display_name=finding.display_name,
        value_numeric=finding.value_numeric,
        value_text=finding.value_text,
        unit=finding.unit,
        flag=finding.flag.value,
        reference_range_text=finding.reference_range_text,
    )


def _report_out(report: Report) -> ReportOut:
    findings = sorted(report.findings, key=lambda item: (item.position, item.display_name.lower()))
    return ReportOut(
        id=report.id,
        subject_user_id=report.subject_user_id,
        created_by_user_id=report.created_by_user_id,
        title=report.title,
        source_kind=report.source_kind.value,
        sharing_mode=report.sharing_mode.value,
        created_at=report.created_at,
        observed_at=report.observed_at,
        findings=[_finding_out(finding) for finding in findings],
        interpretation=report.interpretation_json,
    )


class UserOut(BaseModel):
    """User profile output"""

    id: str
    email: str
    display_name: str
    preferred_language: str | None = None


class ClinicianSharedReportOut(BaseModel):
    """Clinician's view of a shared report"""

    share_id: str
    report_id: str
    report: ReportOut
    patient: UserOut
    scope: str
    access_level: str
    shared_at: datetime
    expires_at: datetime


class SharedReportShareOut(BaseModel):
    share_id: str
    scope: str
    access_level: str
    expires_at: datetime
    include_doctor_summary: bool = False


class SharedReportDetailResponse(BaseModel):
    share: SharedReportShareOut
    patient: UserOut
    report: ReportOut


@router.get("/shared-reports", response_model=list[ClinicianSharedReportOut])
async def list_clinician_shared_reports(
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[ClinicianSharedReportOut]:
    """List all reports actively shared with the authenticated clinician."""
    if "clinician" not in auth.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only clinicians may view shared reports",
        )

    items = await get_clinician_shared_reports(
        session,
        clinician_user_id=auth.user.id,
    )

    return [
        ClinicianSharedReportOut(
            share_id=item.share_id,
            report_id=item.report_id,
            report=_report_out(item.report),
            patient=UserOut(
                id=item.patient.id,
                email=item.patient.email,
                display_name=item.patient.display_name,
                preferred_language=item.patient.preferred_language,
            ),
            scope=item.scope,
            access_level=item.access_level,
            shared_at=item.shared_at,
            expires_at=item.expires_at,
        )
        for item in items
    ]


@router.get("/shared-reports/{report_id}", response_model=SharedReportDetailResponse)
async def get_clinician_shared_report_detail(
    report_id: str,
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> SharedReportDetailResponse:
    """Return one shared report detail for the authenticated clinician."""
    if "clinician" not in auth.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only clinicians may view shared reports",
        )

    report = await get_accessible_report(report_id=report_id, auth=auth, session=session)
    now = datetime.now(UTC)
    share = await session.scalar(
        select(ConsentShare)
        .where(
            ConsentShare.subject_user_id == report.subject_user_id,
            ConsentShare.grantee_user_id == auth.user.id,
            ConsentShare.revoked_at.is_(None),
            ConsentShare.expires_at > now,
            ((ConsentShare.scope == ConsentScope.PATIENT) & (ConsentShare.report_id.is_(None)))
            | ((ConsentShare.scope == ConsentScope.REPORT) & (ConsentShare.report_id == report.id)),
        )
        .order_by(ConsentShare.created_at.desc())
        .limit(1)
    )
    if share is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Shared report access not found"
        )

    patient = await session.get(User, report.subject_user_id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")

    return SharedReportDetailResponse(
        share=SharedReportShareOut(
            share_id=share.id,
            scope=share.scope.value,
            access_level=share.access_level.value,
            expires_at=share.expires_at,
            include_doctor_summary=share.include_doctor_summary,
        ),
        patient=UserOut(
            id=patient.id,
            email=patient.email,
            display_name=patient.display_name,
            preferred_language=patient.preferred_language,
        ),
        report=_report_out(report),
    )


@router.get("/{report_id}", response_model=ReportDetailResponse)
async def get_report_endpoint(
    report: Report = Depends(get_accessible_report),
) -> ReportDetailResponse:
    return ReportDetailResponse(report=_report_out(report))


class SaveInterpretationRequest(BaseModel):
    interpretation: dict


@router.patch("/{report_id}/interpretation", status_code=status.HTTP_204_NO_CONTENT)
async def save_interpretation_endpoint(
    payload: SaveInterpretationRequest,
    report: Report = Depends(get_accessible_report),
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    if report.subject_user_id != auth.user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the report owner may save an interpretation.",
        )
    report.interpretation_json = payload.interpretation
    session.add(report)
    await session.commit()


async def _ensure_full_report_trend_access(
    *,
    report: Report,
    auth: AuthContext,
    session: AsyncSession,
) -> None:
    # T21: Trend data is clinician-only
    if "clinician" not in auth.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only clinicians may access trend data",
        )

    # Report owner (patient who is also clinician - unlikely but handle it) can access own trends
    if report.subject_user_id == auth.user.id:
        return

    # Clinician must have an active share with appropriate scope
    now = datetime.now(UTC)
    share = await session.scalar(
        select(ConsentShare)
        .where(
            ConsentShare.subject_user_id == report.subject_user_id,
            ConsentShare.grantee_user_id == auth.user.id,
            ConsentShare.scope == ConsentScope.PATIENT,
            ConsentShare.report_id.is_(None),
            ConsentShare.revoked_at.is_(None),
            ConsentShare.expires_at > now,
        )
        .limit(1)
    )
    if share is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Trend data requires full-report access to the entire patient, not just individual reports.",
        )

    # T21: Enforce ShareViewScope - trends require full_report or full_report_with_threads
    if share.view_scope == ShareViewScope.SUMMARY_ONLY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Trend data is not available under summary_only scope. Require full_report or full_report_with_threads.",
        )


def _trend_out(trend: BiomarkerTrend) -> BiomarkerTrendOut:
    return BiomarkerTrendOut(
        biomarker_key=trend.biomarker_key,
        display_name=trend.display_name,
        unit=trend.unit,
        direction=trend.direction,
        trend_note=trend.trend_note,
        sparkline=[
            TrendPointOut(
                report_id=point.report_id,
                observed_at=point.observed_at,
                value=point.value,
                unit=point.unit,
                flag=point.flag.value,
                reference_low=point.reference_low,
                reference_high=point.reference_high,
            )
            for point in trend.points
        ],
    )


@router.get("/{report_id}/trends", response_model=ReportTrendsResponse)
async def get_report_trends_endpoint(
    report: Report = Depends(get_accessible_report),
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> ReportTrendsResponse:
    await _ensure_full_report_trend_access(report=report, auth=auth, session=session)

    trends = await build_trends_for_patient(
        session,
        subject_user_id=report.subject_user_id,
    )
    return ReportTrendsResponse(
        report_id=report.id,
        subject_user_id=report.subject_user_id,
        trends=[_trend_out(item) for item in trends],
    )
