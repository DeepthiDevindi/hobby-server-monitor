"""Server-side input validation (nothing from the form is trusted)."""
from __future__ import annotations

import pytest

BAD_NAMES = ["Test1", "a_b", "a b", "a" * 64, "a.b", "a;rm", "ü", "a$(id)", "%2e%2e"]


def create(admin, **kw):
    body = {"name": "ok-1", "image": "ubuntu/24.04", "cpus": 1, "memory_mib": 256, "pool": "fast", "disk_gib": 4} | kw
    return admin.post("/api/containers", json=body)


@pytest.mark.parametrize("name", BAD_NAMES + ["", "-lead", "trail-", "1abc"])
def test_create_rejects_invalid_names(admin, lxd, name):
    assert create(admin, name=name).status_code == 422
    assert lxd.created == []


@pytest.mark.parametrize("name", BAD_NAMES)
def test_path_rejects_invalid_names(admin, alice, name):
    for who in (admin, alice):
        for method, path in [("GET", f"/api/containers/{name}"), ("DELETE", f"/api/containers/{name}"),
                             ("GET", f"/api/containers/{name}/history"), ("POST", f"/api/containers/{name}/exec")]:
            r = who.req(method, path, json={"command": "id"})
            # admin: invalid input (or no route); user: may also be refused as non-admin first
            allowed = (404, 422) if who is admin else (403, 404, 422)
            assert r.status_code in allowed, (who.user["email"], method, name, r.status_code)


@pytest.mark.parametrize("kw,why", [
    ({"image": "images:alpine/edge"}, "not allowlisted"),
    ({"cpus": 5}, "host has 4 CPUs"),
    ({"cpus": 0}, "min 1"),
    ({"memory_mib": 64}, "below minimum"),
    ({"memory_mib": 9 * 1024}, "host has 8 GiB"),
    ({"disk_gib": 19}, "pool has 18 GiB free"),
    ({"disk_gib": 1}, "below minimum disk"),
    ({"pool": "nope"}, "unknown pool"),
    ({"network": "evil0"}, "unknown network"),
    ({"profiles": ["privileged"]}, "unknown profile"),
    ({"cpu_allowance": 1}, "allowance too low"),
    ({"privileged": True}, "unknown field"),
    ({"description": "a\x1b[2J"}, "control chars"),
    ({"cpus": "1; reboot"}, "type"),
])
def test_create_bounds_are_enforced_server_side(admin, lxd, kw, why):
    assert create(admin, **kw).status_code == 422, why
    assert lxd.created == []


def test_disk_size_required_on_sizable_pool_and_ignored_on_dir(admin, lxd):
    assert create(admin, disk_gib=None).status_code == 422
    assert create(admin, name="on-dir", pool="default", disk_gib=None).status_code == 202
    assert "size" not in lxd.created[-1]["devices"]["root"]


def test_valid_create_builds_a_safe_lxd_request(admin, lxd):
    r = create(admin, name="web-1", cpus=2, memory_mib=1024, disk_gib=8, cpu_allowance=50,
               network="lxdbr0", autostart=True, ephemeral=True, description="test box")
    assert r.status_code == 202, r.text
    body = lxd.created[-1]
    cfg = body["config"]
    assert cfg["security.privileged"] == "false" and cfg["security.nesting"] == "false"
    assert cfg["limits.cpu"] == "2" and cfg["limits.memory"] == "1024MiB" and cfg["limits.processes"] == "2000"
    assert cfg["limits.cpu.allowance"] == "100ms/100ms" and cfg["boot.autostart"] == "true"
    assert body["devices"]["root"] == {"type": "disk", "path": "/", "pool": "fast", "size": "8GiB"}
    assert body["devices"]["eth0"]["network"] == "lxdbr0" and body["ephemeral"] is True
    assert body["source"]["alias"] == "24.04"


def test_duplicate_name_is_409(admin):
    assert create(admin, name="test1").status_code == 409


def test_bad_state_role_email_and_exec(admin, alice):
    assert admin.post("/api/containers/test1/state", json={"action": "explode"}).status_code == 422
    assert admin.patch("/api/admin/users/1", json={"role": "root"}).status_code == 422
    assert admin.post("/api/admin/users", json={"email": "not-an-email"}).status_code == 422
    assert alice.post("/api/containers/test1/exec", json={"command": "x" * 5000}).status_code == 422
    assert alice.post("/api/containers/test1/exec", json={"command": ""}).status_code == 422
    assert alice.get("/api/containers/test1/history?range=99y").status_code == 422


def test_oversized_and_malformed_bodies(admin):
    r = admin.post("/api/containers", body=b"{" * 70000, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = admin.post("/api/containers", body=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code in (400, 422)
