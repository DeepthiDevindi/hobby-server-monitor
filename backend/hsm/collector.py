"""Background metrics collector: `python -m hsm.collector`.

Runs as its own process (own systemd unit), independent of the web API and
of browsers. Every COLLECTOR_POLL_INTERVAL_SECONDS (10 s) it makes ONE LXD
request (instances?recursion=2) for all containers, then:

  1. computes rates (CPU %, network B/s) from the previous sample,
  2. appends raw points to the current hourly TinyFlux segment,
  3. folds samples into 5-min and 1-hour rollups (flushed when a window closes),
  4. reconciles the SQLite `containers` table (renames / out-of-band deletes),
  5. atomically publishes latest.json for the live dashboard,
  6. once a minute, deletes segments past retention.

Cost is independent of how many browser tabs are open: tabs never reach
this process. If LXD is down the loop keeps running, records lxd up=0, marks
the snapshot stale and retries next tick.
"""
from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Settings
from .db import Database
from .lxd import LXD, LXDError, parse_size
from .tsdb import RAW, ROLLUP_TIERS, TIERS, MetricWriter

log = logging.getLogger("hsm.collector")


def _net(network: dict[str, Any] | None) -> tuple[int, int, str | None]:
    rx = tx = 0
    ipv4 = None
    for ifname, nic in (network or {}).items():
        if ifname == "lo":
            continue
        c = nic.get("counters") or {}
        rx += c.get("bytes_received", 0)
        tx += c.get("bytes_sent", 0)
        for a in nic.get("addresses") or []:
            if ipv4 is None and a.get("family") == "inet" and a.get("scope") == "global":
                ipv4 = a.get("address")
    return rx, tx, ipv4


def _uptime(inst: dict[str, Any], now: float) -> int | None:
    if inst.get("status") != "Running" or not inst.get("last_used_at"):
        return None
    try:  # LXD sets last_used_at when the instance starts
        started = datetime.fromisoformat(inst["last_used_at"][:26].rstrip("Z") + "+00:00").timestamp()
    except ValueError:
        return None
    return max(0, int(now - started))


def describe(inst: dict[str, Any], now: float) -> dict[str, Any]:
    """Static + current facts about one instance (no rates yet)."""
    cfg = inst.get("config") or {}
    st = inst.get("state") or {}
    mem = st.get("memory") or {}
    root = (st.get("disk") or {}).get("root") or {}
    root_dev = (inst.get("expanded_devices") or {}).get("root") or {}
    rx, tx, ipv4 = _net(st.get("network"))
    image = cfg.get("image.description") or " ".join(
        filter(None, [cfg.get("image.os"), cfg.get("image.release")])) or ""
    return {
        "uuid": cfg.get("volatile.uuid") or inst["name"],
        "name": inst["name"],
        "status": inst.get("status"),
        "image": image,
        "description": inst.get("description") or "",
        "ephemeral": bool(inst.get("ephemeral")),
        "autostart": cfg.get("boot.autostart") == "true",
        "pool": root_dev.get("pool"),
        "ipv4": ipv4,
        "uptime_s": _uptime(inst, now),
        "procs": st.get("processes") if (st.get("processes") or 0) >= 0 else None,
        "cpus": int(cfg["limits.cpu"]) if cfg.get("limits.cpu", "").isdigit() else None,
        "cpu_allowance": cfg.get("limits.cpu.allowance") or None,
        "mem_used": mem.get("usage", 0),
        # LXD reports the limit as total when one is set, else host RAM.
        "mem_limit": mem.get("total", 0),
        "memory_mib": parse_size(cfg.get("limits.memory")) // 2**20 or None,
        "disk_used": root.get("usage") or None,
        "disk_limit": root.get("total") or (parse_size(root_dev.get("size")) or None),
        "disk_gib": (parse_size(root_dev.get("size")) / 2**30) or None,
        "owner_hint": cfg.get("user.hsm.owner_id"),
        "_cpu_ns": (st.get("cpu") or {}).get("usage", 0),
        "_rx": rx,
        "_tx": tx,
    }


@dataclass
class Rollup:
    samples: int = 0
    cpu_n: int = 0  # samples that had a CPU rate (the first one after a start has none)
    cpu_sum: float = 0.0
    cpu_max: float = 0.0
    mem_sum: float = 0.0
    mem_max: float = 0.0
    disk_used: float | None = None
    rx_bytes: float = 0.0
    tx_bytes: float = 0.0
    procs_max: float = 0.0
    running: int = 0

    def add(self, s: dict[str, Any], dt: float) -> None:
        self.samples += 1
        cpu = s.get("cpu_pct")
        if cpu is not None:
            self.cpu_n += 1
            self.cpu_sum += cpu
            self.cpu_max = max(self.cpu_max, cpu)
        self.mem_sum += s["mem_used"]
        self.mem_max = max(self.mem_max, s["mem_used"])
        if s.get("disk_used") is not None:
            self.disk_used = s["disk_used"]
        self.rx_bytes += (s.get("rx_bps") or 0) * dt
        self.tx_bytes += (s.get("tx_bps") or 0) * dt
        self.procs_max = max(self.procs_max, s.get("procs") or 0)
        self.running += s["status"] == "Running"

    def fields(self) -> dict[str, Any]:
        n = max(1, self.samples)
        return {"cpu_avg": self.cpu_sum / max(1, self.cpu_n), "cpu_max": self.cpu_max, "mem_avg": self.mem_sum / n,
                "mem_max": self.mem_max, "disk_used": self.disk_used, "rx_bytes": self.rx_bytes,
                "tx_bytes": self.tx_bytes, "procs_max": self.procs_max, "up_frac": self.running / n}


@dataclass
class Collector:
    settings: Settings
    lxd: Any
    db: Database
    writer: MetricWriter
    prev: dict[str, tuple[float, int, int, int]] = field(default_factory=dict)
    # tier -> uuid -> accumulator, and tier -> start of the open window
    rollups: dict[str, dict[str, Rollup]] = field(default_factory=lambda: {t: {} for t in ROLLUP_TIERS})
    windows: dict[str, int] = field(default_factory=dict)
    last_snapshot: dict[str, Any] = field(default_factory=dict)
    last_ok: float | None = None
    lxd_ok: bool | None = None
    last_prune: float = 0.0
    polls: int = 0

    def tick(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        self.polls += 1
        t0 = time.monotonic()
        try:
            instances = self.lxd.instances_with_state()
        except LXDError as exc:
            return self._lxd_down(now, str(exc))
        poll_ms = (time.monotonic() - t0) * 1000
        if self.lxd_ok is False:
            log.info("LXD reachable again")
        self.lxd_ok, self.last_ok = True, now

        samples = [self._sample(describe(i, now), now) for i in instances]
        self._write(now, samples, poll_ms)
        self._reconcile(now, samples)
        snapshot = {
            "ts": now, "lxd_ok": True, "error": None, "last_ok": now, "poll_ms": round(poll_ms, 1),
            "interval": self.settings.poll_interval,
            "containers": {s["name"]: {k: v for k, v in s.items() if not k.startswith("_")} for s in samples},
        }
        self.last_snapshot = snapshot
        self.writer.publish_latest(snapshot)
        if now - self.last_prune > 60:
            self.last_prune = now
            if removed := self.writer.prune(now):
                log.info("retention: removed %d old segment(s)", removed)
        return snapshot

    def _lxd_down(self, now: float, error: str) -> dict[str, Any]:
        if self.lxd_ok is not False:
            log.warning("LXD unavailable: %s (will keep retrying every %.0fs)", error, self.settings.poll_interval)
        self.lxd_ok = False
        self.prev.clear()  # rates across a gap would be misleading
        self.writer.write(RAW, now, [("lxd", {"host": "local"}, {"up": 0})])
        snapshot = dict(self.last_snapshot or {"containers": {}})
        snapshot.update({"ts": now, "lxd_ok": False, "error": error, "last_ok": self.last_ok,
                         "interval": self.settings.poll_interval})
        self.writer.publish_latest(snapshot)
        return snapshot

    def _sample(self, s: dict[str, Any], now: float) -> dict[str, Any]:
        prev = self.prev.get(s["uuid"])
        s["cpu_pct"] = s["rx_bps"] = s["tx_bps"] = None
        if prev and now > prev[0]:
            dt = now - prev[0]
            # Percent of ONE core, like `top` (200% = two busy cores).
            s["cpu_pct"] = round(max(0, s["_cpu_ns"] - prev[1]) / (dt * 1e9) * 100, 2)
            s["rx_bps"] = round(max(0, s["_rx"] - prev[2]) / dt)
            s["tx_bps"] = round(max(0, s["_tx"] - prev[3]) / dt)
        self.prev[s["uuid"]] = (now, s["_cpu_ns"], s["_rx"], s["_tx"])
        return s

    def _write(self, now: float, samples: list[dict[str, Any]], poll_ms: float) -> None:
        points = [("lxd", {"host": "local"}, {"up": 1, "poll_ms": poll_ms})]
        for s in samples:
            # Raw rows stay lean (TinyFlux repeats field names per row): limits and
            # names are static and live in latest.json / the rollups instead.
            points.append(("ct", {"uuid": s["uuid"]}, {
                "cpu_pct": s["cpu_pct"], "mem_used": s["mem_used"], "disk_used": s["disk_used"],
                "rx_bps": s["rx_bps"], "tx_bps": s["tx_bps"], "procs": s["procs"],
                "running": 1 if s["status"] == "Running" else 0}))
        self.writer.write(RAW, now, points)

        for tier in ROLLUP_TIERS:
            step = TIERS[tier].step
            window = int(now // step) * step
            if self.windows.get(tier) not in (None, window):
                self.flush(tier)
            self.windows[tier] = window
            acc = self.rollups[tier]
            for s in samples:
                acc.setdefault(s["uuid"], Rollup()).add(s, self.settings.poll_interval)
        self.prev = {k: v for k, v in self.prev.items() if k in {s["uuid"] for s in samples}}

    def flush(self, tier: str) -> None:
        """Write one point per container for the window that just closed."""
        acc, window = self.rollups[tier], self.windows.get(tier)
        if acc and window is not None:
            self.writer.write(tier, window, [(TIERS[tier].measurement, {"uuid": u}, r.fields())
                                             for u, r in acc.items()])
        acc.clear()

    def flush_rollups(self) -> None:
        for tier in ROLLUP_TIERS:
            self.flush(tier)

    def _reconcile(self, now: float, samples: list[dict[str, Any]]) -> None:
        """Keep `containers` in step with LXD, keyed by instance uuid."""
        seen = {s["uuid"]: s for s in samples}
        with self.db.transaction() as db:
            rows = {r["uuid"]: r for r in db.all("SELECT * FROM containers")}
            renamed = [u for u, s in seen.items() if u in rows and rows[u]["name"] != s["name"]]
            for u in renamed:  # two-step so a name swap cannot hit UNIQUE(name)
                db.run("UPDATE containers SET name = ? WHERE uuid = ?", (f"~{u}", u))
            for u, s in seen.items():
                if u in rows:
                    db.run("UPDATE containers SET name = ?, status = ?, cpus = ?, memory_mib = ?, disk_gib = ?,"
                           " last_seen_at = ? WHERE uuid = ?",
                           (s["name"], s["status"], s["cpus"], s["memory_mib"], s["disk_gib"], int(now), u))
                else:
                    owner = _int(s.get("owner_hint"))
                    if owner and not db.get_user(owner):
                        owner = None
                    db.run("DELETE FROM containers WHERE name = ?", (s["name"],))  # stale same-name row
                    db.run("INSERT INTO containers (uuid, name, owner_id, cpus, memory_mib, disk_gib, status,"
                           " created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (u, s["name"], owner, s["cpus"], s["memory_mib"], s["disk_gib"], s["status"],
                            int(now), int(now)))
                    db.audit("container_discovered", target=s["name"], detail=f"uuid={u}")
            for u in renamed:
                db.audit("container_renamed", target=seen[u]["name"], detail=f"was {rows[u]['name']}")
            # Rows recorded in the last 30 s may belong to a create that finished
            # after our LXD read began; never drop those.
            for u, r in rows.items():
                if u not in seen and r["created_at"] < now - 30:
                    db.run("DELETE FROM containers WHERE uuid = ?", (u,))  # assignments cascade
                    db.audit("container_vanished", target=r["name"], detail="deleted outside the dashboard")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def run(settings: Settings) -> None:
    db = Database(settings.sqlite_path)
    writer = MetricWriter(settings.tinyflux_dir, settings.retention)
    collector = Collector(settings, LXD(settings.lxd_endpoint, settings.lxd_verify_cert), db, writer)
    stopping = False

    def stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("collector started: every %.0fs -> %s", settings.poll_interval, settings.tinyflux_dir)
    next_tick = time.monotonic()
    while not stopping:
        try:
            collector.tick()
        except Exception:  # never let one bad tick kill collection
            log.exception("collector tick failed")
        next_tick += settings.poll_interval
        # Fixed schedule (no drift); if a tick overran, skip ahead instead of bursting.
        while next_tick < time.monotonic():
            next_tick += settings.poll_interval
        while not stopping and time.monotonic() < next_tick:
            time.sleep(min(1.0, next_tick - time.monotonic()))
    collector.flush_rollups()  # keep the partial 5-min window
    writer.close()
    db.close()
    log.info("collector stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(Settings.from_env())


if __name__ == "__main__":
    main()
