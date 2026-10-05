"""Server-side authN/authZ dependencies. Every route uses one of these; the
role and container assignments are always re-read from the DB, never trusted
from the client."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Path, Request, status
from starlette.requests import HTTPConnection

from .schemas import NAME_PATTERN
from .security import SAFE_METHODS, SESSION_COOKIE, csrf_ok


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    name: str
    role: str
    csrf: str
    session_expires: int
    token: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def resolve_user(conn: HTTPConnection) -> CurrentUser | None:
    """Cookie -> verified signature -> live session row -> user row."""
    state = conn.app.state
    cookie = conn.cookies.get(SESSION_COOKIE)
    token = state.signer.unsign(cookie) if cookie else None
    session = state.db.get_session(token) if token else None
    user = state.db.get_user(session["user_id"]) if session else None
    if not user:
        return None
    return CurrentUser(
        id=user["id"], email=user["email"], name=user["name"], role=user["role"],
        csrf=session["csrf"], session_expires=session["expires_at"], token=token,
    )


def can_access(conn: HTTPConnection, user: CurrentUser, container: str) -> bool:
    return user.is_admin or conn.app.state.db.is_assigned(user.id, container)


async def require_user(request: Request) -> CurrentUser:
    user = resolve_user(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    # CSRF is enforced centrally: any state-changing request needs the token.
    if request.method not in SAFE_METHODS and not csrf_ok(
        request, user.csrf, request.app.state.settings.public_origin
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF check failed")
    return user


async def require_admin(user: CurrentUser = Depends(require_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


async def require_container_access(
    request: Request,
    name: str = Path(pattern=NAME_PATTERN),
    user: CurrentUser = Depends(require_user),
) -> CurrentUser:
    # Same 403 whether the container exists or not, so names cannot be probed.
    if not can_access(request, user, name):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No access to this container")
    return user


async def require_admin_container(
    name: str = Path(pattern=NAME_PATTERN),
    user: CurrentUser = Depends(require_admin),
) -> CurrentUser:
    """Admin + a validated container name (for mutating container routes)."""
    return user
