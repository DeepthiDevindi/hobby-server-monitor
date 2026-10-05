"""App factory: one process, one worker, everything wired here."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import api, auth, metrics, terminal
from .config import Settings
from .database import Database
from .lxd import LXDClient, LXDError
from .security import RateLimiter, SecurityHeaders, SessionSigner

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def _lxd_error(_: Request, exc: LXDError) -> JSONResponse:
    # Pass through LXD's own client errors (not found, already exists...);
    # anything else becomes a generic 502.
    code = exc.status if exc.status in (400, 404, 409) else 502
    return JSONResponse({"detail": str(exc) if code != 502 else "LXD request failed"}, status_code=code)


def create_app(settings: Settings | None = None, lxd: Any | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.metrics.stop()
        await app.state.lxd.close()
        app.state.db.close()

    app = FastAPI(title="Hobby Server Monitor", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    s = app.state
    s.settings = settings
    s.db = Database(settings.database_path)
    s.lxd = lxd or LXDClient(settings.lxd_socket)
    s.metrics = metrics.MetricsHub(s.lxd, settings.metrics_interval)
    s.signer = SessionSigner(settings.secret_key)
    s.limiter = RateLimiter()
    s.terminals = terminal.TerminalRegistry(settings.terminal_max_per_user, settings.terminal_max_total)
    s.oauth = auth.build_oauth(settings.google_client_id, settings.google_client_secret)

    # Short-lived signed cookie used ONLY by authlib for OAuth state + nonce.
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="hsm_oauth",
                       max_age=600, same_site="lax", https_only=settings.cookie_secure)
    app.add_middleware(SecurityHeaders, hsts=settings.cookie_secure)
    app.add_exception_handler(LXDError, _lxd_error)

    for module in (auth, api, metrics, terminal):
        app.include_router(module.router)
    # Static Astro build; no Node at runtime.
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True, check_dir=False), name="ui")
    return app


def get_app() -> FastAPI:
    """uvicorn entrypoint: `uvicorn app.main:get_app --factory`."""
    return create_app()
