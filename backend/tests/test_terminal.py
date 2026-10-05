"""Terminal WebSocket: Origin, authZ before the shell opens, limits, timeouts."""
from __future__ import annotations

import asyncio
import json

import pytest
from falcon import testing
from falcon.errors import WebSocketDisconnected

from hsm.web import terminal
from hsm.web.security import SESSION_COOKIE
from .conftest import ORIGIN, login


class FakeShell:
    """Echo 'shell' standing in for the two LXD exec websockets."""
    opened: list["FakeShell"] = []

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.resizes: list[tuple[int, int]] = []
        self.closed = False
        self.data = self

    @classmethod
    async def open(cls, state, name):
        sh = cls()
        cls.opened.append(sh)
        return sh

    async def send(self, chunk: bytes) -> None:
        await self.q.put(b"echo:" + chunk)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        return await self.q.get()

    async def resize(self, cols, rows):
        self.resizes.append((cols, rows))

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_shell(monkeypatch):
    FakeShell.opened = []
    monkeypatch.setattr(terminal.Shell, "open", FakeShell.open)
    monkeypatch.setattr(terminal, "CHECK_SECONDS", 0.05)


async def connect(app, who, name="test1", origin=ORIGIN):
    c = testing.ASGIConductor(app)
    return c, c.simulate_ws(f"/api/containers/{name}/terminal", headers=ws_headers(who, origin))


def ws_headers(who, origin=ORIGIN):
    # The WS simulator has no cookie jar: send the header a browser would.
    return {"Origin": origin, **({"Cookie": f"{SESSION_COOKIE}={who.cookie}"} if who else {})}


async def close_code(app, who, name="test1", origin=ORIGIN) -> int:
    c, ws_cm = await connect(app, who, name, origin)
    async with c:
        try:
            async with ws_cm as ws:
                await ws.receive_data()
        except WebSocketDisconnected as exc:
            return exc.code
    raise AssertionError("expected the server to close")


@pytest.mark.parametrize("origin", ["https://evil.example", "null", ""])
async def test_bad_origin_is_rejected_before_handshake(app, alice, origin):
    assert await close_code(app, alice, origin=origin) == 3403  # Falcon: 3000 + HTTP 403
    assert FakeShell.opened == []


async def test_requires_session(app):
    assert await close_code(app, None) == 4401


async def test_unassigned_container_never_opens_a_shell(app, alice):
    assert await close_code(app, alice, "secret") == 4403
    assert FakeShell.opened == []


async def test_assigned_user_gets_a_working_terminal(app, alice):
    c, ws_cm = await connect(app, alice)
    async with c, ws_cm as ws:
        await ws.send_data(b"\x00ls\n")
        assert await ws.receive_data() == b"echo:ls\n"
        await ws.send_data(b"\x01" + json.dumps({"cols": 120, "rows": 40}).encode())
        await ws.send_data(b"\x01not json")  # ignored
        await ws.send_data(b"\x00x")
        assert await ws.receive_data() == b"echo:x"
    await asyncio.sleep(0.1)
    assert FakeShell.opened[0].resizes == [(120, 40)] and FakeShell.opened[0].closed
    actions = [a["action"] for a in app.state.db.list_audit(10, None)]
    assert "terminal_open" in actions and "terminal_close" in actions
    assert app.state.terminals.active == {}


async def test_not_running_container(app, alice, lxd):
    from .conftest import publish
    lxd.instances["test1"]["status"] = "Stopped"
    publish(app, lxd)
    assert await close_code(app, alice) == 4409


async def test_idle_timeout(app, alice):
    object.__setattr__(app.state.settings, "terminal_idle_seconds", 0)
    assert await close_code(app, alice) == 4408


@pytest.mark.parametrize("revoke,code", [("logout", 4401), ("unassign", 4403)])
async def test_open_terminal_closes_on_revocation(app, alice, revoke, code):
    c, ws_cm = await connect(app, alice)
    async with c:
        with pytest.raises(WebSocketDisconnected) as e:
            async with ws_cm as ws:
                await ws.send_data(b"\x00a")
                await ws.receive_data()
                if revoke == "logout":
                    app.state.db.delete_session(alice.token)
                else:
                    app.state.db.unassign(alice.id, "uuid-test1")
                while True:
                    await ws.receive_data()
    assert e.value.code == code


async def test_concurrent_session_cap(app, client):
    bob = login(client, "bob@example.com")
    app.state.db.assign(bob.id, "uuid-test1", None)
    limit = app.state.settings.terminal_max_per_user
    c = testing.ASGIConductor(app)
    async with c:
        opened = []
        for _ in range(limit):
            cm = c.simulate_ws("/api/containers/test1/terminal", headers=ws_headers(bob))
            ws = await cm.__aenter__()
            await ws.send_data(b"\x00k")
            await ws.receive_data()
            opened.append(cm)
        assert await close_code(app, bob) == 4429
        for cm in opened:
            await cm.__aexit__(None, None, None)
