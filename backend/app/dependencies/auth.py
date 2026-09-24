from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuthSession, User
from app.db.session import get_db_session
from app.services.auth import AuthError, load_authenticated_session, role_names_for_user

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    user: User
    auth_session: AuthSession
    roles: frozenset[str]


def _auth_http_exception(detail: str, status_code: int) -> HTTPException:
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


async def get_current_auth_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _auth_http_exception("Not authenticated", status.HTTP_401_UNAUTHORIZED)

    try:
        auth_session = await load_authenticated_session(
            session, access_token=credentials.credentials
        )
    except AuthError as exc:
        raise _auth_http_exception(exc.detail, exc.status_code) from exc

    return AuthContext(
        user=auth_session.user,
        auth_session=auth_session,
        roles=frozenset(role_names_for_user(auth_session.user)),
    )


async def get_current_user(context: AuthContext = Depends(get_current_auth_context)) -> User:
    return context.user
