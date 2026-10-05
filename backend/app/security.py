"""Session cookies, CSRF, rate limiting and security headers."""
from __future__ import annotations

import hmac
import secrets
import time
from collections import OrderedDict, deque

from itsdangerous import BadSignature, Signer
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SESSION_COOKIE = "hsm_session"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SessionSigner:
    """Cookie value = HMAC-signed random token. The token maps to a server-side
    session row, so logout/revocation/expiry are enforced on the server.

    Deliberately no signed timestamp: expiry lives in the DB row (and the
    cookie Max-Age). itsdangerous' TimestampSigner rejects "future" signatures,
    which randomly logged users out whenever the wall clock stepped backwards
    (observed on WSL2; also happens with NTP corrections)."""

    def __init__(self, secret_key: str) -> None:
        self._signer = Signer(secret_key, salt="hsm-session")

    def new_token(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        return token, self._signer.sign(token).decode()

    def unsign(self, cookie: str) -> str | None:
        try:
            return self._signer.unsign(cookie).decode()
        except BadSignature:
            return None


def csrf_ok(conn: HTTPConnection, expected: str, public_origin: str) -> bool:
    """Synchronizer-token check plus an Origin check when the browser sends one."""
    origin = conn.headers.get("origin")
    if origin is not None and origin != public_origin:
        return False
    supplied = conn.headers.get(CSRF_HEADER, "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def client_ip(conn: HTTPConnection) -> str:
    return conn.client.host if conn.client else "unknown"


class RateLimiter:
    """In-process sliding-window limiter (fine for a single worker).
    The key table is bounded so it cannot be used to exhaust memory."""

    def __init__(self, max_keys: int = 10_000) -> None:
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._max_keys = max_keys

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        hits = self._hits.pop(key, None) or deque()
        while hits and hits[0] <= now - window:
            hits.popleft()
        allowed = len(hits) < limit
        if allowed:
            hits.append(now)
        self._hits[key] = hits  # re-insert as most recently used
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)
        return allowed


CSP = (
    "default-src 'self'; script-src 'self'; "
    # xterm.js injects a <style> element for its theme; inline *scripts* stay forbidden.
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class SecurityHeaders:
    """Pure ASGI middleware (unlike BaseHTTPMiddleware it does not buffer SSE)."""

    def __init__(self, app: ASGIApp, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h["Content-Security-Policy"] = CSP
                h["X-Frame-Options"] = "DENY"
                h["X-Content-Type-Options"] = "nosniff"
                h["Referrer-Policy"] = "same-origin"
                h["Cross-Origin-Opener-Policy"] = "same-origin"
                h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                if scope["path"].startswith(("/api/", "/auth/")):
                    h["Cache-Control"] = "no-store"
                if self.hsts:
                    h["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        await self.app(scope, receive, send_with_headers)
