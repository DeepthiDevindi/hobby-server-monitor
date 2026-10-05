"""Admin: users (invite, role, quota, revoke), container access, audit log;
plus usage accounting and the caller's own quota."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import falcon

from .. import quota, tsdb
from .common import audit, body, lxd, require_name
from .policy import Policy
from .schemas import Invite, UserUpdate

PERIODS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def _user_view(u: dict[str, Any]) -> dict[str, Any]:
    q = quota.quota_of(u)
    return {k: u.get(k) for k in ("id", "email", "name", "role", "status", "created_at", "last_login_at",
                                   "containers", "allocated")} | {"quota": q}


class Users:
    policy = Policy.ADMIN

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_get(self, req: Any, resp: Any) -> None:
        resp.media = [_user_view(u) for u in self.state.db.list_users()]

    async def on_post(self, req: Any, resp: Any) -> None:
        """Invite: only invited emails can sign in (plus the bootstrap admin)."""
        data = await body(req, Invite)
        db = self.state.db
        if db.get_user_by_email(data.email):
            raise falcon.HTTPConflict(title="That email is already invited")
        user = db.invite_user(data.email, data.role, req.context.user.id, data.quota.model_dump())
        audit(req, "user_invite", user["email"], f"role={data.role} quota={data.quota.model_dump()}")
        resp.status = falcon.HTTP_201
        resp.media = _user_view(user)


class User:
    policy = Policy.ADMIN

    def __init__(self, state: Any) -> None:
        self.state = state

    def _target(self, user_id: str) -> dict[str, Any]:
        target = self.state.db.get_user(_int_id(user_id))
        if target is None:
            raise falcon.HTTPNotFound(title="User not found")
        return target

    def _protect(self, req: Any, target: dict[str, Any]) -> None:
        """No self-demotion/self-revoke, and the bootstrap admin is config-managed."""
        if target["id"] == req.context.user.id:
            raise falcon.HTTPBadRequest(title="You cannot change your own role or revoke yourself")
        if target["email"] == self.state.settings.bootstrap_admin_email:
            raise falcon.HTTPBadRequest(title="The bootstrap admin is managed via BOOTSTRAP_ADMIN_EMAIL")

    async def on_patch(self, req: Any, resp: Any, user_id: str) -> None:
        data = await body(req, UserUpdate)
        target = self._target(user_id)
        if data.role is not None and data.role != target["role"]:
            self._protect(req, target)
            self.state.db.set_role(target["id"], data.role)
            audit(req, "user_role", target["email"], f"{target['role']} -> {data.role}")
        if data.quota is not None:
            q = data.quota
            self.state.db.set_quota(target["id"], q.cpus, q.memory_mib, q.disk_gib)
            audit(req, "user_quota", target["email"], f"{quota.quota_of(target)} -> {q.model_dump()}")
        resp.media = _user_view(next(u for u in self.state.db.list_users() if u["id"] == target["id"]))

    async def on_delete(self, req: Any, resp: Any, user_id: str) -> None:
        """Revoke entirely: user row, sessions and grants go; owned containers
        become unowned (they keep running). Open terminals/streams close at
        their next re-check (<= 5 s)."""
        target = self._target(user_id)
        self._protect(req, target)
        self.state.db.delete_user(target["id"])
        audit(req, "user_revoke", target["email"])
        resp.status = falcon.HTTP_204


class Grant:
    policy = Policy.ADMIN

    def __init__(self, state: Any) -> None:
        self.state = state

    def _resolve(self, user_id: str, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        user = self.state.db.get_user(_int_id(user_id))
        row = self.state.db.container_by_name(require_name(name))
        if user is None or row is None:
            raise falcon.HTTPNotFound(title="User or container not found")
        return user, row

    async def on_put(self, req: Any, resp: Any, user_id: str, name: str) -> None:
        user, row = self._resolve(user_id, name)
        self.state.db.assign(user["id"], row["uuid"], req.context.user.id)
        audit(req, "access_grant", name, f"user={user['email']}")
        resp.status = falcon.HTTP_204

    async def on_delete(self, req: Any, resp: Any, user_id: str, name: str) -> None:
        user, row = self._resolve(user_id, name)
        if not self.state.db.unassign(user["id"], row["uuid"]):
            raise falcon.HTTPNotFound(title="Not assigned")
        audit(req, "access_revoke", name, f"user={user['email']}")
        resp.status = falcon.HTTP_204


class AuditLog:
    policy = Policy.ADMIN

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_get(self, req: Any, resp: Any) -> None:
        limit = req.get_param_as_int("limit", min_value=1, max_value=500) or 100
        before = req.get_param_as_int("before", min_value=1)
        resp.media = self.state.db.list_audit(limit, before)


class Usage:
    """Server-wide accounting: host capacity vs allocation, per-user totals
    vs quota, per-container consumption over a period (from the TSDB)."""
    policy = Policy.ADMIN

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_get(self, req: Any, resp: Any) -> None:
        s = self.state
        period = req.get_param("period") or "24h"
        if period not in PERIODS:
            raise falcon.HTTPUnprocessableEntity(title=f"period must be one of {', '.join(PERIODS)}")
        host, pools = await asyncio.gather(lxd(s.lxd.host_resources), lxd(s.lxd.storage_pools))
        rows = s.db.list_containers()
        allocated = {k: sum(r[k] or 0 for r in rows) for k in quota.RESOURCES}
        unlimited = [r["name"] for r in rows if r["cpus"] is None or r["memory_mib"] is None]
        end = time.time()
        start = end - PERIODS[period]
        per_container = await asyncio.gather(*[
            asyncio.to_thread(tsdb.consumption, s.settings.tinyflux_dir, r["uuid"], start, end,
                              s.settings.retention) for r in rows])
        owners = {u["id"]: u["email"] for u in s.db.all("SELECT id, email FROM users")}
        resp.media = {
            "period": period,
            "host": {"cpus": host["cpus"], "memory_mib": host["memory_bytes"] // 2**20,
                     "pools": [{**p, "total_gib": round(p["total_bytes"] / 2**30, 1),
                                "used_gib": round(p["used_bytes"] / 2**30, 1)} for p in pools]},
            "allocated": allocated,
            "unlimited_containers": unlimited,
            "users": [_user_view(u) for u in s.db.list_users()],
            "containers": [{"name": r["name"], "owner": owners.get(r["owner_id"]), "status": r["status"],
                            "allocated": {k: r[k] for k in quota.RESOURCES}, **c}
                           for r, c in zip(rows, per_container)],
            "tsdb": tsdb.disk_usage(s.settings.tinyflux_dir),
        }


class Me:
    """Who am I, my CSRF token, and my quota vs allocation (both roles)."""
    policy = Policy.USER

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_get(self, req: Any, resp: Any) -> None:
        u = req.context.user
        row = self.state.db.get_user(u.id)
        alloc = self.state.db.allocation_of(u.id)
        resp.media = {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "csrf": u.csrf,
                      "session_expires": u.expires_at, "quota": quota.quota_of(row),
                      "allocated": alloc, "remaining": quota.remaining(row, alloc)}


def _int_id(value: str) -> int:
    if not value.isdigit() or not 0 < int(value) < 2**31:
        raise falcon.HTTPNotFound(title="User not found")
    return int(value)
