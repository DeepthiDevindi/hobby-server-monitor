"""Shared metrics cache + SSE streams.

A single poller task exists only while at least one SSE client is connected.
Each tick makes ONE LXD call (`/1.0/instances?recursion=2`) regardless of how
many viewers there are; every subscriber gets the same snapshot, filtered by
its own (re-checked) permissions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import StreamingResponse

from .dependencies import CurrentUser, can_access, require_container_access, require_user, resolve_user
from .schemas import NAME_PATTERN

log = logging.getLogger("hsm.metrics")
router = APIRouter(prefix="/api")
KEEPALIVE_SECONDS = 20


def _net_totals(network: dict[str, Any] | None) -> tuple[int, int]:
    rx = tx = 0
    for ifname, nic in (network or {}).items():
        if ifname == "lo":
            continue
        c = nic.get("counters") or {}
        rx += c.get("bytes_received", 0)
        tx += c.get("bytes_sent", 0)
    return rx, tx


class MetricsHub:
    def __init__(self, lxd: Any, interval: float) -> None:
        self.lxd = lxd
        self.interval = interval
        self.polls = 0  # number of LXD calls made by the poller
        self._subs: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._latest: dict[str, Any] | None = None
        self._latest_at = 0.0
        self._prev: dict[str, tuple[float, int, int, int]] = {}  # name -> (t, cpu_ns, rx, tx)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subs.add(q)
        if self._latest and time.monotonic() - self._latest_at < self.interval:
            q.put_nowait(self._latest)  # fresh cached snapshot: no extra LXD call
        if not self.running:
            self._task = asyncio.create_task(self._run(), name="metrics-poller")
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)
        if not self._subs and self._task:
            self._task.cancel()  # last viewer left: stop touching LXD entirely
            self._task = None

    async def stop(self) -> None:
        self._subs.clear()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while self._subs:
            snapshot = await self._poll()
            for q in list(self._subs):
                if q.full():
                    q.get_nowait()  # slow consumer: keep only the newest snapshot
                q.put_nowait(snapshot)
            await asyncio.sleep(self.interval)

    async def _poll(self) -> dict[str, Any]:
        self.polls += 1
        try:
            instances = await self.lxd.list_instances(with_state=True)
        except Exception as exc:  # LXD down must not kill the poller
            log.warning("metrics poll failed: %s", exc)
            return {"ts": time.time(), "error": "LXD unavailable", "containers": {}}
        now = time.monotonic()
        containers = {i["name"]: self._compute(i, now) for i in instances}
        self._prev = {k: v for k, v in self._prev.items() if k in containers}
        self._latest, self._latest_at = {"ts": time.time(), "containers": containers}, now
        return self._latest

    def _compute(self, inst: dict[str, Any], now: float) -> dict[str, Any]:
        st = inst.get("state") or {}
        cpu_ns = (st.get("cpu") or {}).get("usage", 0)
        mem = st.get("memory") or {}
        root = (st.get("disk") or {}).get("root") or {}
        rx, tx = _net_totals(st.get("network"))
        out = {
            "status": inst.get("status"),
            "cpu_pct": None, "rx_bps": None, "tx_bps": None,
            "mem_used": mem.get("usage", 0), "mem_total": mem.get("total", 0),
            # `dir` storage pools report 0 (no quota accounting) -> shown as n/a.
            "disk_used": root.get("usage", 0), "disk_total": root.get("total", 0),
            "processes": st.get("processes", 0),
        }
        prev = self._prev.get(inst["name"])
        if prev and now > prev[0]:
            dt = now - prev[0]
            # Percent of ONE core (like `top`); 200% = two cores busy.
            out["cpu_pct"] = round(max(0, cpu_ns - prev[1]) / (dt * 1e9) * 100, 1)
            out["rx_bps"] = round(max(0, rx - prev[2]) / dt)
            out["tx_bps"] = round(max(0, tx - prev[3]) / dt)
        self._prev[inst["name"]] = (now, cpu_ns, rx, tx)
        return out


Selector = Callable[[Request, CurrentUser, dict[str, Any]], dict[str, Any] | None]


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def _stream(request: Request, select: Selector) -> AsyncIterator[str]:
    hub: MetricsHub = request.app.state.metrics
    q = hub.subscribe()
    try:
        yield f"retry: 5000\n\n"
        while True:
            try:
                snap = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            # Re-check session and permissions on every tick: logout, expiry,
            # demotion or unassignment take effect within one interval.
            user = resolve_user(request)
            if user is None:
                yield _sse("expired", {})
                return
            data = select(request, user, snap)
            if data is None:
                yield _sse("forbidden", {})
                return
            yield _sse("metrics", data)
    finally:
        hub.unsubscribe(q)


def _response(gen: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def _select_all(request: Request, user: CurrentUser, snap: dict[str, Any]) -> dict[str, Any]:
    items = snap["containers"]
    if not user.is_admin:
        allowed = request.app.state.db.assigned_containers(user.id)
        items = {k: v for k, v in items.items() if k in allowed}
    return {"ts": snap["ts"], "error": snap.get("error"), "containers": items}


@router.get("/metrics/stream")
async def stream_all(request: Request, _: CurrentUser = Depends(require_user)) -> StreamingResponse:
    return _response(_stream(request, _select_all))


@router.get("/containers/{name}/metrics/stream")
async def stream_one(
    request: Request, name: str = Path(pattern=NAME_PATTERN), _: CurrentUser = Depends(require_container_access)
) -> StreamingResponse:
    def select(req: Request, user: CurrentUser, snap: dict[str, Any]) -> dict[str, Any] | None:
        if not can_access(req, user, name):
            return None
        return {"ts": snap["ts"], "error": snap.get("error"),
                "containers": {name: snap["containers"][name]} if name in snap["containers"] else {}}

    return _response(_stream(request, select))
