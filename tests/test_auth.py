"""Authentication: 401s everywhere, sessions, CSRF, OAuth callback rules."""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.websockets import WebSocketDisconnect

from app.security import SESSION_COOKIE
from .conftest import ORIGIN, hdr, login

PUBLIC = {"/auth/login", "/auth/callback"}


def _concrete(path: str) -> str:
    return path.replace("{name}", "test1").replace("{user_id}", "1")


def _http_routes(app):
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path not in PUBLIC:
            for m in r.methods:
                yield m, _concrete(r.path)


def test_every_api_route_requires_auth(client):
    routes = list(_http_routes(client.app))
    assert len(routes) >= 15  # sanity: the introspection actually found the API
    for method, path in routes:
        r = client.request(method, path, headers={"Origin": ORIGIN}, json={})
        assert r.status_code == 401, (method, path, r.status_code)


def test_websocket_requires_auth(client, lxd):
    ws_routes = [r for r in client.app.routes if isinstance(r, APIWebSocketRoute)]
    assert ws_routes
    for r in ws_routes:
        with client.websocket_connect(_concrete(r.path), headers={"Origin": ORIGIN}) as ws:
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_bytes()
            assert e.value.code == 4401
    assert lxd.execs == []


def test_forged_or_tampered_cookie_is_401(client):
    client.cookies.set(SESSION_COOKIE, "abc.def.ghi")
    assert client.get("/api/me").status_code == 401


def test_valid_signature_but_unknown_session_is_401(client):
    _, cookie = client.app.state.signer.new_token()  # signed, but no DB row
    client.cookies.set(SESSION_COOKIE, cookie)
    assert client.get("/api/me").status_code == 401


def test_expired_session_is_401(client):
    login(client, "u@example.com", ttl=-1)
    assert client.get("/api/me").status_code == 401


def test_me_and_logout_revokes_session(client):
    h = login(client, "u@example.com")
    me = client.get("/api/me").json()
    assert me["email"] == "u@example.com" and me["role"] == "user" and me["csrf"]
    cookie = client.cookies.get(SESSION_COOKIE)
    assert client.post("/api/auth/logout", headers=hdr(h)).status_code == 204
    client.cookies.set(SESSION_COOKIE, cookie)  # replaying the old cookie must fail
    assert client.get("/api/me").status_code == 401


def test_csrf_required_on_state_changes(client):
    h = login(client, "boss@example.com", "admin")
    body = {"action": "stop"}
    assert client.post("/api/containers/test1/actions", json=body).status_code == 403
    bad = {"X-CSRF-Token": "nope", "Origin": ORIGIN}
    assert client.post("/api/containers/test1/actions", json=body, headers=bad).status_code == 403
    evil = {**hdr(h), "Origin": "https://evil.example"}
    assert client.post("/api/containers/test1/actions", json=body, headers=evil).status_code == 403
    assert client.post("/api/containers/test1/actions", json=body, headers=hdr(h)).status_code == 200


# ---- OAuth callback ---------------------------------------------------------
def _fake_google(monkeypatch, app, claims):
    async def fake(request):
        return {"access_token": "t", "userinfo": claims}
    monkeypatch.setattr(app.state.oauth.google, "authorize_access_token", fake)


def test_callback_rejects_unverified_email(client, monkeypatch):
    _fake_google(monkeypatch, client.app, {"email": "x@example.com", "email_verified": False})
    r = client.get("/auth/callback", follow_redirects=False)
    assert r.status_code == 303 and "error=unverified" in r.headers["location"]
    assert SESSION_COOKIE not in r.cookies
    assert client.app.state.db.get_user_by_email("x@example.com") is None


def test_callback_bootstrap_admin_and_plain_user(client, monkeypatch):
    _fake_google(monkeypatch, client.app, {"email": "Boss@Example.com", "email_verified": True, "name": "B"})
    r = client.get("/auth/callback", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie and "max-age=" in set_cookie
    assert client.get("/api/me").json()["role"] == "admin"

    client.cookies.clear()
    _fake_google(monkeypatch, client.app, {"email": "new@example.com", "email_verified": True})
    client.get("/auth/callback", follow_redirects=False)
    me = client.get("/api/me").json()
    assert me["role"] == "user"
    assert client.get("/api/containers").json() == []  # no access until assigned


def test_login_is_rate_limited(client, monkeypatch):
    from starlette.responses import RedirectResponse

    async def fake_redirect(request, uri, **kw):
        assert uri == f"{ORIGIN}/auth/callback"  # fixed from config, not Host header
        return RedirectResponse("https://accounts.google.com/o/oauth2/auth")
    monkeypatch.setattr(client.app.state.oauth.google, "authorize_redirect", fake_redirect)
    codes = [client.get("/auth/login", follow_redirects=False).status_code for _ in range(12)]
    assert 429 in codes


def test_security_headers(client):
    r = client.get("/api/me")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
