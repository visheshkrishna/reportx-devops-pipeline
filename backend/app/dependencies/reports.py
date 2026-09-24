from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import AuditEvent, ConsentScope, ConsentShare, NotificationKind, Report
from app.db.session import get_db_session
from app.services.notifications import emit_notification

from .auth import AuthContext, get_current_auth_context


async def get_accessible_report(
    report_id: str,
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> Report:
    report = await session.scalar(
        select(Report).options(selectinload(Report.findings)).where(Report.id == report_id)
    )
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

    if report.subject_user_id == auth.user.id:
        return report

    now = datetime.now(UTC)

    # Check for active, non-revoked, non-expired shares
    share = await session.scalar(
        select(ConsentShare)
        .where(
            ConsentShare.subject_user_id == report.subject_user_id,
            ConsentShare.grantee_user_id == auth.user.id,
            ConsentShare.revoked_at.is_(None),
            ConsentShare.expires_at > now,
            or_(
                and_(
                    ConsentShare.scope == ConsentScope.PATIENT,
                    ConsentShare.report_id.is_(None),
                ),
                and_(
                    ConsentShare.scope == ConsentScope.REPORT,
                    ConsentShare.report_id == report.id,
                ),
            ),
        )
        .limit(1)
    )

    if share is not None:
        audit = AuditEvent(
            actor_user_id=auth.user.id,
            subject_user_id=auth.user.id,
            resource_type="consent_share",
            resource_id=share.id,
            action="view",
            context={
                "report_id": report.id,
                "scope": share.scope.value,
                "access_level": share.access_level.value,
            },
            occurred_at=now,
        )
        session.add(audit)
        await emit_notification(
            session,
            recipient_user_id=report.subject_user_id,
            kind=NotificationKind.CLINICIAN_VIEWED_REPORT,
            message=f"{auth.user.display_name} viewed your shared report",
            resource_type="report",
            resource_id=report.id,
            report_id=report.id,
            payload={"report_id": report.id, "viewer_user_id": auth.user.id},
        )
        await session.commit()
        return report

    # Check for revoked shares
    revoked_share = await session.scalar(
        select(ConsentShare)
        .where(
            ConsentShare.subject_user_id == report.subject_user_id,
            ConsentShare.grantee_user_id == auth.user.id,
            ConsentShare.revoked_at.is_not(None),
            or_(
                and_(
                    ConsentShare.scope == ConsentScope.PATIENT,
                    ConsentShare.report_id.is_(None),
                ),
                and_(
                    ConsentShare.scope == ConsentScope.REPORT,
                    ConsentShare.report_id == report.id,
                ),
            ),
        )
        .limit(1)
    )
    if revoked_share is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access has been revoked")

    # Check for expired shares
    expired_share = await session.scalar(
        select(ConsentShare)
        .where(
            ConsentShare.subject_user_id == report.subject_user_id,
            ConsentShare.grantee_user_id == auth.user.id,
            ConsentShare.revoked_at.is_(None),
            ConsentShare.expires_at <= now,
            or_(
                and_(
                    ConsentShare.scope == ConsentScope.PATIENT,
                    ConsentShare.report_id.is_(None),
                ),
                and_(
                    ConsentShare.scope == ConsentScope.REPORT,
                    ConsentShare.report_id == report.id,
                ),
            ),
        )
        .limit(1)
    )

    if expired_share is not None:
        # Create SHARE_EXPIRED audit event (first time expired share is accessed)
        # Check if we already logged this expiry
        existing_expiry_event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.resource_type == "consent_share",
                AuditEvent.action == "expired",
                AuditEvent.resource_id == expired_share.id,
            )
        )

        if existing_expiry_event is None:
            # Create audit event for expiry
            audit = AuditEvent(
                actor_user_id=None,  # System action
                subject_user_id=auth.user.id,
                resource_type="consent_share",
                resource_id=expired_share.id,
                action="expired",
                context={
                    "report_id": report.id if expired_share.scope == ConsentScope.REPORT else None,
                    "scope": expired_share.scope.value,
                    "access_level": expired_share.access_level.value,
                    "expired_at": now.isoformat(),
                    "expires_at": expired_share.expires_at.isoformat(),
                },
                occurred_at=now,
            )
            session.add(audit)

        await emit_notification(
            session,
            recipient_user_id=auth.user.id,
            kind=NotificationKind.SHARE_EXPIRED,
            message="A shared report has expired.",
            resource_type="report" if expired_share.report_id else "patient",
            resource_id=expired_share.report_id or report.subject_user_id,
            report_id=expired_share.report_id,
            payload={"share_id": expired_share.id, "subject_user_id": report.subject_user_id},
        )
        await session.commit()

        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Share has expired")

    # No valid share found
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
