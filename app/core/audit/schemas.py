"""Pydantic contracts for core/audit.

These are local to the audit submodule. `AuditLogEntry` is the boundary read model --
services convert ORM rows into it so a live SQLAlchemy object never crosses a
module boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AuditLogEntry(BaseModel):
    """This schema represents one audit-trail record returned by EKIP. With 
    `from_attributes=True`, Pydantic can create an `AuditLogEntry` directly from an 
    `AuditLog` ORM object without manually copying each field. The `event_metadata` 
    field maps to the ORM attribute, which corresponds to the PostgreSQL column 
    named `metadata`. `organization_id` is nullable to allow for possible future 
    platform-admin actions that aren't associated with a specific organization, although
    that use case hasn't been designed yet.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID | None
    actor: str
    action: str
    resource_type: str
    resource_id: uuid.UUID | None
    event_metadata: dict | None
    occurred_at: datetime


class AuditLogQuery(BaseModel):
    """This schema defines the filters and pagination used to read audit logs. All 
    filters are optional and are combined using AND, so an entry must match every filter
    provided. If no filters are supplied, it returns the most recent entries.
    `organization_id` is kept separate and mandatory in the query function, ensuring 
    callers cannot accidentally read audit logs belonging to other organizations.
    """

    resource_type: str | None = None
    resource_id: uuid.UUID | None = None
    actor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)