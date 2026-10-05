"""Interactive terminal: xterm.js <-> this WebSocket <-> LXD exec websockets.

Authorization (Origin, session, container access) happens in AuthMiddleware
before this responder runs. Here: rate limits, concurrency caps, idle timeout
and periodic re-checks of session + access while the shell is open.

Browser -> server frames are binary with a 1-byte type prefix:
    0x00 + bytes   keystrokes (stdin)
    0x01 + JSON    {"cols": int, "rows": int}  resize
Close codes: 4401 session ended, 4403 no access / revoked, 4408 idle,
4409 not running, 4429 limits, 4500 LXD error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from falcon.errors import WebSocketDisconnected
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed

from .common import snapshot
from .policy import Policy, can_access, resolve_user
from .security import SESSION_COOKIE, client_ip

log = logging.getLogger("hsm.terminal")
CHECK_SECONDS = 5.0
MAX_FRAME = 64 * 1024


@dataclass
class TerminalRegistry:
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


class Shell:
    """The two LXD websockets of one interactive exec."""

    def __init__(self, data: Any, control: Any) -> None:
        self.data, self.control = data, control

    @classmethod
    async def open(cls, state: Any, name: str) -> "Shell":
        sock = state.settings.lxd_socket
        if not sock:
            raise RuntimeError("interactive terminal needs a unix-socket LXD_ENDPOINT")
        paths = await asyncio.to_thread(state.lxd.interactive, name)
        data = await unix_connect(sock, f"ws://lxd{paths['ws']}", max_size=2**20, open_timeout=10)
        control = await unix_connect(sock, f"ws://lxd{paths['control']}", open_timeout=10)
        return cls(data, control)

    async def resize(self, cols: int, rows: int) -> None:
        await self.control.send(json.dumps({"command": "window-resize",
                                            "args": {"width": str(cols), "height": str(rows)}}))

    async def close(self) -> None:
        try:  # SIGHUP so the shell does not linger in the container
            await self.control.send(json.dumps({"command": "signal", "signal": 1}))
        except Exception:
            pass
        for ws in (self.data, self.control):
            try:
                await ws.close()
            except Exception:
                pass


class Terminal:
    policy = {"WEBSOCKET": Policy.CONTAINER}

    def __init__(self, state: Any) -> None:
        self.state = state

    async def on_websocket(self, req: Any, ws: Any, name: str) -> None:
        s = self.state
        user = req.context.user
        await ws.accept()
        status = ((snapshot(s).get("containers") or {}).get(name) or {}).get("status")
        if not (s.limiter.allow(f"term:u{user.id}", 10, 60) and s.limiter.allow(f"term:{client_ip(req)}", 20, 60)):
            return await ws.close(4429)
        if status != "Running":
            return await ws.close(4409)
        if not s.terminals.acquire(user.id):
            return await ws.close(4429)
        started = time.monotonic()
        reason, shell = "error", None
        s.db.audit("terminal_open", actor=user.actor(), target=name, ip=client_ip(req))
        try:
            shell = await Shell.open(s, name)
            reason = await self._bridge(req, ws, shell, name)
        except Exception:
            log.exception("terminal failed")
        finally:
            if shell:
                await shell.close()
            s.terminals.release(user.id)
            s.db.audit("terminal_close", actor=user.actor(), target=name, ip=client_ip(req),
                       detail=f"{reason}, {int(time.monotonic() - started)}s")
        code = {"idle": 4408, "expired": 4401, "revoked": 4403, "error": 4500}.get(reason, 1000)
        try:
            await ws.close(code)
        except Exception:
            pass

    async def _bridge(self, req: Any, ws: Any, shell: Shell, name: str) -> str:
        last_input = time.monotonic()
        idle = self.state.settings.terminal_idle_seconds
        cookie = req.cookies.get(SESSION_COOKIE)

        async def from_browser() -> str:
            nonlocal last_input
            while True:
                frame = await ws.receive_data()
                if not frame or len(frame) > MAX_FRAME:
                    continue
                if frame[0] == 0:
                    last_input = time.monotonic()
                    await shell.data.send(frame[1:])
                elif frame[0] == 1:
                    await _resize(shell, frame[1:])

        async def from_lxd() -> str:
            async for msg in shell.data:
                await ws.send_data(msg if isinstance(msg, bytes) else msg.encode())
            return "exit"

        async def watchdog() -> str:
            while True:
                await asyncio.sleep(CHECK_SECONDS)
                current = resolve_user(self.state, cookie, touch=False)
                if current is None or time.time() >= current.expires_at:
                    return "expired"
                if not can_access(self.state, current, name):
                    return "revoked"
                if time.monotonic() - last_input > idle:
                    return "idle"

        tasks = [asyncio.create_task(c()) for c in (from_browser, from_lxd, watchdog)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            task = done.pop()
            exc = task.exception()
            if exc is None:
                return task.result()
            return "client" if isinstance(exc, (WebSocketDisconnected, ConnectionClosed)) else "error"
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _resize(shell: Shell, payload: bytes) -> None:
    try:
        msg = json.loads(payload)
        await shell.resize(min(500, max(10, int(msg["cols"]))), min(500, max(5, int(msg["rows"]))))
    except (ValueError, KeyError, TypeError):
        pass  # ignore malformed control frames
