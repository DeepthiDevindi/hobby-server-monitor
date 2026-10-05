"""Web terminal: browser xterm.js <-> this WebSocket <-> LXD exec websockets.

Close codes sent to the browser (after accept):
  4401 not authenticated / session expired   4403 no access to container
  4408 idle timeout                          4409 container not running
  4429 rate limited / too many sessions      4500 LXD error
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from .dependencies import CurrentUser, can_access, resolve_user
from .schemas import NAME_RE
from .security import client_ip

log = logging.getLogger("hsm.terminal")
router = APIRouter()
CHECK_SECONDS = 5.0  # how often an open terminal re-validates session/access/idle
MAX_INPUT = 64 * 1024


class Denied(Exception):
    def __init__(self, code: int, reason: str) -> None:
        self.code, self.reason = code, reason


@dataclass
class TerminalRegistry:
    """Concurrent-session caps (per user and global)."""
    per_user: int
    total: int
    active: dict[int, int] = field(default_factory=dict)

    def acquire(self, user_id: int) -> bool:
        if sum(self.active.values()) >= self.total or self.active.get(user_id, 0) >= self.per_user:
            return False
        self.active[user_id] = self.active.get(user_id, 0) + 1
        return True

    def release(self, user_id: int) -> None:
        n = self.active.get(user_id, 0) - 1
        if n > 0:
            self.active[user_id] = n
        else:
            self.active.pop(user_id, None)


def _authorize(ws: WebSocket, name: str) -> CurrentUser:
    state = ws.app.state
    user = resolve_user(ws)  # cookie is sent by the browser on the WS handshake
    if user is None:
        raise Denied(4401, "not authenticated")
    if not NAME_RE.fullmatch(name) or not can_access(ws, user, name):
        raise Denied(4403, "no access to this container")
    if not (state.limiter.allow(f"term:u{user.id}", 10, 60) and state.limiter.allow(f"term:{client_ip(ws)}", 20, 60)):
        raise Denied(4429, "too many terminal requests, wait a minute")
    return user


def _dims(ws: WebSocket) -> tuple[int, int]:
    def clamp(key: str, default: int) -> int:
        try:
            return min(500, max(10, int(ws.query_params.get(key, default))))
        except ValueError:
            return default
    return clamp("cols", 80), clamp("rows", 24)


@router.websocket("/api/containers/{name}/terminal")
async def terminal(ws: WebSocket, name: str) -> None:
    state = ws.app.state
    # Cross-site WebSocket hijacking defence: reject the handshake outright.
    if ws.headers.get("origin") != state.settings.public_origin:
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        user = _authorize(ws, name)
        if (await state.lxd.get_state(name)).get("status") != "Running":
            raise Denied(4409, "container is not running")
        if not state.terminals.acquire(user.id):
            raise Denied(4429, "too many open terminals")
    except Denied as d:
        await ws.close(code=d.code, reason=d.reason)
        return
    except Exception:
        log.exception("terminal pre-check failed")
        await ws.close(code=4500, reason="LXD error")
        return

    actor = {"id": user.id, "email": user.email}
    started = time.monotonic()
    reason = "closed"
    state.db.audit("terminal_open", actor=actor, target=name, ip=client_ip(ws))
    try:
        cols, rows = _dims(ws)
        session = await state.lxd.exec_interactive(name, cols, rows)
        try:
            reason = await _bridge(ws, session, user, name)
        finally:
            await session.close()
    except Exception:
        log.exception("terminal bridge failed")
        reason = "error"
    finally:
        state.terminals.release(user.id)
        state.db.audit("terminal_close", actor=actor, target=name, ip=client_ip(ws),
                       detail=f"{reason}, {int(time.monotonic() - started)}s")
    codes = {"idle": (4408, "idle timeout"), "expired": (4401, "session expired"),
             "revoked": (4403, "access revoked"), "error": (4500, "LXD error")}
    code, text = codes.get(reason, (1000, "bye"))
    try:
        await ws.close(code=code, reason=text)
    except Exception:
        pass  # client already gone


async def _bridge(ws: WebSocket, session, user: CurrentUser, name: str) -> str:
    """Pump bytes both ways until one side ends or the watchdog trips.
    Returns a short reason string."""
    last_input = time.monotonic()
    idle = ws.app.state.settings.terminal_idle_seconds

    async def from_browser() -> str:
        nonlocal last_input
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return "client"
            if msg.get("bytes") is not None:
                last_input = time.monotonic()
                await session.send(msg["bytes"][:MAX_INPUT])
            elif msg.get("text"):
                await _control(session, msg["text"])

    async def from_lxd() -> str:
        try:
            while True:
                await ws.send_bytes(await session.recv())
        except ConnectionClosed:
            return "exit"  # shell exited

    async def watchdog() -> str:
        while True:
            await asyncio.sleep(CHECK_SECONDS)
            current = resolve_user(ws)  # logout / expiry / user deletion
            if current is None or time.time() >= current.session_expires:
                return "expired"
            if not can_access(ws, current, name):  # unassigned / demoted
                return "revoked"
            if time.monotonic() - last_input > idle:
                return "idle"

    tasks = [asyncio.create_task(c()) for c in (from_browser, from_lxd, watchdog)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return _outcome(done.pop())
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _outcome(task: asyncio.Task) -> str:
    exc = task.exception()
    if exc is None:
        return task.result()
    # Sending to a browser that already left raises; that is a normal close.
    return "client" if isinstance(exc, (WebSocketDisconnect, RuntimeError)) else "error"


async def _control(session, text: str) -> None:
    """Only one control message is accepted from the browser: resize."""
    try:
        msg = json.loads(text)
        if msg.get("type") == "resize":
            cols = min(500, max(10, int(msg["cols"])))
            rows = min(500, max(5, int(msg["rows"])))
            await session.resize(cols, rows)
    except (ValueError, KeyError, TypeError, AttributeError):
        pass  # ignore malformed control frames
