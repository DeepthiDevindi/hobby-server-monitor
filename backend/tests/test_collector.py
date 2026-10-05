"""Collector + TSDB: independent of the UI, resilient, bounded, schema-safe."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hsm import tsdb
from hsm.collector import Collector
from hsm.db import Database
from hsm.tsdb import MetricWriter
from .conftest import FakeLXD, instance

T0 = 1_790_000_000.0  # fixed clock: 2026-09-21 ~14:13 UTC


@pytest.fixture
def col(settings):
    db = Database(settings.sqlite_path)
    w = MetricWriter(settings.tinyflux_dir, settings.retention)
    return Collector(settings, FakeLXD(), db, w)


def test_one_lxd_call_per_tick(col):
    for i in range(5):
        col.tick(T0 + 10 * i)
    assert col.lxd.calls == 5  # independent of containers and of browsers


def test_samples_rollups_and_latest(col, settings):
    for i in range(32):  # > 5 minutes of 10 s ticks
        col.lxd.instances["test1"]["state"]["cpu"]["usage"] = int(i * 5e8)  # 0.5 s CPU per 10 s = 5%
        col.tick(T0 + 10 * i)
    latest = tsdb.read_latest(settings.tinyflux_dir)
    assert latest["lxd_ok"] and latest["containers"]["test1"]["cpu_pct"] == 5.0
    assert latest["containers"]["test1"]["ipv4"] == "10.0.0.2"
    raw = tsdb.query(settings.tinyflux_dir, tsdb.RAW, "uuid-test1", T0, T0 + 400)
    assert len(raw) == 32
    roll = tsdb.query(settings.tinyflux_dir, tsdb.R5M, "uuid-test1", T0 - 300, T0 + 400)
    assert roll and abs(roll[-1][1]["cpu_avg"] - 5.0) < 0.01 and roll[-1][1]["up_frac"] == 1.0


def test_keeps_running_when_lxd_is_down(col, settings):
    col.tick(T0)
    col.lxd.down = True
    snap = col.tick(T0 + 10)
    assert snap["lxd_ok"] is False and "unreachable" in snap["error"]
    assert "test1" in snap["containers"]  # last known values stay visible (marked stale)
    col.lxd.down = False
    assert col.tick(T0 + 20)["lxd_ok"] is True


def test_rename_keeps_owner_and_grants(col):
    db = col.db
    col.tick(T0)
    bob = db.invite_user("bob@example.com", "user", None, {})
    db.assign(bob["id"], "uuid-test1", None)
    inst = col.lxd.instances.pop("test1")
    inst["name"] = "renamed"
    col.lxd.instances["renamed"] = inst
    col.tick(T0 + 10)
    assert db.can_access(bob["id"], "renamed") and not db.can_access(bob["id"], "test1")
    assert any(a["action"] == "container_renamed" for a in db.list_audit(10, None))


def test_outside_delete_drops_grants_and_same_name_is_not_inherited(col):
    db = col.db
    col.tick(T0)
    bob = db.invite_user("bob@example.com", "user", None, {})
    db.assign(bob["id"], "uuid-test1", None)
    del col.lxd.instances["test1"]                       # `lxc delete test1`
    col.tick(T0 + 60)
    assert db.container_by_name("test1") is None and not db.can_access(bob["id"], "test1")
    col.lxd.instances["test1"] = instance("test1", "uuid-new")  # `lxc launch ... test1` again
    col.tick(T0 + 70)
    assert db.container_by_name("test1")["uuid"] == "uuid-new"
    assert not db.can_access(bob["id"], "test1")  # new box, no inherited access


def test_fresh_rows_are_not_dropped_by_a_racing_tick(col):
    col.db.record_container("uuid-just-made", "just-made", None, None, 1, 512, 4)  # created_at = now
    col.tick(time.time())  # LXD list does not include it yet
    assert col.db.container_by_name("just-made") is not None


def test_owner_recovered_from_lxd_config(col):
    bob = col.db.invite_user("bob@example.com", "user", None, {})
    inst = instance("orphan", "uuid-orphan")
    inst["config"]["user.hsm.owner_id"] = str(bob["id"])
    col.lxd.instances["orphan"] = inst
    col.tick(T0)
    assert col.db.container_by_name("orphan")["owner_id"] == bob["id"]


def test_retention_deletes_whole_segments(col, settings):
    root = Path(settings.tinyflux_dir)
    col.last_prune = float("inf")  # disable the automatic once-a-minute prune
    col.tick(T0 - 26 * 3600)       # older than 6 h raw retention
    col.flush_rollups()
    col.tick(T0 - 31 * 86400)      # older than 30 d 1h-tier retention
    col.flush_rollups()
    col.tick(T0)
    before = {p.name for p in (root / "raw").iterdir()}
    removed = col.writer.prune(T0)
    after = {p.name for p in (root / "raw").iterdir()}
    assert removed >= 2 and len(after) < len(before)
    assert tsdb._segment_name(tsdb.RAW, T0) in after  # current segment kept


def test_storage_is_bounded(settings):
    """A month of samples for 10 containers stays a few tens of MB."""
    w = MetricWriter(settings.tinyflux_dir, settings.retention)
    # Realistic magnitudes: ~600 MiB memory, ~1 GiB disk, KB/s network.
    fields = {"cpu_pct": 12.34, "mem_used": 624115712, "disk_used": 1073741824, "rx_bps": 1530,
              "tx_bps": 420, "procs": 63, "running": 1}
    pts = [("ct", {"uuid": f"0edfa1d7-47db-4580-8333-41{i:010d}"}, fields) for i in range(10)]
    for k in range(36):  # 6 minutes worth of 10-container ticks
        w.write(tsdb.RAW, T0 + 10 * k, pts)
    w.close()
    size = sum(f.stat().st_size for f in (Path(settings.tinyflux_dir) / "raw").glob("*"))
    per_row = size / (36 * 10)
    # rows kept at steady state (defaults): 6 h raw + 7 d of 5-min + 30 d of 1-hour,
    # rollup rows ~1.5x wider than raw rows
    raw_rows = 10 * 6 * 360
    rollup_rows = 10 * (7 * 288 + 30 * 24)
    bound_mb = (raw_rows + 1.5 * rollup_rows) * per_row / 2**20
    print(f"bytes/raw row {per_row:.0f}; 10 containers steady state ~ {bound_mb:.1f} MB")
    assert bound_mb < 15, bound_mb


def test_history_never_sends_more_than_max_points(settings):
    w = MetricWriter(settings.tinyflux_dir, settings.retention)
    for k in range(6 * 360):  # 6 h of raw samples
        w.write(tsdb.RAW, T0 + 10 * k, [("ct", {"uuid": "u1", "name": "c"}, {"cpu_pct": k % 100, "mem_used": 1})])
    w.close()
    h = tsdb.history(settings.tinyflux_dir, "u1", 6 * 3600, settings.retention, now=T0 + 6 * 3600)
    assert h["tier"] == "raw" and len(h["points"]) <= tsdb.MAX_POINTS and h["step"] == 60


def test_long_ranges_use_rollups(settings):
    assert tsdb.history(settings.tinyflux_dir, "u1", 86400, settings.retention, now=T0)["tier"] == "5m"
    h = tsdb.history(settings.tinyflux_dir, "u1", 30 * 86400, settings.retention, now=T0)
    assert h["tier"] == "1h" and h["step"] >= 3600


def test_reader_survives_partial_last_row(settings):
    w = MetricWriter(settings.tinyflux_dir, settings.retention)
    w.write(tsdb.RAW, T0, [("ct", {"uuid": "u1"}, {"cpu_pct": 1.0})])
    w.close()
    seg = next((Path(settings.tinyflux_dir) / "raw").glob("*.tinyflux"))
    with open(seg, "a") as f:
        f.write(seg.read_text().splitlines()[0][:15])  # half-written row
    pts = tsdb.query(settings.tinyflux_dir, tsdb.RAW, "u1", T0 - 10, T0 + 10)
    assert len(pts) in (0, 1)  # no exception; at worst the segment is skipped
    assert os.path.exists(seg)
