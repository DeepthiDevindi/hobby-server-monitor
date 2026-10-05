"""Terminal WebSocket: Origin check, authZ on connect, limits, timeouts."""
from __future__ import annotations

import json
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from app import terminal
from .conftest import ORIGIN, login

URL = "/api/containers/test1/terminal?cols=100&rows=30"


@pytest.fixture
def alice(client):
    h = login(client, "alice@example.com", "user")
    client.app.state.db.assign(int(h["_uid"]), "test1")
    return h


@pytest.fixture(autouse=True)
def fast_checks(monkeypatch):
    monkeypatch.setattr(terminal, "CHECK_SECONDS", 0.05)


def _hangup(client, ws) -> None:
    """Close from the client and wait until the server-side handler has
    finished its cleanup; TestClient cancels handlers still running at exit."""
    open_now = lambda: sum(client.app.state.terminals.active.values())  # noqa: E731
    before = open_now()
    ws.close(1000)
    deadline = time.monotonic() + 2
    while open_now() >= before and time.monotonic() < deadline:
        time.sleep(0.01)


def _close_code(client, url=URL, origin=ORIGIN):
    with client.websocket_connect(url, headers={"Origin": origin}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            while True:
                ws.receive_bytes()
    return e.value.code


@pytest.mark.parametrize("origin", ["https://evil.example", "null", ""])
def test_bad_origin_rejected_before_accept(client, alice, lxd, origin):
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(URL, headers={"Origin": origin}):
            pass
    assert e.value.code == 1008 and lxd.execs == []


def test_assigned_user_gets_a_working_terminal(client, alice, lxd):
    with client.websocket_connect(URL, headers={"Origin": ORIGIN}) as ws:
        ws.send_bytes(b"ls\n")
        assert ws.receive_bytes() == b"echo:ls\n"
        ws.send_text(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
        ws.send_text("garbage{")  # malformed control frames are ignored
        ws.send_bytes(b"x")
        assert ws.receive_bytes() == b"echo:x"
        _hangup(client, ws)
    name, ex = lxd.execs[0]
    assert name == "test1" and (120, 40) in ex.resizes
    assert ex.closed
    actions = [r["action"] for r in client.app.state.db.list_audit(10, None)]
    assert "terminal_open" in actions and "terminal_close" in actions
    assert client.app.state.terminals.active == {}


def test_container_not_running(client, alice, lxd):
    lxd.instances["test1"]["status"] = "Stopped"
    assert _close_code(client) == 4409 and lxd.execs == []


def test_idle_timeout(client, alice):
    object.__setattr__(client.app.state.settings, "terminal_idle_seconds", 0)  # frozen dataclass
    assert _close_code(client) == 4408


def test_session_revocation_closes_terminal(client, alice):
    with client.websocket_connect(URL, headers={"Origin": ORIGIN}) as ws:
        ws.send_bytes(b"a")
        ws.receive_bytes()
        client.app.state.db.delete_session(alice["_raw"])  # logout elsewhere / expiry
        with pytest.raises(WebSocketDisconnect) as e:
            while True:
                ws.receive_bytes()
    assert e.value.code == 4401


def test_unassign_closes_open_terminal(client, alice):
    with client.websocket_connect(URL, headers={"Origin": ORIGIN}) as ws:
        ws.send_bytes(b"a")
        ws.receive_bytes()
        client.app.state.db.unassign(int(alice["_uid"]), "test1")
        with pytest.raises(WebSocketDisconnect) as e:
            while True:
                ws.receive_bytes()
    assert e.value.code == 4403


def test_concurrent_session_limit(client, alice):
    limit = client.app.state.settings.terminal_max_per_user
    opened = [client.websocket_connect(URL, headers={"Origin": ORIGIN}) for _ in range(limit)]
    sockets = [c.__enter__() for c in opened]
    for s in sockets:
        s.send_bytes(b"k")
        s.receive_bytes()
    assert _close_code(client) == 4429
    for c, s in zip(opened, sockets):
        _hangup(client, s)
        c.__exit__(None, None, None)
    assert client.app.state.terminals.active == {}


def test_terminal_rate_limit(client, alice, lxd):
    lxd.instances["test1"]["status"] = "Stopped"  # cheap rejections still count
    codes = [_close_code(client) for _ in range(11)]
    assert codes[-1] == 4429
