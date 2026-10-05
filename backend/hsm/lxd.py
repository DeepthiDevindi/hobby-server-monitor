"""pylxd wrapper shared by the web API and the collector.

Synchronous on purpose (pylxd is built on `requests`): the collector calls it
directly from its loop, the async web API calls it via `asyncio.to_thread`.

Mutations go through pylxd models. The bulk metrics read uses pylxd's raw
API node with `recursion=2`, which returns every instance *with* its state in
ONE request; the model API would need 1 + N requests per poll.
"""
from __future__ import annotations

import logging
import threading
import warnings
from typing import Any, Callable

import pylxd
import requests
from pylxd.exceptions import ClientConnectionFailed, LXDAPIException, NotFound

log = logging.getLogger("hsm.lxd")
logging.getLogger("ws4py").setLevel(logging.WARNING)  # pylxd exec logs every socket close at INFO
warnings.filterwarnings("ignore", message="Attempted to set unknown attribute", module="pylxd")
TIMEOUT = (3.05, 30)  # (connect, read) seconds: a hung LXD can't hang us forever


class LXDError(Exception):
    """Something LXD said no to (status mirrors LXD's HTTP code)."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class LXDUnavailable(LXDError):
    def __init__(self, message: str = "LXD is unreachable") -> None:
        super().__init__(message, 503)


def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Translate pylxd/requests exceptions into LXDError/LXDUnavailable."""

    def inner(self: "LXD", *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(self, *args, **kwargs)
        except NotFound as exc:
            raise LXDError("Instance not found", 404) from exc
        except LXDAPIException as exc:
            status = getattr(exc.response, "status_code", 502) or 502
            raise LXDError(str(exc), status if status in (400, 404, 409) else 502) from exc
        except (ClientConnectionFailed, requests.ConnectionError, requests.Timeout) as exc:
            self._client = None  # reconnect on the next call
            raise LXDUnavailable() from exc

    inner.__name__ = fn.__name__
    inner.__doc__ = fn.__doc__
    return inner


def root_size_gib(devices: dict[str, Any]) -> float | None:
    size = (devices.get("root") or {}).get("size")
    return parse_size(size) / 2**30 if size else None


def parse_size(value: str | None) -> int:
    """'512MiB' / '2GiB' / '1GB' / '1073741824' -> bytes (0 if unset)."""
    if not value:
        return 0
    units = {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40, "kB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12, "B": 1}
    for suffix, mult in units.items():
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)]) * mult)
    return int(value)


class LXD:
    def __init__(self, endpoint: str, verify: bool = True) -> None:
        self.endpoint = endpoint
        self.verify = verify
        self._client: pylxd.Client | None = None
        self._lock = threading.Lock()

    def client(self) -> pylxd.Client:
        with self._lock:
            if self._client is None:
                ep = self.endpoint[len("unix://"):] if self.endpoint.startswith("unix://") else self.endpoint
                self._client = pylxd.Client(endpoint=ep, verify=self.verify, timeout=TIMEOUT)
            return self._client

    # ---- reads -------------------------------------------------------------
    @_wrap
    def instances_with_state(self) -> list[dict[str, Any]]:
        return self.client().api.instances.get(params={"recursion": 2}).json()["metadata"] or []

    @_wrap
    def instance(self, name: str) -> dict[str, Any]:
        return self.client().api.instances[name].get().json()["metadata"]

    @_wrap
    def state(self, name: str) -> dict[str, Any]:
        return self.client().api.instances[name].state.get().json()["metadata"]

    @_wrap
    def host_resources(self) -> dict[str, Any]:
        r = self.client().resources
        return {"cpus": r["cpu"]["total"], "memory_bytes": r["memory"]["total"]}

    @_wrap
    def storage_pools(self) -> list[dict[str, Any]]:
        out = []
        for pool in self.client().storage_pools.all():
            space = self.client().api.storage_pools[pool.name].resources.get().json()["metadata"]["space"]
            out.append({"name": pool.name, "driver": pool.driver,
                        "total_bytes": space["total"], "used_bytes": space["used"],
                        # dir pools cannot enforce a root disk size (no quota support)
                        "sizable": pool.driver not in ("dir",)})
        return out

    @_wrap
    def networks(self) -> list[dict[str, Any]]:
        return [{"name": n.name, "type": n.type} for n in self.client().networks.all()
                if n.managed and n.type == "bridge"]

    @_wrap
    def profiles(self) -> list[str]:
        return [p.name for p in self.client().profiles.all()]

    @_wrap
    def cached_image_descriptions(self) -> list[str]:
        return [img.properties.get("description", "") for img in self.client().images.all()]

    @_wrap
    def operation(self, op_id: str) -> dict[str, Any]:
        return self.client().api.operations[op_id].get().json()["metadata"]

    # ---- writes ------------------------------------------------------------
    @_wrap
    def begin_create(self, body: dict[str, Any]) -> str:
        """Start an async create; returns the LXD operation id to poll.
        (Not wait=True: an image download can take minutes.)"""
        data = self.client().api.instances.post(json=body).json()
        return data["operation"].rsplit("/", 1)[-1]

    @_wrap
    def set_state(self, name: str, action: str) -> None:
        inst = self.client().instances.get(name)
        if action == "freeze":
            inst.freeze(wait=True)
        elif action == "unfreeze":
            inst.unfreeze(wait=True)
        else:
            getattr(inst, action)(wait=True, **({"force": True, "timeout": 30} if action in ("stop", "restart") else {}))

    @_wrap
    def update_limits(self, name: str, config: dict[str, str], disk_gib: float | None) -> None:
        inst = self.client().instances.get(name)
        for key, value in config.items():
            if value == "":
                inst.config.pop(key, None)  # "" means: remove the limit
            else:
                inst.config[key] = value
        if disk_gib is not None:
            # The root disk usually comes from a profile; override it locally.
            root = dict(inst.expanded_devices.get("root") or {"path": "/", "type": "disk"})
            root["size"] = f"{int(disk_gib)}GiB"
            inst.devices["root"] = root
        inst.save(wait=True)

    @_wrap
    def delete(self, name: str) -> None:
        inst = self.client().instances.get(name)
        if inst.status == "Running":
            inst.stop(force=True, wait=True)
        inst.delete(wait=True)

    @_wrap
    def set_user_key(self, name: str, key: str, value: str) -> None:
        inst = self.client().instances.get(name)
        inst.config[key] = value
        inst.save(wait=True)

    # ---- exec --------------------------------------------------------------
    @_wrap
    def execute(self, name: str, argv: list[str], max_bytes: int) -> dict[str, Any]:
        """Non-interactive exec. Output is capped while streaming, so a
        command like `yes` cannot exhaust our memory."""
        bufs = {"stdout": bytearray(), "stderr": bytearray()}
        dropped = {"stdout": 0, "stderr": 0}

        def handler(key: str) -> Callable[[Any], None]:
            def collect(chunk: Any) -> None:
                data = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                room = max_bytes - len(bufs[key])
                bufs[key] += data[:room]
                dropped[key] += max(0, len(data) - room)
            return collect

        inst = self.client().instances.get(name)
        result = inst.execute(argv, environment={"LANG": "C.UTF-8", "HOME": "/root"},
                              stdout_handler=handler("stdout"), stderr_handler=handler("stderr"))
        return {"exit_code": result.exit_code,
                "stdout": bufs["stdout"].decode(errors="replace"),
                "stderr": bufs["stderr"].decode(errors="replace"),
                "truncated": dropped["stdout"] + dropped["stderr"]}

    @_wrap
    def interactive(self, name: str) -> dict[str, str]:
        """Start an interactive shell; returns websocket paths (with secrets)."""
        inst = self.client().instances.get(name)
        # Fixed argv, never built from input. Falls back to sh for images
        # without bash (e.g. Alpine).
        argv = ["/bin/sh", "-c", "if command -v bash >/dev/null; then exec bash -l; else exec sh -l; fi"]
        return inst.raw_interactive_execute(argv, environment={
            "TERM": "xterm-256color", "HOME": "/root", "USER": "root", "LANG": "C.UTF-8"})
