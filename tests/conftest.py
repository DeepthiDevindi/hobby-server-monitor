from __future__ import annotations

import asyncio
import secrets

import pytest
from starlette.testclient import TestClient
from websockets.exceptions import ConnectionClosedOK

from app.config import Settings
from app.lxd import LXDError
from app.main import create_app
from app.security import SESSION_COOKIE

ORIGIN = "http://testserver"


class FakeExec:
    """Echoes stdin back, like a very dumb shell."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.resizes: list[tuple[int, int]] = []

    async def send(self, chunk: bytes) -> None:
        await self.q.put(b"echo:" + chunk)

    async def recv(self) -> bytes:
        item = await self.q.get()
        if item is None:
            raise ConnectionClosedOK(None, None)
        return item

    async def resize(self, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    async def close(self) -> None:
        self.closed = True


class FakeLXD:
    def __init__(self) -> None:
        self.calls = 0
        self.execs: list[tuple[str, FakeExec]] = []
        self.instances = {
            n: {"name": n, "status": "Running", "config": {"limits.cpu": "1"}} for n in ("test1", "secret")
        }

    def _get(self, name: str) -> dict:
        self.calls += 1
        if name not in self.instances:
            raise LXDError("Instance not found", 404)
        return self.instances[name]

    async def close(self) -> None: ...

    async def list_instances(self, with_state: bool = False):
        self.calls += 1
        out = []
        for i in self.instances.values():
            item = dict(i)
            if with_state:
                item["state"] = {"cpu": {"usage": self.calls * 10**9}, "memory": {"usage": 1, "total": 2},
                                 "network": {"eth0": {"counters": {"bytes_received": 5, "bytes_sent": 5}}}}
            out.append(item)
        return out

    async def get_instance(self, name):
        return self._get(name)

    async def get_state(self, name):
        return {"status": self._get(name)["status"]}

    async def create_instance(self, name, source, config):
        self.calls += 1
        if name in self.instances:
            raise LXDError("Instance already exists", 409)
        self.instances[name] = {"name": name, "status": "Stopped", "config": config}

    async def set_state(self, name, action):
        self._get(name)["status"] = "Running" if action != "stop" else "Stopped"

    async def update_config(self, name, config):
        self._get(name)["config"].update(config)

    async def delete_instance(self, name):
        self._get(name)
        del self.instances[name]

    async def exec_interactive(self, name, cols, rows):
        self.calls += 1
        ex = FakeExec()
        self.execs.append((name, ex))
        return ex


@pytest.fixture
def settings(tmp_path):
    return Settings(
        secret_key="x" * 40,
        google_client_id="cid",
        google_client_secret="csecret",
        bootstrap_admin_email="boss@example.com",
        cookie_secure=False,
        public_origin=ORIGIN,
        database_path=str(tmp_path / "t.db"),
        frontend_dir=str(tmp_path / "dist"),
        metrics_interval=3.0,
        max_cpus=4,
    )


@pytest.fixture
def lxd():
    return FakeLXD()


@pytest.fixture
def app(settings, lxd):
    return create_app(settings, lxd)


@pytest.fixture
def client(app):
    with TestClient(app, base_url=ORIGIN) as c:
        yield c


def login(client: TestClient, email: str, role: str = "user", ttl: int = 3600) -> dict:
    """Create a user + server-side session and put the signed cookie in the jar.
    Returns headers carrying the CSRF token for state-changing calls."""
    state = client.app.state
    user = state.db.get_user_by_email(email) or state.db.create_user(email, role)
    state.db.set_role(user["id"], role)
    raw, cookie = state.signer.new_token()
    csrf = secrets.token_urlsafe(16)
    state.db.create_session(raw, user["id"], csrf, ttl)
    client.cookies.set(SESSION_COOKIE, cookie)
    return {"X-CSRF-Token": csrf, "Origin": ORIGIN, "_uid": str(user["id"]), "_raw": raw}


def hdr(h: dict) -> dict:
    return {k: v for k, v in h.items() if not k.startswith("_")}
