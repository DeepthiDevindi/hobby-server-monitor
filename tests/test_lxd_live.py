"""Read-only smoke tests against the real LXD daemon (container `test1`).
Skipped when the socket is not accessible, e.g. in CI."""
from __future__ import annotations

import asyncio
import os

import pytest

from app.lxd import LXDClient, LXDError
from app.metrics import MetricsHub

SOCKET = os.environ.get("LXD_SOCKET", "/var/snap/lxd/common/lxd/unix.socket")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.access(SOCKET, os.R_OK | os.W_OK), reason="LXD socket not accessible"),
]


@pytest.fixture
async def lxd():
    client = LXDClient(SOCKET)
    yield client
    await client.close()


async def test_list_with_state_is_one_call(lxd):
    instances = await lxd.list_instances(with_state=True)
    assert lxd.calls == 1
    test1 = next(i for i in instances if i["name"] == "test1")
    assert "memory" in test1["state"]


async def test_hub_computes_metrics(lxd):
    hub = MetricsHub(lxd, interval=1)
    await hub._poll()
    snap = await hub._poll()
    m = snap["containers"]["test1"]
    assert m["status"] in ("Running", "Stopped")
    if m["status"] == "Running":
        assert m["mem_used"] > 0 and m["cpu_pct"] is not None


async def test_missing_instance_is_404(lxd):
    with pytest.raises(LXDError) as e:
        await lxd.get_instance("definitely-not-here")
    assert e.value.status == 404


async def test_exec_roundtrip(lxd):
    if (await lxd.get_state("test1"))["status"] != "Running":
        pytest.skip("test1 not running")
    s = await lxd.exec_interactive("test1", 80, 24)
    try:
        await s.send(b"echo live-$((40+2))\n")
        out = b""
        while b"live-42" not in out:
            out += await asyncio.wait_for(s.recv(), 5)
    finally:
        await s.close()
