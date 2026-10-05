"""Smoke tests against the real LXD daemon (needs container `test1` running).
Read-only apart from exec. Skipped when the socket is not accessible (CI)."""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from hsm.collector import Collector
from hsm.db import Database
from hsm.lxd import LXD, LXDError
from hsm.tsdb import MetricWriter
from hsm.web.terminal import Shell

SOCKET = "/var/snap/lxd/common/lxd/unix.socket"
pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.access(SOCKET, os.R_OK | os.W_OK), reason="LXD socket not accessible")]


@pytest.fixture
def lxd():
    return LXD(f"unix://{SOCKET}")


@pytest.fixture
def running(lxd):
    if lxd.state("test1")["status"] != "Running":
        pytest.skip("test1 is not running")


def test_bulk_read_and_host_facts(lxd):
    names = {i["name"] for i in lxd.instances_with_state()}
    assert "test1" in names
    assert lxd.host_resources()["cpus"] >= 1
    assert any(p["driver"] for p in lxd.storage_pools())


def test_missing_instance_is_404(lxd):
    with pytest.raises(LXDError) as e:
        lxd.instance("definitely-not-here")
    assert e.value.status == 404


def test_collector_tick_on_real_lxd(lxd, settings):
    col = Collector(settings, lxd, Database(settings.sqlite_path), MetricWriter(settings.tinyflux_dir, settings.retention))
    col.tick(time.time())
    time.sleep(1)
    snap = col.tick(time.time())
    t1 = snap["containers"]["test1"]
    assert snap["lxd_ok"] and t1["uuid"] and t1["mem_used"] >= 0
    assert col.db.container_by_name("test1")["uuid"] == t1["uuid"]


def test_exec_output_is_capped(lxd, running):
    r = lxd.execute("test1", ["timeout", "-s", "KILL", "5", "/bin/sh", "-c", "yes | head -c 100000; exit 3"], 1000)
    assert r["exit_code"] == 3 and len(r["stdout"]) == 1000 and r["truncated"] == 99000


async def test_interactive_shell_roundtrip(lxd, running):
    state = SimpleNamespace(lxd=lxd, settings=SimpleNamespace(lxd_socket=SOCKET))
    sh = await Shell.open(state, "test1")
    try:
        await sh.data.send(b"echo live-$((40+2))\n")
        out = b""
        while b"live-42" not in out:
            out += await asyncio.wait_for(sh.data.recv(), 5)
    finally:
        await sh.close()
