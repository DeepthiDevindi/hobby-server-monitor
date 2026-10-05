"""Container CRUD and admin REST endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status

from .dependencies import (
    CurrentUser, require_admin, require_admin_container, require_container_access, require_user,
)
from .schemas import NAME_PATTERN, ContainerAction, ContainerCreate, LimitsUpdate, RoleUpdate, UserCreate
from .security import client_ip

router = APIRouter(prefix="/api")


def _actor(user: CurrentUser) -> dict[str, Any]:
    return {"id": user.id, "email": user.email}


def _audit(request: Request, user: CurrentUser, action: str, target: str | None = None, detail: str | None = None) -> None:
    request.app.state.db.audit(action, actor=_actor(user), target=target, detail=detail, ip=client_ip(request))


def container_view(inst: dict[str, Any]) -> dict[str, Any]:
    cfg = inst.get("config") or {}
    return {
        "name": inst.get("name"),
        "status": inst.get("status"),
        "image": cfg.get("image.description", ""),
        "cpus": cfg.get("limits.cpu", ""),
        "memory": cfg.get("limits.memory", ""),
        "created_at": inst.get("created_at"),
    }


def _check_limits(request: Request, cpus: int | None, memory_mib: int | None) -> dict[str, str]:
    s = request.app.state.settings
    if cpus is not None and cpus > s.max_cpus:
        raise HTTPException(422, f"cpus must be <= {s.max_cpus}")
    if memory_mib is not None and memory_mib > s.max_memory_mib:
        raise HTTPException(422, f"memory_mib must be <= {s.max_memory_mib}")
    cfg: dict[str, str] = {}
    if cpus is not None:
        cfg["limits.cpu"] = str(cpus)
    if memory_mib is not None:
        cfg["limits.memory"] = f"{memory_mib}MiB"
    return cfg


# ---- containers (read) -----------------------------------------------------
@router.get("/images")
async def images(request: Request, _: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    s = request.app.state.settings
    return {"images": sorted(s.images), "max_cpus": s.max_cpus, "max_memory_mib": s.max_memory_mib}


@router.get("/containers")
async def list_containers(request: Request, user: CurrentUser = Depends(require_user)) -> list[dict[str, Any]]:
    if not user.is_admin:
        allowed = request.app.state.db.assigned_containers(user.id)
        if not allowed:
            return []  # nothing assigned: do not even ask LXD
    instances = await request.app.state.lxd.list_instances()
    if not user.is_admin:
        instances = [i for i in instances if i.get("name") in allowed]
    return [container_view(i) for i in instances]


@router.get("/containers/{name}")
async def get_container(
    request: Request, name: str = Path(pattern=NAME_PATTERN), _: CurrentUser = Depends(require_container_access)
) -> dict[str, Any]:
    return container_view(await request.app.state.lxd.get_instance(name))


# ---- containers (admin writes) ---------------------------------------------
@router.post("/containers", status_code=201)
async def create_container(body: ContainerCreate, request: Request, user: CurrentUser = Depends(require_admin)) -> dict:
    source = request.app.state.settings.images.get(body.image)
    if source is None:
        raise HTTPException(422, "image is not in the allowlist")
    cfg = _check_limits(request, body.cpus, body.memory_mib)
    cfg["security.privileged"] = "false"
    cfg["security.nesting"] = "false"
    await request.app.state.lxd.create_instance(body.name, source, cfg)
    # Clear stale grants in case a same-named container was removed outside this tool.
    request.app.state.db.drop_container(body.name)
    _audit(request, user, "container_create", body.name, f"{body.image} cpus={body.cpus} mem={body.memory_mib}MiB")
    return container_view(await request.app.state.lxd.get_instance(body.name))


@router.post("/containers/{name}/actions")
async def container_action(
    body: ContainerAction, request: Request, name: str = Path(pattern=NAME_PATTERN),
    user: CurrentUser = Depends(require_admin_container),
) -> dict:
    await request.app.state.lxd.set_state(name, body.action)
    _audit(request, user, f"container_{body.action}", name)
    return container_view(await request.app.state.lxd.get_instance(name))


@router.patch("/containers/{name}")
async def update_limits(
    body: LimitsUpdate, request: Request, name: str = Path(pattern=NAME_PATTERN),
    user: CurrentUser = Depends(require_admin_container),
) -> dict:
    cfg = _check_limits(request, body.cpus, body.memory_mib)
    if not cfg:
        raise HTTPException(422, "nothing to update")
    await request.app.state.lxd.update_config(name, cfg)
    _audit(request, user, "container_update", name, ", ".join(f"{k}={v}" for k, v in cfg.items()))
    return container_view(await request.app.state.lxd.get_instance(name))


@router.delete("/containers/{name}", status_code=204)
async def delete_container(
    request: Request, name: str = Path(pattern=NAME_PATTERN), user: CurrentUser = Depends(require_admin_container)
) -> Response:
    lxd = request.app.state.lxd
    if (await lxd.get_instance(name)).get("status") == "Running":
        await lxd.set_state(name, "stop")
    await lxd.delete_instance(name)
    request.app.state.db.drop_container(name)
    _audit(request, user, "container_delete", name)
    return Response(status_code=204)


# ---- admin: users & assignments ---------------------------------------------
def _target_user(request: Request, user_id: int) -> dict[str, Any]:
    target = request.app.state.db.get_user(user_id)
    if not target:
        raise HTTPException(404, "User not found")
    return target


def _protect(request: Request, admin: CurrentUser, target: dict[str, Any]) -> None:
    """Admins cannot lock themselves out or demote the configured bootstrap admin."""
    if target["id"] == admin.id:
        raise HTTPException(400, "You cannot change your own role or delete yourself")
    if target["email"] == request.app.state.settings.bootstrap_admin_email:
        raise HTTPException(400, "The bootstrap admin is managed via BOOTSTRAP_ADMIN_EMAIL")


@router.get("/admin/users")
async def list_users(request: Request, _: CurrentUser = Depends(require_admin)) -> list[dict[str, Any]]:
    return request.app.state.db.list_users()


@router.post("/admin/users", status_code=201)
async def create_user(body: UserCreate, request: Request, admin: CurrentUser = Depends(require_admin)) -> dict:
    db = request.app.state.db
    if db.get_user_by_email(body.email):
        raise HTTPException(409, "User already exists")
    user = db.create_user(body.email, body.role)
    _audit(request, admin, "user_create", user["email"], f"role={body.role}")
    return user


@router.patch("/admin/users/{user_id}")
async def change_role(
    body: RoleUpdate, request: Request, user_id: int = Path(gt=0), admin: CurrentUser = Depends(require_admin)
) -> dict:
    target = _target_user(request, user_id)
    _protect(request, admin, target)
    request.app.state.db.set_role(user_id, body.role)
    _audit(request, admin, "role_change", target["email"], f"{target['role']} -> {body.role}")
    return _target_user(request, user_id)


@router.delete("/admin/users/{user_id}", status_code=204)
async def delete_user(request: Request, user_id: int = Path(gt=0), admin: CurrentUser = Depends(require_admin)) -> Response:
    target = _target_user(request, user_id)
    _protect(request, admin, target)
    request.app.state.db.delete_user(user_id)  # sessions + assignments cascade
    _audit(request, admin, "user_delete", target["email"])
    return Response(status_code=204)


@router.put("/admin/users/{user_id}/containers/{name}", status_code=204)
async def assign(
    request: Request, user_id: int = Path(gt=0), name: str = Path(pattern=NAME_PATTERN),
    admin: CurrentUser = Depends(require_admin),
) -> Response:
    target = _target_user(request, user_id)
    await request.app.state.lxd.get_instance(name)  # 404 if it does not exist: no orphan grants
    request.app.state.db.assign(user_id, name)
    _audit(request, admin, "assign", name, f"user={target['email']}")
    return Response(status_code=204)


@router.delete("/admin/users/{user_id}/containers/{name}", status_code=204)
async def unassign(
    request: Request, user_id: int = Path(gt=0), name: str = Path(pattern=NAME_PATTERN),
    admin: CurrentUser = Depends(require_admin),
) -> Response:
    target = _target_user(request, user_id)
    if not request.app.state.db.unassign(user_id, name):
        raise HTTPException(404, "Not assigned")
    _audit(request, admin, "unassign", name, f"user={target['email']}")
    return Response(status_code=204)


@router.get("/admin/audit")
async def audit_log(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    before: int | None = Query(None, ge=1),
    _: CurrentUser = Depends(require_admin),
) -> list[dict[str, Any]]:
    return request.app.state.db.list_audit(limit, before)
