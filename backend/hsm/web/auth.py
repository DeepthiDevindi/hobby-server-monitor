"""Google OAuth 2.0 / OpenID Connect sign-in (authorization code + PKCE).

Invite-only: a verified Google account gets in only if an admin invited that
email, or it is BOOTSTRAP_ADMIN_EMAIL. Anyone else is turned away without a
row being created, so the users table cannot be filled by strangers.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import base64
import hashlib

import falcon
import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

from .common import audit
from .policy import Policy
from .security import OAUTH_COOKIE, SESSION_COOKIE, client_ip

log = logging.getLogger("hsm.auth")
# Google's endpoints (from https://accounts.google.com/.well-known/openid-configuration)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
OAUTH_TTL = 600


def generate_token(nbytes: int) -> str:
    return secrets.token_urlsafe(nbytes)


def s256(verifier: str) -> str:
    """PKCE S256 code challenge (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class GoogleKeys:
    """Google's signing keys, cached for an hour (they rotate rarely)."""

    def __init__(self) -> None:
        self._keys: Any = None
        self._at = 0.0

    async def get(self, http: httpx.AsyncClient, force: bool = False) -> Any:
        if force or self._keys is None or time.monotonic() - self._at > 3600:
            r = await http.get(JWKS_URL, timeout=10)
            r.raise_for_status()
            self._keys, self._at = KeySet.import_key_set(r.json()), time.monotonic()
        return self._keys


def _rate_limit(state: Any, req: Any) -> None:
    if not state.limiter.allow(f"login:{client_ip(req)}", 10, 60):
        raise falcon.HTTPTooManyRequests(title="Too many sign-in attempts; wait a minute")


def _redirect(resp: Any, location: str) -> None:
    # Set on resp (not raised) so cookies set on this response are kept.
    resp.status = falcon.HTTP_303
    resp.location = location


def _fail(resp: Any, reason: str) -> None:
    resp.unset_cookie(OAUTH_COOKIE, path="/")
    _redirect(resp, f"/login/?error={reason}")


class Login:
    policy = Policy.PUBLIC

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_get(self, req: Any, resp: Any) -> None:
        s = self.state
        _rate_limit(s, req)
        if not s.settings.google_client_id:
            raise falcon.HTTPServiceUnavailable(title="Google OAuth is not configured",
                                                description="set GOOGLE_OAUTH_CLIENT_ID/SECRET in .env")
        st, nonce, verifier = generate_token(32), generate_token(32), generate_token(64)
        params = {
            "response_type": "code", "client_id": s.settings.google_client_id,
            # Fixed from config, never derived from the Host header.
            "redirect_uri": s.settings.google_redirect_uri,
            "scope": "openid email profile", "state": st, "nonce": nonce, "prompt": "select_account",
            "code_challenge": s256(verifier), "code_challenge_method": "S256",
        }
        cookie = s.signer.pack("oauth", {"state": st, "nonce": nonce, "verifier": verifier, "iat": int(time.time())})
        resp.set_cookie(OAUTH_COOKIE, cookie, max_age=OAUTH_TTL, http_only=True, same_site="Lax",
                        secure=s.settings.cookie_secure, path="/")
        _redirect(resp, f"{AUTH_URL}?{urlencode(params)}")


class Callback:
    policy = Policy.PUBLIC

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_get(self, req: Any, resp: Any) -> None:
        s = self.state
        _rate_limit(s, req)
        flow = s.signer.unpack("oauth", req.cookies.get(OAUTH_COOKIE))
        age = time.time() - (flow or {}).get("iat", 0)
        # Login CSRF defence: state must match the value bound to this browser.
        if (not flow or not -60 < age < OAUTH_TTL or not req.get_param("code")
                or not secrets.compare_digest(req.get_param("state") or "", flow["state"])):
            s.db.audit("login_failed", detail="bad or expired oauth state", ip=client_ip(req))
            return _fail(resp, "oauth")
        try:
            claims = await self._verify(req.get_param("code"), flow)
        except (httpx.HTTPError, JoseError, KeyError, ValueError) as exc:
            log.warning("OAuth verification failed: %s", exc)
            s.db.audit("login_failed", detail=f"token verification: {type(exc).__name__}", ip=client_ip(req))
            return _fail(resp, "oauth")

        email = str(claims.get("email") or "").lower()
        if not email or claims.get("email_verified") is not True:
            s.db.audit("login_failed", detail=f"unverified email {email}", ip=client_ip(req))
            return _fail(resp, "unverified")
        user = self._admit(email)
        if user is None:
            s.db.audit("login_denied", target=email, detail="not invited", ip=client_ip(req))
            return _fail(resp, "not_invited")

        name = str(claims.get("name") or "")[:200]
        s.db.mark_login(user["id"], name, "admin" if email == s.settings.bootstrap_admin_email else None)
        raw, cookie = s.signer.new_session_token()
        ttl = int(s.settings.session_hours * 3600)
        s.db.create_session(raw, user["id"], secrets.token_urlsafe(32), ttl)
        s.db.audit("login", actor=user, ip=client_ip(req))
        resp.unset_cookie(OAUTH_COOKIE, path="/")
        resp.set_cookie(SESSION_COOKIE, cookie, max_age=ttl, http_only=True, same_site="Lax",
                        secure=s.settings.cookie_secure, path="/")
        _redirect(resp, "/")

    def _admit(self, email: str) -> dict[str, Any] | None:
        user = self.state.db.get_user_by_email(email)
        if user is None and email == self.state.settings.bootstrap_admin_email:
            user = self.state.db.invite_user(email, "admin", None, {})
        return user

    async def _verify(self, code: str, flow: dict[str, Any]) -> dict[str, Any]:
        """Code -> tokens over TLS, then validate the ID token: RS256 signature
        against Google's JWKS, iss, aud == our client id, exp/iat (60 s leeway)
        and the nonce bound to this browser's login attempt."""
        st = self.state.settings
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": st.google_redirect_uri,
                "client_id": st.google_client_id, "client_secret": st.google_client_secret,
                "code_verifier": flow["verifier"]})
            r.raise_for_status()
            id_token = r.json()["id_token"]
            keys = await self.state.google_keys.get(http)
            try:
                token = jwt.decode(id_token, keys, algorithms=["RS256"])
            except JoseError:  # maybe Google rotated keys: refresh once
                token = jwt.decode(id_token, await self.state.google_keys.get(http, force=True), algorithms=["RS256"])
        JWTClaimsRegistry(
            leeway=60,
            iss={"essential": True, "values": ISSUERS},
            aud={"essential": True, "value": st.google_client_id},
            nonce={"essential": True, "value": flow["nonce"]},
            exp={"essential": True},
            iat={"essential": True},
        ).validate(token.claims)
        return dict(token.claims)


class Logout:
    policy = Policy.USER

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_post(self, req: Any, resp: Any) -> None:
        """Deletes the server-side session, so the cookie is dead everywhere;
        open terminals/streams of this session close at their next check."""
        self.state.db.delete_session(req.context.user.token)
        audit(req, "logout")
        resp.unset_cookie(SESSION_COOKIE, path="/")
        resp.status = falcon.HTTP_204
