"""Helpers shared by the resources."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, TypeVar

import falcon
from pydantic import BaseModel, ValidationError

from ..lxd import LXDError, LXDUnavailable
from ..tsdb import read_latest
from .policy import NAME_RE
from .security import client_ip

MAX_BODY = 64 * 1024
M = TypeVar("M", bound=BaseModel)


async def body(req: Any, model: type[M]) -> M:
    """Parse + validate a JSON body. Size-capped before parsing."""
    if (req.content_length or 0) > MAX_BODY:
        raise falcon.HTTPContentTooLarge(title="Request body too large")
    try:
        data = await req.get_media()
    except falcon.MediaNotFoundError:
        data = {}
    except falcon.HTTPError:
        raise falcon.HTTPBadRequest(title="Body must be valid JSON")
    try:
        return model.model_validate(data if data is not None else {})
    except ValidationError as exc:
        raise falcon.HTTPUnprocessableEntity(title="Invalid input", description=_explain(exc))


def _explain(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'body'}: {e['msg']}" for e in exc.errors())


async def lxd(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking pylxd call in a worker thread; map errors to HTTP."""
    try:
        return await asyncio.to_thread(fn, *args)
    except LXDUnavailable:
        raise falcon.HTTPServiceUnavailable(title="LXD is unreachable",
                                            description="The LXD daemon did not answer. Try again shortly.")
    except LXDError as exc:
        status = {400: falcon.HTTP_400, 404: falcon.HTTP_404, 409: falcon.HTTP_409}.get(exc.status, falcon.HTTP_502)
        raise falcon.HTTPError(status, title="LXD refused the request", description=str(exc))


def audit(req: Any, action: str, target: str | None = None, detail: str | None = None) -> None:
    user = getattr(req.context, "user", None)
    req.context.state.db.audit(action, actor=user.actor() if user else None, target=target, detail=detail,
                               ip=client_ip(req))


def snapshot(state: Any) -> dict[str, Any]:
    """Newest collector snapshot plus freshness metadata."""
    snap = read_latest(state.settings.tinyflux_dir) or {"containers": {}, "ts": None, "lxd_ok": None}
    age = time.time() - snap["ts"] if snap.get("ts") else None
    snap["age_s"] = None if age is None else round(age, 1)
    # Collector considered down if it missed three ticks.
    snap["collector_ok"] = age is not None and age < 3 * state.settings.poll_interval + 5
    return snap


def error_serializer(req: Any, resp: Any, exc: falcon.HTTPError) -> None:
    resp.content_type = falcon.MEDIA_JSON
    resp.media = {"detail": exc.description or exc.title or "error", "title": exc.title}


def require_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise falcon.HTTPUnprocessableEntity(title="Invalid container name",
                                             description="lowercase letters, digits and '-', max 63")
    return name
