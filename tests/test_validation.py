"""Input validation: container names, image allowlist, limits, unknown fields."""
from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from .conftest import ORIGIN, hdr, login

BAD_NAMES = ["Test1", "a_b", "a b", "a" * 64, "a.b", "%2e%2e", "a;rm", "ü", "a$(id)", "test1%0A"]


@pytest.fixture
def admin(client):
    return hdr(login(client, "boss@example.com", "admin"))


def _create(client, admin, **kw):
    body = {"name": "ok-1", "image": "ubuntu/24.04", "cpus": 1, "memory_mib": 256} | kw
    return client.post("/api/containers", json=body, headers=admin)


@pytest.mark.parametrize("name", BAD_NAMES + ["", "-lead", "trail-", "1abc"])
def test_create_rejects_invalid_names(client, admin, lxd, name):
    before = set(lxd.instances)
    assert _create(client, admin, name=name).status_code == 422
    assert set(lxd.instances) == before


@pytest.mark.parametrize("name", BAD_NAMES)
def test_path_rejects_invalid_names(client, admin, name):
    for method, path in [("GET", f"/api/containers/{name}"), ("DELETE", f"/api/containers/{name}"),
                         ("PATCH", f"/api/containers/{name}"), ("GET", f"/api/containers/{name}/metrics/stream"),
                         ("PUT", f"/api/admin/users/1/containers/{name}")]:
        r = client.request(method, path, headers=admin, json={"cpus": 1})
        assert r.status_code in (404, 422), (method, name, r.status_code)  # 404: path never matches a route


def test_terminal_rejects_invalid_name(client, lxd):
    login(client, "boss@example.com", "admin")
    with client.websocket_connect("/api/containers/BAD_NAME/terminal", headers={"Origin": ORIGIN}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_bytes()
    assert e.value.code == 4403 and lxd.execs == []


def test_image_allowlist(client, admin):
    assert _create(client, admin, image="images:alpine/edge").status_code == 422
    assert _create(client, admin, image="../../etc").status_code == 422


@pytest.mark.parametrize("kw", [{"cpus": 0}, {"cpus": 5}, {"memory_mib": 64}, {"memory_mib": 999999},
                                {"cpus": "1; reboot"}, {"privileged": True}])
def test_limits_and_extra_fields(client, admin, kw):
    assert _create(client, admin, **kw).status_code == 422


def test_bad_action_and_role(client, admin):
    assert client.post("/api/containers/test1/actions", json={"action": "freeze"}, headers=admin).status_code == 422
    assert client.patch("/api/admin/users/1", json={"role": "root"}, headers=admin).status_code == 422
    assert client.post("/api/admin/users", json={"email": "not-an-email"}, headers=admin).status_code == 422


def test_valid_create_passes(client, admin, lxd):
    assert _create(client, admin, name="good-name-2").status_code == 201
    assert "good-name-2" in lxd.instances
