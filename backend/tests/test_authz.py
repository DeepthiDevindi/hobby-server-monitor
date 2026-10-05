"""Authorization: users see only what they were given; admin routes are admin-only."""
from __future__ import annotations

import pytest
from falcon import testing

from hsm.web.policy import AuthMiddleware, Policy, check_routes, policy_for
from .conftest import ORIGIN, login
from .test_authn import METHODS, concrete


# ---- container users ----------------------------------------------------------
@pytest.mark.parametrize("path", ["/api/containers/secret", "/api/containers/secret/history?range=1h",
                                  "/api/containers/nope"])
def test_user_gets_403_for_unassigned_or_unknown_container(alice, path):
    # Same answer whether it exists or not: names cannot be probed.
    assert alice.get(path).status_code == 403


def test_user_sees_assigned_container(alice):
    assert alice.get("/api/containers/test1").status_code == 200
    assert alice.get("/api/containers/test1/history?range=1h").status_code == 200


def test_list_contains_only_assigned(alice):
    names = [c["name"] for c in alice.get("/api/containers").json["containers"]]
    assert names == ["test1"]


def test_user_cannot_exec_on_unassigned(alice, lxd):
    assert alice.post("/api/containers/secret/exec", json={"command": "id"}).status_code == 403
    assert lxd.execs == []


def test_user_can_exec_on_assigned(alice, lxd):
    r = alice.post("/api/containers/test1/exec", json={"command": "echo hello"})
    assert r.status_code == 200 and r.json["stdout"] == "hello\n"
    name, argv = lxd.execs[0]
    # argv list with a timeout wrapper; the command is one argument, never spliced.
    assert name == "test1" and argv[:4] == ["timeout", "-s", "KILL", "30"] and argv[-1] == "echo hello"


def test_owner_has_access_without_assignment(client, admin):
    bob = login(client, "bob@example.com")
    client.app.state.db.set_owner("uuid-secret", bob.id)
    assert bob.get("/api/containers/secret").status_code == 200


def test_assignment_never_grants_lifecycle_control(alice):
    assert alice.post("/api/containers/test1/state", json={"action": "stop"}).status_code == 403
    assert alice.delete("/api/containers/test1").status_code == 403
    assert alice.patch("/api/containers/test1", json={"cpus": 2}).status_code == 403


def test_unassign_demote_and_revoke_take_effect_immediately(client, alice, admin):
    db = client.app.state.db
    db.unassign(alice.id, "uuid-test1")
    assert alice.get("/api/containers/test1").status_code == 403

    boss2 = login(client, "boss2@example.com", "admin")
    assert boss2.get("/api/admin/users").status_code == 200
    db.set_role(boss2.id, "user")  # role is re-read on every request
    assert boss2.get("/api/admin/users").status_code == 403

    assert admin.delete(f"/api/admin/users/{alice.id}").status_code == 204
    assert alice.get("/api/me").status_code == 401  # sessions die with the user


def test_user_cannot_self_promote(alice):
    assert alice.patch(f"/api/admin/users/{alice.id}", json={"role": "admin"}).status_code == 403
    assert alice.get("/api/me").json["role"] == "user"


# ---- admin-only routes --------------------------------------------------------
def admin_routes(app):
    for path, res in app.route_table:
        for m in METHODS:
            if hasattr(res, f"on_{m.lower()}") and policy_for(res, m) is Policy.ADMIN:
                yield m, concrete(path)


def test_non_admin_gets_403_on_every_admin_route(alice, lxd):
    routes = list(admin_routes(alice.client.app))
    assert len(routes) >= 15
    before = {k: dict(v) for k, v in lxd.instances.items()}
    for method, path in routes:
        r = alice.req(method, path, json={"name": "x1", "image": "ubuntu/24.04", "cpus": 1, "memory_mib": 256,
                                          "pool": "fast", "disk_gib": 4, "action": "stop", "role": "admin",
                                          "email": "e@x.io", "owner_id": alice.id})
        assert r.status_code == 403, (method, path, r.status_code)
    assert {k: dict(v) for k, v in lxd.instances.items()} == before and lxd.created == []


# ---- the safety net itself ---------------------------------------------------
class Unprotected:
    async def on_get(self, req, resp):
        resp.media = {"secret": True}


def test_startup_refuses_a_route_without_policy():
    with pytest.raises(RuntimeError, match="without an authorization policy"):
        check_routes([("/api/new-thing", Unprotected())])


def test_middleware_denies_a_responder_without_policy(app):
    app.add_route("/api/forgotten", Unprotected())  # bypassing check_routes on purpose
    r = testing.TestClient(app).simulate_get("/api/forgotten")
    assert r.status_code == 500 and "secret" not in r.text


def test_partial_policy_dict_denies_unlisted_methods(app):
    class Half:
        policy = {"GET": Policy.USER}

        async def on_get(self, req, resp):
            resp.media = {}

        async def on_delete(self, req, resp):
            resp.media = {"deleted": True}

    app.add_route("/api/half", Half())
    u = login(testing.TestClient(app), "boss@example.com", "admin")
    assert u.delete("/api/half").status_code == 500


def test_ws_origin_is_checked_in_middleware():
    assert hasattr(AuthMiddleware, "process_resource_ws")
    assert ORIGIN  # behaviour covered in test_terminal.py
