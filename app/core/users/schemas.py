"""This module defines the Pydantic schemas for `core/users`. `Permission` and `Role` are 
created directly from their database ORM objects, while `UserProfile` is built by 
combining a user record with its roles and permissions. The key difference is that 
`Identity` is a small, security-focused object used to check permissions during every 
request, whereas `UserProfile` contains richer user information for display and 
user-management endpoints. They serve different purposes: authorization versus display 
and administration.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Permission(BaseModel):
    """A single permission code, e.g. `incident:write`."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    code: str
    description: str | None = None


class Role(BaseModel):
    """A named role, e.g. `incident_commander`."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    name: str
    description: str | None = None


class UserProfile(BaseModel):
    """A user together with their resolved access.

    `roles` is the list of assigned role names; `permissions` is the flattened,
    de-duplicated, sorted set of permission codes those roles grant -- the same
    set that lands in `Identity.permissions`. Sorted so the representation is
    deterministic (stable API responses, stable test assertions).
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    created_at: datetime
    updated_at: datetime


class OrganizationMember(BaseModel):
    """One user's membership summary within a single organization -- the
    read surface backing `GET /organizations/{id}/members` Deliberately not `UserProfile`: this is a listing
    of other users seen by an admin, not "my own resolved access," so it
    carries `roles` only (never a flattened `permissions` set for someone
    else's account) and omits anything password/credential-adjacent.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    roles: tuple[str, ...] = ()
    created_at: datetime


class UserCredentialLookup(BaseModel):
    """The minimum a caller verifying a password credential needs -- not a
    full `User` row.

    Exists so `core.auth.service` (which owns hashing/verification "core/auth verifies authentication") can check a
    password without core/users handing another module a raw ORM row, or
    core/auth reaching into `core.users.repository` directly (breaking the
    "call the owning layer's service.py, never its repository.py" convention
    every other cross-module call in this codebase follows).
    """

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    password_hash: str | None
    is_active: bool