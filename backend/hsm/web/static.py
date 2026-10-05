"""Serves the static Astro build (no Node at runtime)."""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

import falcon

from .policy import Policy


class Static:
    policy = Policy.PUBLIC  # pages are public; all data behind them needs a session

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _file(self, path: str) -> Path | None:
        target = (self.root / path.lstrip("/")).resolve()
        if not target.is_relative_to(self.root):  # path traversal guard
            return None
        if target.is_dir():
            target = target / "index.html"
        return target if target.is_file() else None

    async def on_get(self, req: Any, resp: Any, path: str = "") -> None:
        if path.startswith(("api/", "auth/")):
            raise falcon.HTTPNotFound()
        f = self._file(path)
        if f is None:
            if path and not path.endswith("/") and self._file(path + "/"):
                raise falcon.HTTPMovedPermanently(f"/{path}/")
            f = self._file("404.html")
            if f is None:
                raise falcon.HTTPNotFound()
            resp.status = falcon.HTTP_404
        resp.content_type = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        # Hashed asset names never change; HTML must always be revalidated.
        resp.cache_control = ["public", "max-age=31536000", "immutable"] if path.startswith("assets/") else ["no-cache"]
        resp.data = f.read_bytes()
