"""Authorization, enforced in ONE place: this middleware.

Every resource class must declare `policy` (one Policy for all methods, or a
dict of method -> Policy). The middleware runs before any responder:

    PUBLIC     no session needed (login, static files)
    USER       any signed-in user
    ADMIN      role == admin
    CONTAINER  signed-in AND (admin OR owner/assignee of the {name} in the URL)

Default deny: a resource without a policy for the requested method gets a 500
and is logged, so an endpoint written next month cannot be reachable by
accident. `check_routes()` also fails app startup if any route lacks a policy,
and a test walks every route to prove the 401/403 behaviour.

Role and assignments are read from SQLite on every request; nothing the
client sends (ids, roles, cached lists) is trusted.
"""
from __future__ import annotations

import enum
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import falcon

from .security import SAFE_METHODS, SESSION_COOKIE, csrf_ok

log = logging.getLogger("hsm.policy")
NAME_RE = re.compile(r"[a-z0-9-]{1,63}")  # always used with fullmatch()


class Policy(enum.Enum):
    PUBLIC = "public"
    USER = "user"
    ADMIN = "admin"
    CONTAINER = "container"


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    name: str
    role: str
    csrf: str
    expires_at: int
    token: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def actor(self) -> dict[str, Any]:
        return {"id": self.id, "email": self.email}


def policy_for(resource: Any, method: str) -> Policy | None:
    p = getattr(resource, "policy", None)
    if isinstance(p, dict):
        return p.get(method)
    return p if isinstance(p, Policy) else None


def resolve_user(app_state: Any, cookie: str | None, touch: bool = True) -> CurrentUser | None:
    """Cookie -> HMAC check -> live session row (absolute + idle expiry) -> user."""
    s = app_state
    token = s.signer.unsign("session", cookie)
    if not token:
        return None
    session = s.db.get_session(token, s.settings.session_idle_minutes * 60)
    user = s.db.get_user(session["user_id"]) if session else None
    if not user:
        return None
    # Sliding idle timeout, written at most once a minute (not per request).
    if touch and session["last_seen_at"] < time.time() - 60:
        s.db.touch_session(token)
    return CurrentUser(user["id"], user["email"], user["name"], user["role"], session["csrf"],
                       session["expires_at"], token)


def can_access(app_state: Any, user: CurrentUser, name: str) -> bool:
    return user.is_admin or app_state.db.can_access(user.id, name)


class AuthMiddleware:
    def __init__(self, state: Any) -> None:
        self.state = state

    async def process_request(self, req: Any, resp: Any) -> None:
        req.context.state = self.state

    async def process_request_ws(self, req: Any, ws: Any) -> None:
        req.context.state = self.state

    async def process_resource(self, req: Any, resp: Any, resource: Any, params: dict[str, Any]) -> None:
        if resource is None:
            return  # 404: no route matched
        policy = policy_for(resource, req.method)
        if policy is None:
            log.error("no policy declared for %s %s (%s): denied", req.method, req.path, type(resource).__name__)
            raise falcon.HTTPInternalServerError(title="Endpoint has no authorization policy")
        if policy is Policy.PUBLIC:
            return
        user = resolve_user(self.state, req.cookies.get(SESSION_COOKIE))
        if user is None:
            raise falcon.HTTPUnauthorized(title="Not signed in")
        if req.method not in SAFE_METHODS and not csrf_ok(req, user.csrf, self.state.settings.public_origin):
            raise falcon.HTTPForbidden(title="CSRF check failed")
        self._authorize(policy, user, params)
        req.context.user = user

    @staticmethod
    def _authorize_name(params: dict[str, Any]) -> None:
        # The naming rule is public, so a 422 here leaks nothing.
        if "name" in params and not NAME_RE.fullmatch(params["name"]):
            raise falcon.HTTPUnprocessableEntity(title="Invalid container name",
                                                 description="lowercase letters, digits and '-', 1-63 chars")

    def _authorize(self, policy: Policy, user: CurrentUser, params: dict[str, Any]) -> None:
        if policy is Policy.ADMIN and not user.is_admin:
            raise falcon.HTTPForbidden(title="Admin only")
        self._authorize_name(params)  # any {name} in a URL must be a valid name
        if policy is Policy.CONTAINER:
            # Same 403 for "does not exist" and "not yours": names can't be probed.
            if not can_access(self.state, user, params.get("name", "")):
                raise falcon.HTTPForbidden(title="No access to this container")

    async def process_resource_ws(self, req: Any, ws: Any, resource: Any, params: dict[str, Any]) -> None:
        """WebSockets: Origin is checked before the handshake (cross-site
        WebSocket hijacking). Auth failures accept-then-close with a 44xx code
        so the browser can show *why*; the responder never runs."""
        if req.get_header("Origin") != self.state.settings.public_origin:
            raise falcon.HTTPForbidden(title="Bad origin")  # handshake rejected
        policy = policy_for(resource, "WEBSOCKET")
        if policy is None:
            raise falcon.HTTPInternalServerError(title="Endpoint has no authorization policy")
        user = resolve_user(self.state, req.cookies.get(SESSION_COOKIE))
        try:
            if user is None:
                raise _Deny(4401, "not signed in")
            try:
                self._authorize(policy, user, params)
            except falcon.HTTPError as exc:
                raise _Deny(4403, exc.title or "forbidden") from exc
        except _Deny as d:
            await ws.accept()
            await ws.close(d.code)
            raise falcon.HTTPForbidden(title=d.reason) from None
        req.context.user = user


class _Deny(Exception):
    def __init__(self, code: int, reason: str) -> None:
        self.code, self.reason = code, reason


def check_routes(routes: list[tuple[str, Any]]) -> None:
    """Startup guard: refuse to start if any route lacks a policy."""
    missing = [path for path, res in routes if getattr(res, "policy", None) is None]
    if missing:
        raise RuntimeError(f"routes without an authorization policy: {missing}")
