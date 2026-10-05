"""Authentication: 401 everywhere, sessions, CSRF, Google OIDC + invite-only."""
from __future__ import annotations

import re
import time

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from hsm.web import auth
from hsm.web.policy import Policy, policy_for
from hsm.web.security import OAUTH_COOKIE, SESSION_COOKIE
from .conftest import ORIGIN, login

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def concrete(path: str) -> str:
    return re.sub(r"\{(\w+)(:\w+)?\}", lambda m: {"name": "test1", "user_id": "1", "job_id": "x"}.get(m[1], "x"), path)


def protected_http_routes(app):
    """(method, path) for every responder whose policy is not PUBLIC."""
    for path, res in app.route_table:
        for m in METHODS:
            if hasattr(res, f"on_{m.lower()}") and policy_for(res, m) not in (Policy.PUBLIC, None):
                yield m, concrete(path)


def test_every_protected_route_needs_a_session(client):
    routes = list(protected_http_routes(client.app))
    assert len(routes) >= 20  # sanity: introspection found the API
    for method, path in routes:
        r = client.simulate_request(method, path, headers={"Origin": ORIGIN}, json={})
        assert r.status_code == 401, (method, path, r.status_code)


def test_public_routes_are_only_login_and_static(client):
    public = {p for p, res in client.app.route_table if any(
        policy_for(res, m) is Policy.PUBLIC for m in METHODS)}
    assert public == {"/auth/login", "/auth/google/callback", "/", "/{path:path}"}


def test_tampered_and_unknown_cookies_are_401(client):
    assert client.simulate_get("/api/me", cookies={SESSION_COOKIE: "abc.def"}).status_code == 401
    _, signed_but_unknown = client.app.state.signer.new_session_token()
    assert client.simulate_get("/api/me", cookies={SESSION_COOKIE: signed_but_unknown}).status_code == 401


def test_expired_and_idle_sessions_are_401(client):
    assert login(client, "a@example.com", ttl=-1).get("/api/me").status_code == 401
    u = login(client, "b@example.com")
    client.app.state.db.run("UPDATE sessions SET last_seen_at = ?", (int(time.time()) - 2 * 3600,))
    assert u.get("/api/me").status_code == 401  # idle timeout (60 min default)


def test_logout_kills_the_session_server_side(client):
    u = login(client, "u@example.com")
    assert u.get("/api/me").json["email"] == "u@example.com"
    assert u.post("/api/auth/logout").status_code == 204
    assert u.get("/api/me").status_code == 401  # replaying the old cookie fails


def test_csrf_and_origin_required_for_writes(client, admin):
    body = {"action": "stop"}
    assert admin.post("/api/containers/test1/state", json=body, csrf=False).status_code == 403
    assert admin.post("/api/containers/test1/state", json=body, headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert admin.post("/api/containers/test1/state", json=body,
                      headers={"Origin": "https://evil.example"}).status_code == 403
    assert admin.post("/api/containers/test1/state", json=body).status_code == 200


def test_security_headers(client):
    h = client.simulate_get("/api/me").headers
    assert "script-src 'self'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "same-origin" and h["cache-control"] == "no-store"


# ---- Google OIDC --------------------------------------------------------------
KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"})
OTHER_KEY = RSAKey.generate_key(2048, parameters={"kid": "k1"})


def _start_login(client):
    r = client.simulate_get("/auth/login")
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith(auth.AUTH_URL)
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fauth%2Fgoogle%2Fcallback" in loc  # from config, not Host
    assert "code_challenge_method=S256" in loc
    state = re.search(r"state=([^&]+)", loc)[1]
    cookie = r.cookies[OAUTH_COOKIE]
    assert cookie.http_only and cookie.same_site.lower() == "lax"
    flow = client.app.state.signer.unpack("oauth", cookie.value)
    return state, cookie.value, flow


def _id_token(flow, key=KEY, **over):
    now = int(time.time())
    claims = {"iss": "https://accounts.google.com", "aud": "cid", "sub": "123", "iat": now, "exp": now + 600,
              "nonce": flow["nonce"], "email": "new@example.com", "email_verified": True, "name": "New"} | over
    return jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key)


def _callback(client, monkeypatch, id_token, state, cookie):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == auth.TOKEN_URL:
            assert b"code_verifier=" in request.content  # PKCE sent
            return httpx.Response(200, json={"access_token": "at", "id_token": id_token})
        if request.url == auth.JWKS_URL:
            return httpx.Response(200, json=KeySet([KEY]).as_dict(private=False))
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    return client.simulate_get("/auth/google/callback", params={"code": "c", "state": state},
                               cookies={OAUTH_COOKIE: cookie})


def test_invited_user_can_sign_in(client, monkeypatch):
    client.app.state.db.invite_user("new@example.com", "user", None, {})
    state, cookie, flow = _start_login(client)
    r = _callback(client, monkeypatch, _id_token(flow), state, cookie)
    assert r.status_code == 303 and r.headers["location"] == "/"
    c = r.cookies[SESSION_COOKIE]
    assert c.http_only and c.same_site.lower() == "lax" and c.max_age > 0
    me = client.simulate_get("/api/me", cookies={SESSION_COOKIE: c.value}).json
    assert me["email"] == "new@example.com" and me["role"] == "user"


def test_uninvited_account_is_refused_without_creating_a_row(client, monkeypatch):
    state, cookie, flow = _start_login(client)
    r = _callback(client, monkeypatch, _id_token(flow), state, cookie)
    assert r.headers["location"] == "/login/?error=not_invited"
    assert SESSION_COOKIE not in r.cookies
    assert client.app.state.db.get_user_by_email("new@example.com") is None


def test_bootstrap_admin_gets_in_as_admin(client, monkeypatch):
    state, cookie, flow = _start_login(client)
    r = _callback(client, monkeypatch, _id_token(flow, email="Boss@Example.com"), state, cookie)
    me = client.simulate_get("/api/me", cookies={SESSION_COOKIE: r.cookies[SESSION_COOKIE].value}).json
    assert me["role"] == "admin"


@pytest.mark.parametrize("bad", [
    {"key": OTHER_KEY},                       # forged signature
    {"aud": "someone-else"},                  # token minted for another app
    {"nonce": "replayed"},                    # not bound to this login
    {"iss": "https://evil.example"},
    {"exp": int(time.time()) - 3600},         # expired
    {"email_verified": False},
])
def test_bad_id_tokens_are_rejected(client, monkeypatch, bad):
    client.app.state.db.invite_user("new@example.com", "user", None, {})
    state, cookie, flow = _start_login(client)
    key = bad.pop("key", KEY)
    r = _callback(client, monkeypatch, _id_token(flow, key=key, **bad), state, cookie)
    assert r.status_code == 303 and "/login/?error=" in r.headers["location"]
    assert SESSION_COOKIE not in r.cookies


def test_state_mismatch_is_rejected(client, monkeypatch):
    client.app.state.db.invite_user("new@example.com", "user", None, {})
    state, cookie, flow = _start_login(client)
    r = _callback(client, monkeypatch, _id_token(flow), "attacker-state", cookie)
    assert r.headers["location"] == "/login/?error=oauth"


def test_login_is_rate_limited(client):
    codes = [client.simulate_get("/auth/login").status_code for _ in range(12)]
    assert codes[:10] == [303] * 10 and codes[-1] == 429
