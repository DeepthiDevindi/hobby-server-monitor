"""Quotas: what they measure, and what happens at the limit."""
from __future__ import annotations

import asyncio

from falcon import testing

from hsm import quota
from hsm.web.security import SESSION_COOKIE
from .conftest import ORIGIN, login


def body(**kw):
    return {"name": "q-1", "image": "ubuntu/24.04", "cpus": 1, "memory_mib": 512, "pool": "fast", "disk_gib": 4} | kw


async def test_create_within_quota_then_refused_at_quota(app, client, admin):
    """Runs on Falcon's async conductor so the background create job completes,
    as it does under uvicorn."""
    bob = login(client, "bob@example.com", quota={"cpus": 2, "memory_mib": 1024, "disk_gib": 10})
    headers = {"Origin": ORIGIN, "X-CSRF-Token": admin.csrf}
    cookies = {SESSION_COOKIE: admin.cookie}
    async with testing.ASGIConductor(app) as c:
        r = await c.simulate_post("/api/containers", json=body(owner_id=bob.id), headers=headers, cookies=cookies)
        assert r.status_code == 202
        job = app.state.jobs[r.json["id"]]
        for _ in range(100):
            if job["status"] != "running":
                break
            await asyncio.sleep(0.02)
        assert job["status"] == "done", job
        # 512 allocated + 768 requested > 1024 quota: refused, nothing created.
        r = await c.simulate_post("/api/containers", json=body(name="q-2", memory_mib=768, owner_id=bob.id),
                                  headers=headers, cookies=cookies)
        assert r.status_code == 422 and "memory_mib must be between" in r.json["title"]
        me = (await c.simulate_get("/api/me", cookies={SESSION_COOKIE: bob.cookie})).json
    assert me["allocated"] == {"cpus": 1, "memory_mib": 512, "disk_gib": 4.0}
    assert me["remaining"] == {"cpus": 1, "memory_mib": 512, "disk_gib": 6.0}
    assert client.app.state.db.can_access(bob.id, "q-1")  # owner sees their container


def test_form_bounds_reflect_remaining_quota(client, admin):
    bob = login(client, "bob@example.com", quota={"cpus": 3, "memory_mib": 2048, "disk_gib": 6})
    opts = admin.get(f"/api/containers/options?owner_id={bob.id}").json
    assert opts["bounds"]["fast"]["cpus"] == {"min": 1, "max": 3}            # quota < host (4)
    assert opts["bounds"]["fast"]["memory_mib"]["max"] == 2048               # quota < host (8 GiB)
    assert opts["bounds"]["fast"]["disk_gib"]["max"] == 6                    # quota < pool free (18)
    assert opts["bounds"]["default"]["disk_gib"] is None                     # dir pool: no size
    assert {"name": "lxdbr0", "type": "bridge"} in opts["networks"]


def test_unlimited_owner_is_bounded_by_host(client, admin):
    opts = admin.get("/api/containers/options").json  # admin has no quota
    assert opts["bounds"]["fast"]["cpus"]["max"] == 4 and opts["bounds"]["fast"]["disk_gib"]["max"] == 18


def test_limit_increase_checked_against_quota_decrease_always_allowed(client, admin):
    db = client.app.state.db
    bob = login(client, "bob@example.com", quota={"cpus": 2, "memory_mib": 1024, "disk_gib": 10})
    db.set_owner("uuid-test1", bob.id)  # test1 allocates 1 cpu / 512 MiB / 4 GiB
    r = admin.patch("/api/containers/test1", json={"memory_mib": 2048})
    assert r.status_code == 422  # over the 1024 MiB quota
    assert admin.patch("/api/containers/test1", json={"memory_mib": 1024}).status_code == 200
    db.set_quota(bob.id, 1, 256, 10)  # admin lowers the quota below current use
    assert admin.patch("/api/containers/test1", json={"memory_mib": 256}).status_code == 200  # shrinking is fine
    me = bob.get("/api/me").json
    assert me["allocated"]["memory_mib"] == 256


def test_owner_change_respects_new_owners_quota(client, admin):
    tiny = login(client, "tiny@example.com", quota={"cpus": 1, "memory_mib": 128, "disk_gib": 1})
    r = admin.put("/api/containers/test1/owner", json={"owner_id": tiny.id})
    assert r.status_code == 409 and "quota" in r.json["title"].lower()


async def test_concurrent_creates_cannot_overshoot(client, admin, app):
    """Reservation under one lock: in-flight creates count against the quota."""
    bob = login(client, "bob@example.com", quota={"cpus": 4, "memory_mib": 1024, "disk_gib": 20})
    s = app.state
    # Simulate one create still running (reserved 768 MiB).
    async with s.quota_lock:
        from hsm.web.containers import _allocated_with_pending, _new_job
        _new_job(s, "in-flight", bob.id, 1, 768, 4)
        alloc = _allocated_with_pending(s, bob.id)
    assert alloc["memory_mib"] == 768
    r = await asyncio.to_thread(admin.post, "/api/containers", json=body(owner_id=bob.id))
    assert r.status_code == 422  # 768 reserved + 512 > 1024


def test_quota_check_unit():
    user = {"quota_cpus": 4, "quota_memory_mib": 4096, "quota_disk_gib": None}
    quota.check(user, {"cpus": 3, "memory_mib": 2048}, quota.Request(1, 2048, 999))  # exactly at quota: ok
    try:
        quota.check(user, {"cpus": 3, "memory_mib": 2048}, quota.Request(2, 512, None))
    except quota.QuotaExceeded as exc:
        assert exc.resource == "cpus"
    else:
        raise AssertionError("expected QuotaExceeded")


def test_usage_accounting(client, admin):
    bob = login(client, "bob@example.com", quota={"cpus": 4, "memory_mib": 4096, "disk_gib": 40})
    client.app.state.db.set_owner("uuid-test1", bob.id)
    u = admin.get("/api/admin/usage?period=24h").json
    assert u["host"]["cpus"] == 4 and u["host"]["memory_mib"] == 8192
    assert u["allocated"] == {"cpus": 2, "memory_mib": 1024, "disk_gib": 8.0}
    row = next(x for x in u["users"] if x["email"] == "bob@example.com")
    assert row["allocated"]["memory_mib"] == 512 and row["quota"]["memory_mib"] == 4096
    assert {c["name"] for c in u["containers"]} == {"test1", "secret"}
