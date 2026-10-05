from __future__ import annotations

import secrets
import time
from typing import Any

import pytest
from falcon import testing

from hsm.config import Settings
from hsm.lxd import LXDError, LXDUnavailable
from hsm.tsdb import MetricWriter
from hsm.web.app import create_app
from hsm.web.security import SESSION_COOKIE

ORIGIN = "http://testserver"
GiB = 2**30


def instance(name: str, uuid: str, status: str = "Running", cpus: int | None = 1, mem_mib: int | None = 512,
             disk_gib: int | None = 4, pool: str = "fast") -> dict[str, Any]:
    cfg = {"volatile.uuid": uuid, "image.description": "ubuntu 24.04"}
    if cpus:
        cfg["limits.cpu"] = str(cpus)
    if mem_mib:
        cfg["limits.memory"] = f"{mem_mib}MiB"
    root = {"type": "disk", "path": "/", "pool": pool, **({"size": f"{disk_gib}GiB"} if disk_gib else {})}
    return {"name": name, "status": status, "config": cfg, "expanded_devices": {"root": root}, "devices": {},
            "last_used_at": "2026-10-05T00:00:00Z", "description": "", "ephemeral": False,
            "state": {"cpu": {"usage": 0}, "memory": {"usage": 100 * 2**20, "total": (mem_mib or 8192) * 2**20},
                      "disk": {"root": {"usage": GiB, "total": (disk_gib or 0) * GiB}}, "processes": 12,
                      "network": {"eth0": {"counters": {"bytes_received": 0, "bytes_sent": 0},
                                           "addresses": [{"family": "inet", "scope": "global", "address": "10.0.0.2"}]}}}}


class FakeLXD:
    """Same surface as hsm.lxd.LXD, in memory."""

    def __init__(self) -> None:
        self.calls = 0
        self.down = False
        self.instances = {"test1": instance("test1", "uuid-test1"), "secret": instance("secret", "uuid-secret")}
        self.created: list[dict[str, Any]] = []
        self.execs: list[tuple[str, list[str]]] = []

    def _tick(self) -> None:
        self.calls += 1
        if self.down:
            raise LXDUnavailable()

    def _get(self, name: str) -> dict[str, Any]:
        self._tick()
        if name not in self.instances:
            raise LXDError("Instance not found", 404)
        return self.instances[name]

    def instances_with_state(self):
        self._tick()
        return list(self.instances.values())

    def instance(self, name):
        return self._get(name)

    def state(self, name):
        return {"status": self._get(name)["status"]}

    def host_resources(self):
        self._tick()
        return {"cpus": 4, "memory_bytes": 8 * GiB}

    def storage_pools(self):
        self._tick()
        return [{"name": "fast", "driver": "btrfs", "total_bytes": 20 * GiB, "used_bytes": 2 * GiB, "sizable": True},
                {"name": "default", "driver": "dir", "total_bytes": 100 * GiB, "used_bytes": 0, "sizable": False}]

    def networks(self):
        self._tick()
        return [{"name": "lxdbr0", "type": "bridge"}]

    def profiles(self):
        self._tick()
        return ["default"]

    def cached_image_descriptions(self):
        return ["ubuntu 24.04 LTS"]

    def begin_create(self, body):
        self._tick()
        if body["name"] in self.instances:
            raise LXDError("Instance already exists", 409)
        self.created.append(body)
        root = body["devices"]["root"]
        inst = instance(body["name"], f"uuid-{body['name']}", "Stopped", int(body["config"]["limits.cpu"]),
                        int(body["config"]["limits.memory"].removesuffix("MiB")),
                        int(root["size"].removesuffix("GiB")) if "size" in root else None, root["pool"])
        inst["config"].update(body["config"])
        self.instances[body["name"]] = inst
        return "op-1"

    def operation(self, op_id):
        return {"status": "Success"}

    def set_state(self, name, action):
        self._get(name)["status"] = {"stop": "Stopped", "freeze": "Frozen"}.get(action, "Running")

    def update_limits(self, name, config, disk_gib):
        inst = self._get(name)
        inst["config"].update({k: v for k, v in config.items() if v})
        if disk_gib:
            inst["expanded_devices"]["root"]["size"] = f"{disk_gib}GiB"

    def delete(self, name):
        self._get(name)
        del self.instances[name]

    def set_user_key(self, name, key, value):
        self._get(name)["config"][key] = value

    def execute(self, name, argv, max_bytes):
        self._get(name)
        self.execs.append((name, argv))
        return {"exit_code": 0, "stdout": "hello\n", "stderr": "", "truncated": 0}

    def interactive(self, name):
        raise AssertionError("tests patch Shell.open instead")


@pytest.fixture
def settings(tmp_path):
    return Settings(
        session_secret="s" * 40, google_client_id="cid", google_client_secret="csecret",
        google_redirect_uri=f"{ORIGIN}/auth/google/callback", bootstrap_admin_email="boss@example.com",
        cookie_secure=False, public_origin=ORIGIN, sqlite_path=str(tmp_path / "app.db"),
        tinyflux_dir=str(tmp_path / "metrics"), dashboard_dir=str(tmp_path / "dist"),
    )


@pytest.fixture
def lxd():
    return FakeLXD()


@pytest.fixture
def app(settings, lxd):
    app = create_app(settings, lxd)
    # Containers known to the DB + a fresh collector snapshot, as in production.
    for name in ("test1", "secret"):
        app.state.db.record_container(f"uuid-{name}", name, None, None, 1, 512, 4)
    publish(app, lxd)
    return app


def publish(app, lxd) -> None:
    from hsm.collector import describe
    now = time.time()
    snap = {"ts": now, "lxd_ok": True, "error": None, "last_ok": now, "interval": 10,
            "containers": {}}
    for inst in lxd.instances.values():
        d = describe(inst, now)
        snap["containers"][d["name"]] = {k: v for k, v in d.items() if not k.startswith("_")}
    MetricWriter(app.state.settings.tinyflux_dir, app.state.settings.retention).publish_latest(snap)


@pytest.fixture
def client(app):
    return testing.TestClient(app)


class As:
    """A signed-in browser: cookie + CSRF header on every request."""

    def __init__(self, client: testing.TestClient, user: dict[str, Any], cookie: str, csrf: str, token: str):
        self.client, self.user, self.cookie, self.csrf, self.token = client, user, cookie, csrf, token

    @property
    def id(self) -> int:
        return self.user["id"]

    def req(self, method: str, path: str, csrf: bool = True, **kw: Any):
        headers = {"Origin": ORIGIN, **({"X-CSRF-Token": self.csrf} if csrf else {}), **kw.pop("headers", {})}
        return self.client.simulate_request(method, path, cookies={SESSION_COOKIE: self.cookie},
                                            headers=headers, **kw)

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, **kw):
        return self.req("POST", path, **kw)

    def patch(self, path, **kw):
        return self.req("PATCH", path, **kw)

    def put(self, path, **kw):
        return self.req("PUT", path, **kw)

    def delete(self, path, **kw):
        return self.req("DELETE", path, **kw)


def login(client: testing.TestClient, email: str, role: str = "user", ttl: int = 3600,
          quota: dict[str, int | None] | None = None) -> As:
    db = client.app.state.db
    user = db.get_user_by_email(email) or db.invite_user(email, role, None, quota or {})
    db.set_role(user["id"], role)
    if quota is not None:
        db.set_quota(user["id"], quota.get("cpus"), quota.get("memory_mib"), quota.get("disk_gib"))
    raw, cookie = client.app.state.signer.new_session_token()
    csrf = secrets.token_urlsafe(16)
    db.create_session(raw, user["id"], csrf, ttl)
    return As(client, db.get_user(user["id"]), cookie, csrf, raw)


@pytest.fixture
def admin(client):
    return login(client, "boss@example.com", "admin")


@pytest.fixture
def alice(client):
    """A container user with test1 assigned."""
    u = login(client, "alice@example.com", "user")
    client.app.state.db.assign(u.id, "uuid-test1", None)
    return u
