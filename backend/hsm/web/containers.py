"""Container endpoints: list/detail, create (async job), limits, state, owner,
delete, history and one-shot command execution."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

import falcon

from .. import quota, tsdb
from ..lxd import parse_size
from .common import audit, body, lxd, require_name, snapshot
from .policy import Policy
from .schemas import ContainerCreate, ExecRequest, LimitsUpdate, OwnerUpdate, StateAction

log = logging.getLogger("hsm.containers")
RANGES = {"15m": 900, "1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def _row_view(state: Any, row: dict[str, Any], live: dict[str, Any] | None, admin: bool) -> dict[str, Any]:
    """Merge the DB row (ownership, allocation) with the live sample."""
    owner = state.db.get_user(row["owner_id"]) if row and row.get("owner_id") else None
    out = dict(live or {"name": row["name"], "status": row.get("status") or "Unknown", "uuid": row["uuid"]})
    out.update({
        "owner": owner["email"] if owner else None,
        "owner_id": owner["id"] if owner else None,
        "allocated": {"cpus": row.get("cpus"), "memory_mib": row.get("memory_mib"), "disk_gib": row.get("disk_gib")},
        "pending": live is None,  # recorded but not seen by the collector yet
    })
    if admin:
        out["assigned"] = [r["email"] for r in state.db.all(
            "SELECT u.email FROM assignments a JOIN users u ON u.id = a.user_id WHERE a.container_uuid = ?",
            (row["uuid"],))]
    return out


class Containers:
    policy = {"GET": Policy.USER, "POST": Policy.ADMIN}

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_get(self, req: Any, resp: Any) -> None:
        user = req.context.user
        snap = snapshot(self.state)
        rows = self.state.db.list_containers()
        if not user.is_admin:
            allowed = self.state.db.accessible_names(user.id)
            rows = [r for r in rows if r["name"] in allowed]
        live = snap.get("containers") or {}
        resp.media = {
            "meta": {k: snap.get(k) for k in ("ts", "age_s", "lxd_ok", "error", "last_ok", "collector_ok", "interval")},
            "containers": [_row_view(self.state, r, live.get(r["name"]), user.is_admin) for r in rows],
            "jobs": [j for j in self.state.jobs.values() if j["status"] == "running"] if user.is_admin else [],
        }

    async def on_post(self, req: Any, resp: Any) -> None:
        data = await body(req, ContainerCreate)
        s = self.state
        source = s.settings.images.get(data.image)
        if source is None:
            raise falcon.HTTPUnprocessableEntity(title="Image is not in the allowlist")
        if s.db.container_by_name(data.name) or data.name in {j["name"] for j in s.jobs.values() if j["status"] == "running"}:
            raise falcon.HTTPConflict(title=f"A container named {data.name} already exists")
        owner = s.db.get_user(data.owner_id or req.context.user.id)
        if owner is None:
            raise falcon.HTTPUnprocessableEntity(title="Owner does not exist")

        opts = await _live_options(s)
        pool = next((p for p in opts["pools"] if p["name"] == data.pool), None)
        if pool is None:
            raise falcon.HTTPUnprocessableEntity(title=f"Unknown storage pool {data.pool}")
        if data.network and data.network not in {n["name"] for n in opts["networks"]}:
            raise falcon.HTTPUnprocessableEntity(title=f"Unknown network {data.network}")
        if not set(data.profiles) <= set(opts["profiles"]):
            raise falcon.HTTPUnprocessableEntity(title="Unknown profile")
        disk = data.disk_gib if pool["sizable"] else None
        if pool["sizable"] and disk is None:
            raise falcon.HTTPUnprocessableEntity(title=f"disk_gib is required for pool {pool['name']}")

        async with s.quota_lock:  # check + reserve atomically (single web process)
            alloc = _allocated_with_pending(s, owner["id"])
            b = quota.bounds(opts["host"], pool, quota.remaining(owner, alloc), s.settings.min_memory_mib,
                             s.settings.min_disk_gib)
            _within(b, data.cpus, data.memory_mib, disk)
            _check_quota(owner, alloc, data.cpus, data.memory_mib, disk)
            job = _new_job(s, data.name, owner["id"], data.cpus, data.memory_mib, disk)

        config = {
            "limits.cpu": str(data.cpus),
            "limits.memory": f"{data.memory_mib}MiB",
            "limits.processes": "2000",            # fork-bomb guard
            "security.privileged": "false",        # never a privileged container
            "security.nesting": "false",           # no LXD/Docker inside
            "boot.autostart": "true" if data.autostart else "false",
            "user.hsm.owner_id": str(owner["id"]),  # lets the collector recover ownership
        }
        if data.cpu_allowance < 100:
            # Hard cap: N% of each allocated core, e.g. 2 cores at 50% = 100ms per 100ms.
            config["limits.cpu.allowance"] = f"{data.cpus * data.cpu_allowance}ms/100ms"
        devices: dict[str, Any] = {"root": {"type": "disk", "path": "/", "pool": data.pool}}
        if disk:
            devices["root"]["size"] = f"{disk}GiB"
        if data.network:
            devices["eth0"] = {"type": "nic", "network": data.network, "name": "eth0"}
        lxd_body = {"name": data.name, "type": "container", "description": data.description,
                    "ephemeral": data.ephemeral, "profiles": data.profiles, "config": config,
                    "devices": devices, "source": {"type": "image", "mode": "pull", **source}}
        try:
            job["op"] = await lxd(s.lxd.begin_create, lxd_body)
        except falcon.HTTPError:
            job["status"] = "failed"
            raise
        audit(req, "container_create_started", data.name,
              f"{data.image} cpus={data.cpus} mem={data.memory_mib}MiB disk={disk} owner={owner['email']}")
        job["task"] = asyncio.create_task(_finish_create(s, job, req.context.user, data.start))
        resp.status = falcon.HTTP_202
        resp.media = _job_view(job)


def _within(b: dict[str, Any], cpus: int, memory_mib: int, disk: float | None) -> None:
    """Server-side re-validation of every slider (never trust the form)."""
    for key, val in (("cpus", cpus), ("memory_mib", memory_mib), ("disk_gib", disk)):
        rng = b.get(key)
        if rng is None or val is None:
            continue
        if not rng["min"] <= val <= rng["max"]:
            raise falcon.HTTPUnprocessableEntity(
                title=f"{key} must be between {rng['min']} and {rng['max']}",
                description="bounded by host capacity and the owner's remaining quota")


def _check_quota(owner: dict[str, Any], alloc: dict[str, float], cpus: int, mem: int, disk: float | None) -> None:
    try:
        quota.check(owner, alloc, quota.Request(cpus, mem, disk))
    except quota.QuotaExceeded as exc:
        raise falcon.HTTPConflict(title="Quota exceeded", description=str(exc))


def _allocated_with_pending(s: Any, owner_id: int, exclude_uuid: str | None = None) -> dict[str, float]:
    alloc = s.db.allocation_of(owner_id, exclude_uuid)
    for j in s.jobs.values():  # reservations for creates still in flight
        if j["status"] == "running" and j["owner_id"] == owner_id:
            for k in quota.RESOURCES:
                alloc[k] = (alloc.get(k) or 0) + (j["reserved"][k] or 0)
    return alloc


def _new_job(s: Any, name: str, owner_id: int, cpus: int, mem: int, disk: float | None) -> dict[str, Any]:
    if len(s.jobs) > 50:  # keep memory bounded: drop finished jobs
        for jid in [k for k, j in s.jobs.items() if j["status"] != "running"][:25]:
            s.jobs.pop(jid, None)
    job = {"id": secrets.token_hex(8), "name": name, "owner_id": owner_id, "status": "running",
           "started": time.time(), "error": None, "op": None,
           "reserved": {"cpus": cpus, "memory_mib": mem, "disk_gib": disk}}
    s.jobs[job["id"]] = job
    return job


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {k: job[k] for k in ("id", "name", "status", "started", "error")}


async def _finish_create(s: Any, job: dict[str, Any], user: Any, start: bool) -> None:
    """Poll the LXD operation, then record ownership and optionally start."""
    try:
        while True:
            op = await asyncio.to_thread(s.lxd.operation, job["op"])
            if op["status"] in ("Success", "Failure", "Cancelled"):
                break
            await asyncio.sleep(2)
        if op["status"] != "Success":
            raise RuntimeError(op.get("err") or op["status"])
        inst = await asyncio.to_thread(s.lxd.instance, job["name"])
        r = job["reserved"]
        s.db.record_container(inst["config"]["volatile.uuid"], job["name"], job["owner_id"], user.id,
                              r["cpus"], r["memory_mib"], r["disk_gib"])
        if start:
            await asyncio.to_thread(s.lxd.set_state, job["name"], "start")
        job["status"] = "done"
        s.db.audit("container_create", actor=user.actor(), target=job["name"], detail=f"job={job['id']}")
    except Exception as exc:  # report, don't crash the server
        log.warning("create %s failed: %s", job["name"], exc)
        job["status"], job["error"] = "failed", str(exc)[:300]
        s.db.audit("container_create_failed", actor=user.actor(), target=job["name"], detail=job["error"])


async def _live_options(s: Any) -> dict[str, Any]:
    host, pools, networks, profiles = await asyncio.gather(
        lxd(s.lxd.host_resources), lxd(s.lxd.storage_pools), lxd(s.lxd.networks), lxd(s.lxd.profiles))
    return {"host": host, "pools": pools, "networks": networks, "profiles": profiles}


class CreateOptions:
    """Everything the create form may offer, discovered at runtime."""
    policy = Policy.ADMIN

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_get(self, req: Any, resp: Any) -> None:
        s = self.state
        owner = s.db.get_user(req.get_param_as_int("owner_id") or req.context.user.id)
        if owner is None:
            raise falcon.HTTPNotFound(title="Owner not found")
        opts = await _live_options(s)
        cached = await lxd(s.lxd.cached_image_descriptions)
        rem = quota.remaining(owner, _allocated_with_pending(s, owner["id"]))
        resp.media = {
            **opts,
            "images": [{"key": k, "cached": any(k.split("/")[1] in d and k.split("/")[0] in d.lower() for d in cached)}
                       for k in s.settings.images],
            "owner": {"id": owner["id"], "email": owner["email"], "remaining": rem},
            "bounds": {p["name"]: quota.bounds(opts["host"], p, rem, s.settings.min_memory_mib,
                                               s.settings.min_disk_gib) for p in opts["pools"]},
            "users": [{"id": u["id"], "email": u["email"]} for u in s.db.list_users()],
        }


class Job:
    policy = Policy.ADMIN

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_get(self, req: Any, resp: Any, job_id: str) -> None:
        job = self.state.jobs.get(job_id)
        if job is None:
            raise falcon.HTTPNotFound(title="Unknown job")
        resp.media = _job_view(job)


class Container:
    policy = {"GET": Policy.CONTAINER, "PATCH": Policy.ADMIN, "DELETE": Policy.ADMIN}

    def __init__(self, state: Any) -> None:
        self.state = state

    def _row(self, name: str) -> dict[str, Any]:
        row = self.state.db.container_by_name(require_name(name))
        if row is None:
            raise falcon.HTTPNotFound(title="Container not found")
        return row

    async def on_get(self, req: Any, resp: Any, name: str) -> None:
        row = self._row(name)
        snap = snapshot(self.state)
        resp.media = _row_view(self.state, row, (snap.get("containers") or {}).get(name), req.context.user.is_admin)

    async def on_patch(self, req: Any, resp: Any, name: str) -> None:
        """Change limits (also on a running container), within host + quota."""
        data = await body(req, LimitsUpdate)
        s = self.state
        row = self._row(name)
        inst = await lxd(s.lxd.instance, name)
        cfg = inst.get("config") or {}
        root = (inst.get("expanded_devices") or {}).get("root") or {}
        cpus = data.cpus or int(cfg.get("limits.cpu") or 1)
        mem = data.memory_mib or (parse_size(cfg.get("limits.memory")) // 2**20 or s.settings.min_memory_mib)
        disk = data.disk_gib if data.disk_gib is not None else (parse_size(root.get("size")) / 2**30 or None)
        opts = await _live_options(s)
        pool = next((p for p in opts["pools"] if p["name"] == root.get("pool")), None)
        if data.disk_gib is not None and not (pool and pool["sizable"]):
            raise falcon.HTTPUnprocessableEntity(title="This storage pool cannot enforce a disk size")
        owner = s.db.get_user(row["owner_id"]) if row["owner_id"] else None
        async with s.quota_lock:
            alloc = _allocated_with_pending(s, owner["id"], row["uuid"]) if owner else {}
            rem = quota.remaining(owner, alloc) if owner else {k: None for k in quota.RESOURCES}
            b = quota.bounds(opts["host"], pool, rem, s.settings.min_memory_mib, s.settings.min_disk_gib)
            if pool and pool["sizable"] and b["disk_gib"]:
                # Growing only needs free space for the *increase*.
                b["disk_gib"]["max"] += int(row.get("disk_gib") or 0)
            _within(b, cpus, mem, disk if data.disk_gib is not None else None)
            if owner:
                _check_quota(owner, alloc, cpus, mem, disk)
            new_cfg = {"limits.cpu": str(cpus), "limits.memory": f"{mem}MiB"}
            if data.cpu_allowance is not None:
                new_cfg["limits.cpu.allowance"] = "" if data.cpu_allowance == 100 else f"{cpus * data.cpu_allowance}ms/100ms"
            await lxd(s.lxd.update_limits, name, new_cfg, data.disk_gib)
            s.db.update_limits(row["uuid"], cpus, mem, disk)
        audit(req, "container_limits", name,
              f"cpus {row['cpus']}->{cpus}, mem {row['memory_mib']}->{mem}MiB, disk {row['disk_gib']}->{disk}GiB"
              + (f", allowance {data.cpu_allowance}%" if data.cpu_allowance is not None else ""))
        resp.media = {"name": name, "cpus": cpus, "memory_mib": mem, "disk_gib": disk}

    async def on_delete(self, req: Any, resp: Any, name: str) -> None:
        row = self._row(name)
        await lxd(self.state.lxd.delete, name)
        self.state.db.forget_container(row["uuid"])  # assignments cascade
        audit(req, "container_delete", name, f"uuid={row['uuid']}")
        resp.status = falcon.HTTP_204


class ContainerState:
    policy = Policy.ADMIN

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_post(self, req: Any, resp: Any, name: str) -> None:
        data = await body(req, StateAction)
        await lxd(self.state.lxd.set_state, require_name(name), data.action)
        st = await lxd(self.state.lxd.state, name)
        self.state.db.run("UPDATE containers SET status = ? WHERE name = ?", (st.get("status"), name))
        audit(req, f"container_{data.action}", name)
        resp.media = {"name": name, "status": st.get("status")}


class ContainerOwner:
    policy = Policy.ADMIN

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_put(self, req: Any, resp: Any, name: str) -> None:
        data = await body(req, OwnerUpdate)
        s = self.state
        row = s.db.container_by_name(require_name(name))
        if row is None:
            raise falcon.HTTPNotFound(title="Container not found")
        owner = s.db.get_user(data.owner_id) if data.owner_id else None
        if data.owner_id and owner is None:
            raise falcon.HTTPUnprocessableEntity(title="Owner does not exist")
        async with s.quota_lock:
            if owner:
                if row["cpus"] is None or row["memory_mib"] is None:
                    raise falcon.HTTPConflict(title="Set CPU and memory limits before giving it an owner",
                                              description="an unlimited container cannot be counted against a quota")
                _check_quota(owner, _allocated_with_pending(s, owner["id"], row["uuid"]),
                             row["cpus"], row["memory_mib"], row["disk_gib"])
            await lxd(s.lxd.set_user_key, name, "user.hsm.owner_id", str(owner["id"]) if owner else "")
            s.db.set_owner(row["uuid"], owner["id"] if owner else None)
        audit(req, "container_owner", name, f"owner={owner['email'] if owner else None}")
        resp.media = {"name": name, "owner": owner["email"] if owner else None}


class History:
    policy = Policy.CONTAINER

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_get(self, req: Any, resp: Any, name: str) -> None:
        rng = req.get_param("range") or "1h"
        if rng not in RANGES:
            raise falcon.HTTPUnprocessableEntity(title=f"range must be one of {', '.join(RANGES)}")
        row = self.state.db.container_by_name(name)
        if row is None:
            raise falcon.HTTPNotFound(title="Container not found")
        s = self.state.settings
        data = await asyncio.to_thread(tsdb.history, s.tinyflux_dir, row["uuid"], RANGES[rng], s.retention)
        resp.media = {"name": name, "range": rng, **data}


class Exec:
    """Run ONE command in the container and return its output (pylxd exec).
    The command runs *inside* the container as root via /bin/sh -c; that is
    the feature. Host safety comes from container isolation (unprivileged,
    no nesting, process/memory/CPU limits), not from filtering the text."""
    policy = Policy.CONTAINER

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_post(self, req: Any, resp: Any, name: str) -> None:
        data = await body(req, ExecRequest)
        user = req.context.user
        if not self.state.limiter.allow(f"exec:{user.id}", 20, 60):
            raise falcon.HTTPTooManyRequests(title="Too many commands; wait a minute")
        if "\x00" in data.command:
            raise falcon.HTTPUnprocessableEntity(title="NUL bytes are not allowed")
        t = self.state.settings.exec_timeout_seconds
        # argv list, no host shell. `timeout` (coreutils/busybox) kills runaway commands.
        argv = ["timeout", "-s", "KILL", str(t), "/bin/sh", "-c", data.command]
        started = time.monotonic()
        result = await lxd(self.state.lxd.execute, name, argv, 64 * 1024)
        took = round(time.monotonic() - started, 2)
        audit(req, "exec", name, f"exit={result['exit_code']} {took}s: {data.command[:200]}")
        resp.media = {**result, "duration_s": took, "timed_out": result["exit_code"] == 137 and took >= t - 1}
