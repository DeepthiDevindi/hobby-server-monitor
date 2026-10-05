"""Authorization: container users see only their assigned containers; only
admins reach admin endpoints."""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from starlette.websockets import WebSocketDisconnect

from app.dependencies import require_admin, require_admin_container
from .conftest import ORIGIN, hdr, login


@pytest.fixture
def user(client):
    h = login(client, "alice@example.com", "user")
    client.app.state.db.assign(int(h["_uid"]), "test1")
    return h


# ---- Container User vs. unassigned container --------------------------------
def test_user_can_view_assigned_but_not_unassigned(client, user):
    assert client.get("/api/containers/test1").status_code == 200
    assert client.get("/api/containers/secret").status_code == 403
    # Non-existent names get the same 403, so a user cannot probe what exists.
    assert client.get("/api/containers/nope").status_code == 403


def test_list_only_shows_assigned(client, user):
    assert [c["name"] for c in client.get("/api/containers").json()] == ["test1"]


def test_user_cannot_stream_unassigned(client, user):
    r = client.get("/api/containers/secret/metrics/stream")
    assert r.status_code == 403
    assert client.app.state.metrics.subscribers == 0  # never subscribed


def test_user_cannot_open_terminal_on_unassigned(client, user, lxd):
    with client.websocket_connect("/api/containers/secret/terminal", headers={"Origin": ORIGIN}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_bytes()
    assert e.value.code == 4403
    assert lxd.execs == []  # LXD exec was never reached


def test_user_cannot_act_on_assigned_container(client, user):
    # Assignment grants view + terminal only, never lifecycle control.
    assert client.post("/api/containers/test1/actions", json={"action": "stop"}, headers=hdr(user)).status_code == 403
    assert client.delete("/api/containers/test1", headers=hdr(user)).status_code == 403


def test_unassign_takes_effect_immediately(client, user):
    client.app.state.db.unassign(int(user["_uid"]), "test1")
    assert client.get("/api/containers/test1").status_code == 403


def test_demotion_takes_effect_immediately(client):
    login(client, "boss2@example.com", "admin")
    assert client.get("/api/admin/users").status_code == 200
    uid = client.app.state.db.get_user_by_email("boss2@example.com")["id"]
    client.app.state.db.set_role(uid, "user")  # role is re-read every request
    assert client.get("/api/admin/users").status_code == 403


# ---- Non-admin vs. admin endpoints ------------------------------------------
def _admin_routes(app):
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        deps = {d.call for d in r.dependant.dependencies}
        if deps & {require_admin, require_admin_container}:
            for m in r.methods:
                yield m, r.path.replace("{name}", "test1").replace("{user_id}", "1")


def test_non_admin_gets_403_on_every_admin_endpoint(client, user, lxd):
    routes = list(_admin_routes(client.app))
    assert len(routes) >= 12
    before = dict(lxd.instances)
    for method, path in routes:
        r = client.request(method, path, headers=hdr(user),
                           json={"name": "x1", "image": "ubuntu/24.04", "cpus": 1, "memory_mib": 256,
                                 "action": "stop", "role": "admin", "email": "e@x.io"})
        # Dependencies run before body validation, so the answer is always 403.
        assert r.status_code == 403, (method, path, r.status_code)
    assert lxd.instances == before


def test_user_cannot_self_promote(client, user):
    r = client.patch(f"/api/admin/users/{user['_uid']}", json={"role": "admin"}, headers=hdr(user))
    assert r.status_code == 403
    assert client.get("/api/me").json()["role"] == "user"


# ---- Admin happy paths ------------------------------------------------------
@pytest.fixture
def admin(client):
    return login(client, "boss@example.com", "admin")


def test_admin_crud_and_audit(client, admin, lxd):
    h = hdr(admin)
    r = client.post("/api/containers", json={"name": "web-1", "image": "ubuntu/24.04", "cpus": 2, "memory_mib": 512}, headers=h)
    assert r.status_code == 201, r.text
    assert lxd.instances["web-1"]["config"]["limits.memory"] == "512MiB"
    assert lxd.instances["web-1"]["config"]["security.privileged"] == "false"
    assert client.post("/api/containers/web-1/actions", json={"action": "start"}, headers=h).json()["status"] == "Running"
    assert client.patch("/api/containers/web-1", json={"cpus": 3}, headers=h).json()["cpus"] == "3"
    assert client.delete("/api/containers/web-1", headers=h).status_code == 204
    assert "web-1" not in lxd.instances
    actions = [e["action"] for e in client.get("/api/admin/audit").json()]
    assert {"container_create", "container_start", "container_update", "container_delete"} <= set(actions)


def test_admin_assign_unassign(client, admin):
    h = hdr(admin)
    uid = client.post("/api/admin/users", json={"email": "bob@example.com"}, headers=h).json()["id"]
    assert client.put(f"/api/admin/users/{uid}/containers/secret", headers=h).status_code == 204
    assert client.put(f"/api/admin/users/{uid}/containers/ghost", headers=h).status_code == 404  # no orphan grant
    users = {u["email"]: u for u in client.get("/api/admin/users").json()}
    assert users["bob@example.com"]["containers"] == ["secret"]
    assert client.delete(f"/api/admin/users/{uid}/containers/secret", headers=h).status_code == 204
    assert client.app.state.db.assigned_containers(uid) == set()


def test_admin_cannot_demote_self_or_bootstrap(client, admin):
    h = hdr(admin)
    assert client.patch(f"/api/admin/users/{admin['_uid']}", json={"role": "user"}, headers=h).status_code == 400
    other = login(client, "other-admin@example.com", "admin")
    boss_id = client.app.state.db.get_user_by_email("boss@example.com")["id"]
    assert client.patch(f"/api/admin/users/{boss_id}", json={"role": "user"}, headers=hdr(other)).status_code == 400


def test_delete_container_drops_assignments(client, admin):
    db = client.app.state.db
    bob = db.create_user("bob@example.com")
    db.assign(bob["id"], "secret")
    assert client.delete("/api/containers/secret", headers=hdr(admin)).status_code == 204
    assert db.assigned_containers(bob["id"]) == set()
