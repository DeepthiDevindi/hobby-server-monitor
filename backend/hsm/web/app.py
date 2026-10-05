"""App factory and route table.

    uvicorn hsm.web.app:create_app --factory --app-dir backend
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import falcon
import falcon.asgi

from ..config import Settings
from ..db import Database
from ..lxd import LXD
from . import admin, auth, containers, live, terminal
from .common import error_serializer
from .policy import AuthMiddleware, check_routes
from .security import RateLimiter, SecurityHeaders, Signer
from .static import Static

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def routes(state: Any) -> list[tuple[str, Any]]:
    """Single source of truth for what is reachable (tests walk this list)."""
    callback_path = urlparse(state.settings.google_redirect_uri).path or "/auth/google/callback"
    static = Static(state.settings.dashboard_dir)
    return [
        ("/auth/login", auth.Login(state)),
        (callback_path, auth.Callback(state)),
        ("/api/auth/logout", auth.Logout(state)),
        ("/api/me", admin.Me(state)),
        ("/api/live", live.LiveStream(state)),
        ("/api/containers", containers.Containers(state)),
        ("/api/containers/options", containers.CreateOptions(state)),
        ("/api/jobs/{job_id}", containers.Job(state)),
        ("/api/containers/{name}", containers.Container(state)),
        ("/api/containers/{name}/state", containers.ContainerState(state)),
        ("/api/containers/{name}/owner", containers.ContainerOwner(state)),
        ("/api/containers/{name}/history", containers.History(state)),
        ("/api/containers/{name}/exec", containers.Exec(state)),
        ("/api/containers/{name}/terminal", terminal.Terminal(state)),
        ("/api/admin/users", admin.Users(state)),
        ("/api/admin/users/{user_id}", admin.User(state)),
        ("/api/admin/users/{user_id}/containers/{name}", admin.Grant(state)),
        ("/api/admin/audit", admin.AuditLog(state)),
        ("/api/admin/usage", admin.Usage(state)),
        ("/", static),
        ("/{path:path}", static),
    ]


class HSMApp(falcon.asgi.App):
    """falcon.asgi.App uses __slots__; this subclass can carry `state` and
    `route_table` (used by tests and by nothing else)."""


class Lifespan:
    def __init__(self, state: Any) -> None:
        self.state = state

    async def process_shutdown(self, scope: Any, event: Any) -> None:
        await self.state.live.stop()
        for job in self.state.jobs.values():
            if job.get("task"):
                job["task"].cancel()
        # The SQLite connection is left to process exit (WAL is crash-safe);
        # Falcon's test client runs this hook after every simulated request.


def create_app(settings: Settings | None = None, lxd: Any | None = None) -> falcon.asgi.App:
    settings = settings or Settings.from_env()
    state = SimpleNamespace(
        settings=settings,
        db=Database(settings.sqlite_path),
        lxd=lxd or LXD(settings.lxd_endpoint, settings.lxd_verify_cert),
        signer=Signer(settings.session_secret),
        limiter=RateLimiter(),
        terminals=terminal.TerminalRegistry(settings.terminal_max_per_user, settings.terminal_max_total),
        google_keys=auth.GoogleKeys(),
        jobs={},
        quota_lock=asyncio.Lock(),
    )
    state.live = live.LiveHub(state)
    app = HSMApp(middleware=[SecurityHeaders(hsts=settings.cookie_secure), AuthMiddleware(state),
                                      Lifespan(state)])
    app.req_options.strip_url_path_trailing_slash = False
    app.set_error_serializer(error_serializer)
    table = routes(state)
    check_routes(table)  # refuse to start with an unprotected route
    for path, resource in table:
        app.add_route(path, resource)
    app.state = state
    app.route_table = table
    return app
