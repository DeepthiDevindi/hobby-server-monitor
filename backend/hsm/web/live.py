"""Live dashboard updates over Server-Sent Events.

The collector already polls LXD every 10 s and publishes latest.json. Here a
single watcher task stats that file once a second, and only while at least
one browser is connected; each new snapshot is fanned out to every
subscriber. So N open tabs cost N small writes, not N LXD calls, and zero
browsers cost nothing at all in this process.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator

from falcon.asgi import SSEvent

from .common import snapshot
from .policy import Policy, resolve_user
from .security import SESSION_COOKIE

KEEPALIVE_S = 15


class LiveHub:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._mtime = 0.0
        self.reads = 0  # snapshots read from disk (for tests / observability)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.subscribers.add(q)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._watch())
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
        if not self.subscribers and self._task:
            self._task.cancel()
            self._task = None

    async def stop(self) -> None:
        self.subscribers.clear()
        if self._task:
            self._task.cancel()

    async def _watch(self) -> None:
        path = self.state.settings.latest_path
        first = True
        while self.subscribers:
            try:
                mtime = os.stat(path).st_mtime
            except FileNotFoundError:
                mtime = 0.0
            # Publish on change, and once per keepalive so "collector down" shows.
            if first or mtime != self._mtime:
                first = False
                self._mtime = mtime
                self.reads += 1
                snap = snapshot(self.state)
                for q in list(self.subscribers):
                    if q.full():
                        q.get_nowait()  # slow tab: keep only the newest
                    q.put_nowait(snap)
            await asyncio.sleep(1)


def _visible(state: Any, user: Any, snap: dict[str, Any]) -> dict[str, Any]:
    items = snap.get("containers") or {}
    if not user.is_admin:
        allowed = state.db.accessible_names(user.id)
        items = {k: v for k, v in items.items() if k in allowed}
    meta = {k: snap.get(k) for k in ("ts", "age_s", "lxd_ok", "error", "last_ok", "collector_ok", "interval")}
    return {"meta": meta, "containers": items}


class LiveStream:
    policy = Policy.USER

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_get(self, req: Any, resp: Any) -> None:
        resp.set_header("X-Accel-Buffering", "no")
        resp.sse = self._events(req.cookies.get(SESSION_COOKIE))

    async def _events(self, cookie: str | None) -> AsyncIterator[SSEvent | None]:
        hub: LiveHub = self.state.live
        q = hub.subscribe()
        try:
            yield SSEvent(retry=5000)
            while True:
                try:
                    snap = await asyncio.wait_for(q.get(), KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield None  # Falcon sends an empty event = keepalive
                    continue
                # Re-check the session on every push: logout, expiry, revocation
                # and unassignment take effect within one update.
                user = resolve_user(self.state, cookie, touch=False)
                if user is None:
                    yield SSEvent(event="expired", data=b"{}")
                    return
                yield SSEvent(event="snapshot", json=_visible(self.state, user, snap))
        finally:
            hub.unsubscribe(q)
