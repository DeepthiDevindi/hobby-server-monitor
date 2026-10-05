"""Pydantic models for every request body. Unknown fields are rejected."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Spec regex, used for every container name coming from a path or body.
NAME_PATTERN = r"^[a-z0-9-]{1,63}$"
NAME_RE = re.compile(NAME_PATTERN)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContainerCreate(Strict):
    name: str = Field(pattern=NAME_PATTERN)
    image: str = Field(max_length=64)  # checked against the allowlist in the route
    cpus: int = Field(ge=1, le=64)
    memory_mib: int = Field(ge=128, le=262144)

    @field_validator("name")
    @classmethod
    def lxd_hostname_rules(cls, v: str) -> str:
        # LXD also requires a valid hostname; reject early with a clear message.
        if v.startswith("-") or v.endswith("-") or v[0].isdigit():
            raise ValueError("must start with a letter and not end with '-'")
        return v


class LimitsUpdate(Strict):
    cpus: int | None = Field(default=None, ge=1, le=64)
    memory_mib: int | None = Field(default=None, ge=128, le=262144)


class ContainerAction(Strict):
    action: Literal["start", "stop", "restart"]


class RoleUpdate(Strict):
    role: Literal["admin", "user"]


class UserCreate(Strict):
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Literal["admin", "user"] = "user"

