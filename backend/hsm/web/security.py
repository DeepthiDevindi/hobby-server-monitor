"""Cookie signing, CSRF, rate limiting and security headers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict, deque
from typing import Any

SESSION_COOKIE = "hsm_session"
OAUTH_COOKIE = "hsm_oauth"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class Signer:
    """HMAC-SHA256 signing with a purpose label (so a token signed for one
    use cannot be replayed as another).

    Deliberately no signed timestamp: session expiry is enforced by the DB row.
    (An earlier version used itsdangerous' TimestampSigner, which rejects
    "future" signatures; on WSL2 the wall clock stepped back 1.4 s twice in
    30 s and users were randomly logged out.)"""

    def __init__(self, secret: str) -> None:
        self._key = hashlib.sha256(secret.encode()).digest()

    def _mac(self, purpose: str, value: str) -> str:
        mac = hmac.new(self._key, f"{purpose}\x00{value}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def sign(self, purpose: str, value: str) -> str:
        return f"{value}.{self._mac(purpose, value)}"

    def unsign(self, purpose: str, signed: str | None) -> str | None:
        if not signed or "." not in signed:
            return None
        value, mac = signed.rsplit(".", 1)
        return value if hmac.compare_digest(mac, self._mac(purpose, value)) else None

    # session cookie = signed random token (the token maps to a DB row)
    def new_session_token(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        return token, self.sign("session", token)

    # short-lived OAuth state (state, nonce, PKCE verifier, issue time)
    def pack(self, purpose: str, data: dict[str, Any]) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
        return self.sign(purpose, raw)

    def unpack(self, purpose: str, signed: str | None) -> dict[str, Any] | None:
        raw = self.unsign(purpose, signed)
        if raw is None:
            return None
        try:
            return json.loads(base64.urlsafe_b64decode(raw.encode()))
        except ValueError:
            return None


def csrf_ok(req: Any, expected: str, public_origin: str) -> bool:
    """Synchronizer token + Origin check (when the browser sends Origin)."""
    origin = req.get_header("Origin")
    if origin is not None and origin != public_origin:
        return False
    supplied = req.get_header(CSRF_HEADER) or ""
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def client_ip(req: Any) -> str:
    return getattr(req, "remote_addr", None) or "unknown"


class RateLimiter:
    """In-process sliding window (fine for one web process). The key table is
    bounded, so it cannot be used to exhaust memory."""

    def __init__(self, max_keys: int = 10_000) -> None:
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._max_keys = max_keys

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        hits = self._hits.pop(key, None) or deque()
        while hits and hits[0] <= now - window:
            hits.popleft()
        ok = len(hits) < limit
        if ok:
            hits.append(now)
        self._hits[key] = hits
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)
        return ok


CSP = (
    "default-src 'self'; script-src 'self'; "
    # xterm.js injects a <style> element for its theme; inline *scripts* stay forbidden.
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class SecurityHeaders:
    """Falcon middleware: headers on every HTTP response (API and static)."""

    def __init__(self, hsts: bool) -> None:
        self.hsts = hsts

    async def process_response(self, req: Any, resp: Any, resource: Any, req_succeeded: bool) -> None:
        resp.set_header("Content-Security-Policy", CSP)
        resp.set_header("X-Frame-Options", "DENY")
        resp.set_header("X-Content-Type-Options", "nosniff")
        resp.set_header("Referrer-Policy", "same-origin")
        resp.set_header("Cross-Origin-Opener-Policy", "same-origin")
        resp.set_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if req.path.startswith(("/api/", "/auth/")):
            resp.set_header("Cache-Control", "no-store")
        if self.hsts:
            resp.set_header("Strict-Transport-Security", "max-age=31536000")
