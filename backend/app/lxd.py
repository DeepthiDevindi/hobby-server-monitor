"""Minimal async LXD REST client over the local unix socket (no `lxc` subprocesses)."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx
from websockets.asyncio.client import ClientConnection, unix_connect


class LXDError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class ExecSession:
    """An interactive `exec` with its stdin/stdout ("0") and control websockets."""

    def __init__(self, data: ClientConnection, control: ClientConnection) -> None:
        self.data = data
        self.control = control

    async def send(self, chunk: bytes) -> None:
        await self.data.send(chunk)

    async def recv(self) -> bytes:
        msg = await self.data.recv()
        return msg if isinstance(msg, bytes) else msg.encode()

    async def resize(self, cols: int, rows: int) -> None:
        await self.control.send(json.dumps(
            {"command": "window-resize", "args": {"width": str(cols), "height": str(rows)}}
        ))

    async def close(self) -> None:
        # SIGHUP the shell so it does not linger inside the container.
        try:
            await self.control.send(json.dumps({"command": "signal", "signal": 1}))
        except Exception:
            pass
        for ws in (self.data, self.control):
            try:
                await ws.close()
            except Exception:
                pass


def _path(name: str, suffix: str = "") -> str:
    # Names are regex-validated before reaching here; quoting is defence in depth.
    return f"/1.0/instances/{quote(name, safe='')}{suffix}"


class LXDClient:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://lxd",
            timeout=httpx.Timeout(30.0),
        )
        self.calls = 0  # request counter, used by tests and for observability

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        self.calls += 1
        try:
            resp = await self._http.request(method, path, **kw)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LXDError("LXD is unreachable") from exc
        if resp.is_error or data.get("type") == "error":
            raise LXDError(data.get("error") or f"LXD HTTP {resp.status_code}", resp.status_code)
        return data

    async def _wait(self, data: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        """Block until an async LXD operation finishes; raise if it failed."""
        op = data.get("operation")
        if not op:
            return data.get("metadata") or {}
        result = (await self._request("GET", f"{op}/wait", params={"timeout": timeout},
                                      timeout=timeout + 10)).get("metadata") or {}
        if result.get("status") != "Success":
            raise LXDError(result.get("err") or "LXD operation failed")
        return result

    # ---- reads -------------------------------------------------------------
    async def list_instances(self, with_state: bool = False) -> list[dict[str, Any]]:
        # recursion=2 embeds each instance's state, so all metrics come from ONE call.
        data = await self._request("GET", "/1.0/instances", params={"recursion": 2 if with_state else 1})
        return data.get("metadata") or []

    async def get_instance(self, name: str) -> dict[str, Any]:
        return (await self._request("GET", _path(name))).get("metadata") or {}

    async def get_state(self, name: str) -> dict[str, Any]:
        return (await self._request("GET", _path(name, "/state"))).get("metadata") or {}

    # ---- writes ------------------------------------------------------------
    async def create_instance(self, name: str, source: dict[str, str], config: dict[str, str]) -> None:
        body = {
            "name": name,
            "type": "container",
            "profiles": ["default"],
            "config": config,
            "source": {"type": "image", "mode": "pull", **source},
        }
        await self._wait(await self._request("POST", "/1.0/instances", json=body), timeout=600)

    async def set_state(self, name: str, action: str) -> None:
        body = {"action": action, "timeout": 30, "force": action != "start"}
        await self._wait(await self._request("PUT", _path(name, "/state"), json=body))

    async def update_config(self, name: str, config: dict[str, str]) -> None:
        # PATCH merges keys, so unrelated config is left untouched.
        await self._wait(await self._request("PATCH", _path(name), json={"config": config}))

    async def delete_instance(self, name: str) -> None:
        await self._wait(await self._request("DELETE", _path(name)))

    # ---- exec --------------------------------------------------------------
    async def exec_interactive(self, name: str, cols: int, rows: int) -> ExecSession:
        body = {
            "command": ["/bin/bash", "-l"],  # fixed argv; never built from user input
            "environment": {"TERM": "xterm-256color", "HOME": "/root", "USER": "root", "LANG": "C.UTF-8"},
            "interactive": True,
            "wait-for-websocket": True,
            "width": cols,
            "height": rows,
        }
        data = await self._request("POST", _path(name, "/exec"), json=body)
        op = data["operation"]
        fds = data["metadata"]["metadata"]["fds"]
        data_ws = await self._ws(f"{op}/websocket?secret={quote(fds['0'])}")
        control_ws = await self._ws(f"{op}/websocket?secret={quote(fds['control'])}")
        return ExecSession(data_ws, control_ws)

    async def _ws(self, path: str) -> ClientConnection:
        self.calls += 1
        return await unix_connect(self.socket_path, f"ws://lxd{path}", max_size=2**20, open_timeout=10)
