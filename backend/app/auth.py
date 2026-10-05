"""Google OAuth 2.0 / OpenID Connect login."""
from __future__ import annotations

import logging
import secrets

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from .dependencies import CurrentUser, require_user
from .security import SESSION_COOKIE, client_ip

log = logging.getLogger("hsm.auth")
router = APIRouter()
GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"


def build_oauth(client_id: str, client_secret: str) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=GOOGLE_METADATA,
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


def _rate_limit(request: Request) -> None:
    if not request.app.state.limiter.allow(f"login:{client_ip(request)}", limit=10, window=60):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts")


def _fail(reason: str) -> RedirectResponse:
    return RedirectResponse(f"/login/?error={reason}", status_code=303)


@router.get("/auth/login", dependencies=[Depends(_rate_limit)])
async def login(request: Request) -> Response:
    settings = request.app.state.settings
    if not settings.google_client_id:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google OAuth is not configured")
    # Fixed redirect URI from config; never derived from the Host header.
    redirect_uri = f"{settings.public_origin}/auth/callback"
    # authlib stores state + nonce in the short-lived signed oauth cookie.
    return await request.app.state.oauth.google.authorize_redirect(
        request, redirect_uri, prompt="select_account"
    )


@router.get("/auth/callback", dependencies=[Depends(_rate_limit)])
async def callback(request: Request) -> Response:
    app = request.app.state
    try:
        # Exchanges the code, then validates the ID token: JWKS signature,
        # iss, aud == our client id, exp, and the nonce bound to this browser.
        token = await app.oauth.google.authorize_access_token(request)
    except (OAuthError, ValueError) as exc:
        log.warning("OAuth callback rejected: %s", exc)
        app.db.audit("login_failed", detail="oauth error", ip=client_ip(request))
        return _fail("oauth")
    finally:
        request.session.clear()
    claims = token.get("userinfo") or {}
    email = str(claims.get("email") or "").lower()
    if not email or claims.get("email_verified") is not True:
        app.db.audit("login_failed", detail=f"unverified email {email}", ip=client_ip(request))
        return _fail("unverified")

    user = app.db.record_login(email, str(claims.get("name") or "")[:200], app.settings.bootstrap_admin_email)
    raw, cookie = app.signer.new_token()
    ttl = int(app.settings.session_hours * 3600)
    app.db.create_session(raw, user["id"], secrets.token_urlsafe(32), ttl)
    app.db.audit("login", actor=user, detail=f"role={user['role']}", ip=client_ip(request))

    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, cookie, max_age=ttl, httponly=True, samesite="lax",
        secure=app.settings.cookie_secure, path="/",
    )
    return resp


@router.post("/api/auth/logout", status_code=204)
async def logout(request: Request, user: CurrentUser = Depends(require_user)) -> Response:
    request.app.state.db.delete_session(user.token)
    request.app.state.db.audit("logout", actor={"id": user.id, "email": user.email}, ip=client_ip(request))
    resp = Response(status_code=204)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/api/me")
async def me(user: CurrentUser = Depends(require_user)) -> dict:
    # The CSRF token is readable by same-origin JS only (CORS is not enabled).
    return {
        "id": user.id, "email": user.email, "name": user.name, "role": user.role,
        "csrf": user.csrf, "session_expires": user.session_expires,
    }
