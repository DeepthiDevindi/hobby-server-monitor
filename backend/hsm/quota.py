"""Quotas and capacity bounds.

A quota caps what a user's containers may *allocate* (sum of limits.cpu,
limits.memory and root disk size over the containers they own), not what
they happen to use right now. Allocation is what is reserved on the host and
is stable, so a user can't be pushed over quota by a load spike.

When a request would cross the quota it is refused (HTTP 409) with the
numbers; nothing that already runs is stopped or shrunk. If an admin lowers a
quota below the current allocation, the user is shown as over quota and can
only shrink or delete until back under.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RESOURCES = ("cpus", "memory_mib", "disk_gib")


@dataclass(frozen=True)
class Request:
    cpus: int
    memory_mib: int
    disk_gib: float | None  # None: pool cannot enforce a size (dir)


class QuotaExceeded(Exception):
    def __init__(self, resource: str, used: float, requested: float, limit: float) -> None:
        self.resource, self.used, self.requested, self.limit = resource, used, requested, limit
        super().__init__(f"{resource}: {used:g} allocated + {requested:g} requested > quota {limit:g}")


def quota_of(user: dict[str, Any]) -> dict[str, int | None]:
    return {"cpus": user["quota_cpus"], "memory_mib": user["quota_memory_mib"], "disk_gib": user["quota_disk_gib"]}


def remaining(user: dict[str, Any], allocated: dict[str, float]) -> dict[str, float | None]:
    """None = unlimited."""
    q = quota_of(user)
    return {r: None if q[r] is None else max(0.0, q[r] - (allocated.get(r) or 0)) for r in RESOURCES}


def check(user: dict[str, Any], allocated: dict[str, float], req: Request) -> None:
    """Raise QuotaExceeded if `req` (the new totals of ONE container, with that
    container's old allocation already excluded from `allocated`) won't fit."""
    q = quota_of(user)
    wanted = {"cpus": req.cpus, "memory_mib": req.memory_mib, "disk_gib": req.disk_gib or 0}
    for r in RESOURCES:
        if q[r] is not None and (allocated.get(r) or 0) + wanted[r] > q[r]:
            raise QuotaExceeded(r, allocated.get(r) or 0, wanted[r], q[r])


def bounds(host: dict[str, Any], pool: dict[str, Any] | None, rem: dict[str, float | None],
           min_memory_mib: int, min_disk_gib: int) -> dict[str, Any]:
    """Slider bounds for the create/limits form: min(host capacity, quota left)."""
    def cap(host_max: float, left: float | None) -> float:
        return host_max if left is None else min(host_max, left)

    host_mem_mib = host["memory_bytes"] // 2**20
    out = {
        "cpus": {"min": 1, "max": int(cap(host["cpus"], rem["cpus"]))},
        "memory_mib": {"min": min_memory_mib, "max": int(cap(host_mem_mib, rem["memory_mib"]))},
        "disk_gib": None,
    }
    if pool and pool.get("sizable"):
        free_gib = (pool["total_bytes"] - pool["used_bytes"]) / 2**30
        out["disk_gib"] = {"min": min_disk_gib, "max": int(cap(free_gib, rem["disk_gib"]))}
    return out
