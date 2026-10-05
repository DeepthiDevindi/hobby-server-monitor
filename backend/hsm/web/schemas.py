"""Request bodies (pydantic). Unknown fields are rejected everywhere.

These check *shape*; bounds that depend on the host or on a quota (max CPUs,
memory, disk, pool, network, profile) are re-validated in the handlers
against live LXD data, never trusted from the form.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NAME_PATTERN = r"^[a-z0-9-]{1,63}$"
LXD_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"  # pool / network / profile names


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContainerCreate(Strict):
    name: str = Field(pattern=NAME_PATTERN)
    image: str = Field(max_length=64)
    cpus: int = Field(ge=1, le=1024)
    cpu_allowance: int = Field(default=100, ge=5, le=100)  # % of each core
    memory_mib: int = Field(ge=1, le=2**22)
    disk_gib: int | None = Field(default=None, ge=1, le=65536)
    pool: str = Field(pattern=LXD_NAME)
    network: str | None = Field(default=None, pattern=LXD_NAME)
    profiles: list[str] = Field(default_factory=lambda: ["default"], max_length=8)
    ephemeral: bool = False
    autostart: bool = False
    description: str = Field(default="", max_length=200)
    owner_id: int | None = Field(default=None, ge=1)
    start: bool = True

    @field_validator("name")
    @classmethod
    def lxd_hostname(cls, v: str) -> str:
        # LXD names must be valid hostnames: start with a letter, no trailing '-'.
        if not v[0].isalpha() or v.endswith("-"):
            raise ValueError("must start with a letter and not end with '-'")
        return v

    @field_validator("profiles")
    @classmethod
    def profile_names(cls, v: list[str]) -> list[str]:
        if not v or any(not re.fullmatch(LXD_NAME[1:-1], p) for p in v) or len(set(v)) != len(v):
            raise ValueError("invalid profile list")
        return v

    @field_validator("description")
    @classmethod
    def printable(cls, v: str) -> str:
        if any(ord(c) < 32 for c in v):
            raise ValueError("control characters are not allowed")
        return v


class LimitsUpdate(Strict):
    cpus: int | None = Field(default=None, ge=1, le=1024)
    cpu_allowance: int | None = Field(default=None, ge=5, le=100)
    memory_mib: int | None = Field(default=None, ge=1, le=2**22)
    disk_gib: int | None = Field(default=None, ge=1, le=65536)


class StateAction(Strict):
    action: Literal["start", "stop", "restart", "freeze", "unfreeze"]


class OwnerUpdate(Strict):
    owner_id: int | None = Field(default=None, ge=1)


class ExecRequest(Strict):
    command: str = Field(min_length=1, max_length=4096)


class Quota(Strict):
    cpus: int | None = Field(default=None, ge=0, le=1024)
    memory_mib: int | None = Field(default=None, ge=0, le=2**22)
    disk_gib: int | None = Field(default=None, ge=0, le=65536)


class Invite(Strict):
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Literal["admin", "user"] = "user"
    quota: Quota = Field(default_factory=Quota)


class UserUpdate(Strict):
    role: Literal["admin", "user"] | None = None
    quota: Quota | None = None
