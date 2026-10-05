"""Time-series storage on TinyFlux: three downsampling tiers, each split into
time segments (one TinyFlux file per hour or day).

Layout under TINYFLUX_DB_PATH (a directory):

    tier  resolution  segment file              kept (default)            used for
    raw   10 s        raw/2026100513.tinyflux   RAW_RETENTION_HOURS=6     charts up to 6 h
    5m    5 min       5m/20261005.tinyflux      ROLLUP_RETENTION_DAYS=7   24 h / 7 d charts
    1h    1 hour      1h/20261005.tinyflux      METRICS_RETENTION_DAYS=30 30 d charts, usage
    latest.json  newest snapshot for the live dashboard (atomic replace)

Why segments: TinyFlux parses a whole file to answer a query. Measured: one
file holding a day of 10 s samples for 5 containers (43k rows) took 0.34 s
and ~60 MB RSS per query. Small segments keep each read cheap, and retention
is "delete files older than X": bounded growth with no rewrite in place, so
the collector (sole writer) and the web API (reader) never race on a file.

Why tiers: TinyFlux stores CSV with field names on every row (~200 B/row).
Keeping 5-min rows for 30 days would cost ~45 MB/month for 10 containers;
tiering brings it to ~12 MB (see tests/test_collector.py::test_storage_is_bounded).

Measurements (tag: uuid = LXD volatile.uuid, so history survives renames):
    ct     raw     cpu_pct mem_used disk_used rx_bps tx_bps procs running
    ct5m   5m      cpu_avg cpu_max mem_avg mem_max disk_used rx_bytes tx_bytes procs_max up_frac
    ct1h   1h      (same fields as ct5m)
    lxd    raw     up (1/0) poll_ms          tag: host
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from tinyflux import Point, TagQuery, TimeQuery, TinyFlux

log = logging.getLogger("hsm.tsdb")
RAW, R5M, R1H = "raw", "5m", "1h"
MAX_POINTS = 360  # upper bound on points per series sent to the browser
RAW_FIELDS = ("cpu_pct", "mem_used", "disk_used", "rx_bps", "tx_bps", "procs", "running")


@dataclass(frozen=True)
class Tier:
    name: str
    step: int          # seconds per point (raw: the poll interval, nominal)
    seg_fmt: str       # strftime of one segment file
    seg_span: int      # seconds covered by one segment
    measurement: str


TIERS = {
    RAW: Tier(RAW, 10, "%Y%m%d%H", 3600, "ct"),
    R5M: Tier(R5M, 300, "%Y%m%d", 86400, "ct5m"),
    R1H: Tier(R1H, 3600, "%Y%m%d", 86400, "ct1h"),
}
ROLLUP_TIERS = (R5M, R1H)


def utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _segment_name(tier: str, ts: float) -> str:
    return utc(ts).strftime(TIERS[tier].seg_fmt) + ".tinyflux"


def _segment_start(tier: str, name: str) -> float:
    return datetime.strptime(name.split(".")[0], TIERS[tier].seg_fmt).replace(tzinfo=timezone.utc).timestamp()


def retention_seconds(raw_hours: int, rollup_days: int, days: int) -> dict[str, int]:
    return {RAW: raw_hours * 3600, R5M: rollup_days * 86400, R1H: days * 86400}


class MetricWriter:
    """Used only by the collector process (single writer)."""

    def __init__(self, root: str, retention: dict[str, int]) -> None:
        self.root = Path(root)
        self.retention = retention
        self._open: dict[str, tuple[str, TinyFlux]] = {}  # tier -> (segment name, db)
        for tier in TIERS:
            (self.root / tier).mkdir(parents=True, exist_ok=True)

    def _db(self, tier: str, ts: float) -> TinyFlux:
        name = _segment_name(tier, ts)
        current = self._open.get(tier)
        if current and current[0] == name:
            return current[1]
        if current:
            current[1].close()  # segment boundary crossed: rotate
        db = TinyFlux(self.root / tier / name, auto_index=False)
        self._open[tier] = (name, db)
        return db

    def write(self, tier: str, ts: float, points: list[tuple[str, dict[str, str], dict[str, Any]]]) -> None:
        """points: (measurement, tags, fields); one insert_multiple = one fsync."""
        if not points:
            return
        t = utc(ts)
        self._db(tier, ts).insert_multiple(
            [Point(time=t, measurement=m, tags=tags, fields=_clean(fields)) for m, tags, fields in points])

    def prune(self, now: float) -> int:
        """Delete whole segments past retention; returns files removed."""
        removed = 0
        for tier, keep in self.retention.items():
            open_name = self._open.get(tier, ("",))[0]
            for f in (self.root / tier).glob("*.tinyflux"):
                try:
                    end = _segment_start(tier, f.name) + TIERS[tier].seg_span
                except ValueError:
                    continue  # not ours
                if end < now - keep and f.name != open_name:
                    f.unlink(missing_ok=True)
                    removed += 1
        return removed

    def publish_latest(self, snapshot: dict[str, Any]) -> None:
        """Atomic write: readers see the old or the new file, never half."""
        tmp = self.root / "latest.json.tmp"
        tmp.write_text(json.dumps(snapshot, separators=(",", ":")))
        os.replace(tmp, self.root / "latest.json")

    def close(self) -> None:
        for _, db in self._open.values():
            db.close()
        self._open.clear()


def _clean(fields: dict[str, Any]) -> dict[str, float | int]:
    """TinyFlux fields are numeric; None is omitted. Whole numbers are stored
    as ints and rates rounded, which keeps CSV rows short (bytes stay exact)."""
    out: dict[str, float | int] = {}
    for k, v in fields.items():
        if v is None:
            continue
        v = float(v)
        out[k] = int(v) if v.is_integer() else round(v, 2)
    return out


# ---- reading (web API) -------------------------------------------------------
def disk_usage(root: str) -> dict[str, Any]:
    """Files and bytes on disk per tier (shown on the usage page)."""
    out = {}
    for tier in TIERS:
        files = list((Path(root) / tier).glob("*.tinyflux"))
        out[tier] = {"files": len(files), "bytes": sum(f.stat().st_size for f in files)}
    return out


def read_latest(root: str) -> dict[str, Any] | None:
    try:
        return json.loads((Path(root) / "latest.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _segments(root: str, tier: str, start: float, end: float) -> Iterable[Path]:
    for f in sorted((Path(root) / tier).glob("*.tinyflux")):
        try:
            s = _segment_start(tier, f.name)
        except ValueError:
            continue
        if s + TIERS[tier].seg_span > start and s <= end:
            yield f


def query(root: str, tier: str, uuid: str, start: float, end: float) -> list[tuple[float, dict[str, float]]]:
    """All points for one container in [start, end], oldest first."""
    q = (TagQuery().uuid == uuid) & (TimeQuery() >= utc(start)) & (TimeQuery() <= utc(end))
    out: list[tuple[float, dict[str, float]]] = []
    for path in _segments(root, tier, start, end):
        out.extend(_read_segment(path, TIERS[tier].measurement, q))
    out.sort(key=lambda p: p[0])
    return out


def _read_segment(path: Path, measurement: str, q: Any) -> list[tuple[float, dict[str, float]]]:
    # The collector may be appending to the newest segment; a half-written last
    # row can fail to parse. Retry briefly, then skip, instead of failing.
    for attempt in range(3):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                db = TinyFlux(path, access_mode="r")
            try:
                return [(p.time.timestamp(), dict(p.fields)) for p in db.search(q, measurement=measurement)]
            finally:
                db.close()
        except (ValueError, IndexError, OSError) as exc:
            if attempt == 2:
                log.warning("skipping unreadable segment %s: %s", path.name, exc)
                return []
            time.sleep(0.05)
    return []


# How each field combines when several points fall in one chart bucket.
_AGG = {"cpu_pct": "avg", "mem_used": "avg", "rx_bps": "avg", "tx_bps": "avg", "disk_used": "last",
        "procs": "max", "running": "avg",
        "cpu_avg": "avg", "cpu_max": "max", "mem_avg": "avg", "mem_max": "max", "rx_bytes": "sum",
        "tx_bytes": "sum", "procs_max": "max", "up_frac": "avg"}


def downsample(points: list[tuple[float, dict[str, float]]], start: float, end: float,
               max_points: int = MAX_POINTS, min_step: int = 10) -> tuple[int, list[dict[str, float]]]:
    """Bucket points so a chart never receives more than `max_points`."""
    step = max(min_step, math.ceil((end - start) / max_points))
    buckets: dict[int, list[dict[str, float]]] = {}
    for t, fields in points:
        buckets.setdefault(int((t - start) // step), []).append(fields)
    series = []
    for idx in sorted(buckets):
        rows = buckets[idx]
        out: dict[str, float] = {"t": start + idx * step}
        for key, how in _AGG.items():
            vals = [r[key] for r in rows if key in r]
            if vals:
                out[key] = (sum(vals) / len(vals) if how == "avg" else max(vals) if how == "max"
                            else sum(vals) if how == "sum" else vals[-1])
        series.append(out)
    return step, series


def pick_tier(range_seconds: int, retention: dict[str, int]) -> str:
    """Finest tier that still covers the whole range."""
    if range_seconds <= min(6 * 3600, retention[RAW]):
        return RAW
    if range_seconds <= retention[R5M]:
        return R5M
    return R1H


def history(root: str, uuid: str, range_seconds: int, retention: dict[str, int],
            now: float | None = None) -> dict[str, Any]:
    end = now or time.time()
    start = end - range_seconds
    tier = pick_tier(range_seconds, retention)
    points = query(root, tier, uuid, start, end)
    step, series = downsample(points, start, end, min_step=TIERS[tier].step)
    return {"tier": tier, "step": step, "start": start, "end": end, "points": series}


def consumption(root: str, uuid: str, start: float, end: float, retention: dict[str, int]) -> dict[str, Any]:
    """Usage accounting over a period, from the rollup tiers."""
    tier = R5M if end - start <= retention[R5M] else R1H
    pts = [f for _, f in query(root, tier, uuid, start, end)]
    if not pts:
        return {"samples": 0, "cpu_avg": None, "cpu_max": None, "mem_max": None,
                "rx_bytes": 0, "tx_bytes": 0, "up_frac": None, "cpu_core_hours": 0}
    step = TIERS[tier].step
    return {
        "samples": len(pts),
        "cpu_avg": round(sum(p.get("cpu_avg", 0) for p in pts) / len(pts), 2),
        "cpu_max": max(p.get("cpu_max", 0) for p in pts),
        "mem_max": max(p.get("mem_max", 0) for p in pts),
        "rx_bytes": sum(p.get("rx_bytes", 0) for p in pts),
        "tx_bytes": sum(p.get("tx_bytes", 0) for p in pts),
        "up_frac": round(sum(p.get("up_frac", 0) for p in pts) / len(pts), 3),
        # cpu_avg is % of one core: 100% for one hour = 1 core-hour
        "cpu_core_hours": round(sum(p.get("cpu_avg", 0) / 100 * step / 3600 for p in pts), 3),
    }
