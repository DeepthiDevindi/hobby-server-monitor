"""Live updates: cost independent of open tabs; per-user filtering; revocation."""
from __future__ import annotations

import asyncio

from hsm.web.live import LiveHub
from .conftest import login, publish


async def collect(gen, n):
    out = []
    async for ev in gen:
        if ev is not None and ev.event:
            out.append(ev)
            if len(out) >= n or ev.event == "expired":
                break
    await gen.aclose()
    return out


async def test_many_tabs_one_reader_and_nothing_when_idle(app):
    hub: LiveHub = app.state.live
    assert hub._task is None  # no tabs open: no work at all
    queues = [hub.subscribe() for _ in range(25)]
    await asyncio.sleep(0.05)
    assert hub.reads == 1 and all(q.qsize() == 1 for q in queues)  # one file read, 25 deliveries
    for q in queues:
        hub.unsubscribe(q)
    assert hub._task is None


async def test_stream_is_filtered_per_user(app, alice):
    from hsm.web.live import LiveStream
    events = await collect(LiveStream(app.state)._events(alice.cookie), 1)
    data = events[0].json
    assert list(data["containers"]) == ["test1"] and data["meta"]["lxd_ok"] is True


async def test_admin_sees_everything(app, client):
    from hsm.web.live import LiveStream
    boss = login(client, "boss@example.com", "admin")
    events = await collect(LiveStream(app.state)._events(boss.cookie), 1)
    assert set(events[0].json["containers"]) == {"test1", "secret"}


async def test_stream_ends_when_session_is_revoked(app, alice, lxd):
    from hsm.web.live import LiveStream
    app.state.db.delete_session(alice.token)
    events = await collect(LiveStream(app.state)._events(alice.cookie), 2)
    assert events[-1].event == "expired"


async def test_new_snapshot_is_pushed(app, alice, lxd):
    from hsm.web.live import LiveStream
    gen = LiveStream(app.state)._events(alice.cookie)
    first = await collect_one(gen)
    lxd.instances["test1"]["status"] = "Frozen"
    await asyncio.sleep(1.1)  # mtime granularity
    publish(app, lxd)
    second = await collect_one(gen)
    await gen.aclose()
    assert first["containers"]["test1"]["status"] == "Running"
    assert second["containers"]["test1"]["status"] == "Frozen"


async def collect_one(gen):
    async for ev in gen:
        if ev is not None and ev.event == "snapshot":
            return ev.json
