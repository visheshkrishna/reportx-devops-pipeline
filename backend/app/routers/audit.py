"""Audit log endpoints"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.dependencies.auth import AuthContext, get_current_auth_context
from app.services.reports import ReportServiceError, get_report_audit_log

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditEventOut(BaseModel):
    """Audit event output"""

    event_id: str
    action: str
    occurred_at: datetime
    context: dict


@router.get("/reports/{report_id}", response_model=list[AuditEventOut])
async def get_report_audit_log_endpoint(
    report_id: str,
    action: str | None = None,
    auth: AuthContext = Depends(get_current_auth_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[AuditEventOut]:
    """Get audit log for a report owned by authenticated user.

    Filters:
        action: Comma-separated list of actions (created, revoked, expired)
    """
    from fastapi import status as http_status  # local to avoid circular

    if "clinician" in auth.roles and "patient" not in auth.roles:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Clinicians may not access audit logs",
        )
    try:
        actions = None
        if action:
            actions = [a.strip() for a in action.split(",") if a.strip()]

        entries = await get_report_audit_log(
            session,
            report_id=report_id,
            owner_user_id=auth.user.id,
            actions=actions,
        )
    except ReportServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)

    return [
        AuditEventOut(
            event_id=entry.event_id,
            action=entry.action,
            occurred_at=entry.occurred_at,
            context=entry.context,
        )
        for entry in entries
    ]
