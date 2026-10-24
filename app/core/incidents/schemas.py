"""Pydantic contracts for core/incidents.

Owned by: core/incidents. Local to this submodule except where a type is
reused verbatim from shared/schemas/ (API_DESIGN.md "Design conventions":
defined once, reused everywhere -- `Severity`, `IncidentStatus`,
`PostmortemStatus`, `ActionItemStatus` already exist there and are imported,
not redefined, here).

core/incidents owns BOTH `incidents` and `postmortems` (PROJECT_PLAN.md
section 9.4: "the incident and postmortem system of record") -- not two
separate submodules, matching DATABASE_DESIGN.md's original table-ownership
grouping.

`Incident`/`Postmortem` carry `organization_id` (and `Incident`/`Postmortem`
also `project_id`/`incident_id` respectively) per PROJECT_PLAN.md section
3.2 -- this was already true of the underlying ORM models
(`database/models/core_models.py`) before this file existed; these schemas
just expose it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.schemas import ActionItemStatus, IncidentStatus, PostmortemStatus, Severity

# --- Incidents -----------------------------------------------------------------


class IncidentCreate(BaseModel):
    """Request body for `create_incident`.

    `project_id` is optional: omitting it defaults to the organization's
    auto-created default project, so a small
    customer that never bothered creating a second project never has to
    think about `project_id` at all. A caller scoping incidents to a
    specific team's project supplies it explicitly.
    """

    title: str
    description: str
    severity: Severity
    project_id: uuid.UUID | None = None


class Incident(BaseModel):
    """An incident record, as returned by the read surface."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    title: str
    description: str
    status: IncidentStatus
    severity: Severity
    owner_team: str | None
    reported_by: uuid.UUID
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class IncidentUpdate(BaseModel):
    """Request body for `update_incident` (PATCH /incidents/{id}).

    All fields optional; only fields actually present in the request are
    changed (the service layer uses `model_fields_set`/`exclude_unset`, not
    an `is not None` check, so `owner_team` can be explicitly cleared to
    `null` -- an `is not None` check would make "clear this field" and
    "don't touch this field" indistinguishable).
    """

    status: IncidentStatus | None = None
    severity: Severity | None = None
    owner_team: str | None = None


class IncidentFilter(BaseModel):
    """Filter + pagination for `list_incidents`. All
    filters optional and AND-combined, mirroring
    `core.audit.schemas.AuditLogQuery`'s existing convention.
    """

    status: IncidentStatus | None = None
    severity: Severity | None = None
    owner_team: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


# --- Timeline --------------------------------------------------------------------


class TimelineNoteCreate(BaseModel):
    """Request body for `add_timeline_note` -- a manual, human-authored
    timeline entry (API_DESIGN.md: `POST /incidents/{id}/timeline`).
    Agent-authored timeline entries (e.g. investigation evidence) are a
    separate, not-yet-built write path -- this schema is specifically for
    the human-note case named in the API.
    """

    note: str


class TimelineEntry(BaseModel):
    """One chronological entry on an incident, as returned by the read
    surface.
    """

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    incident_id: uuid.UUID
    event_type: str
    event_data: dict
    actor: str
    occurred_at: datetime


# --- Postmortems -----------------------------------------------------------------


class ActionItem(BaseModel):
    """A single postmortem action item. Local to this
    module, not shared/schemas/ -- nothing outside core/incidents references
    it yet.
    """

    description: str
    owner: str | None = None
    status: ActionItemStatus = "open"


class Postmortem(BaseModel):
    """A postmortem report, as returned by the read surface."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    incident_id: uuid.UUID
    status: PostmortemStatus
    root_cause: str | None
    action_items: list[ActionItem]
    generated_by: str
    reviewed_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class PostmortemUpdate(BaseModel):
    """Request body for `update_postmortem` (PATCH /postmortems/{id}) --
    human edits during review. `status` here is deliberately restricted to
    `draft`/`in_review` only: moving to `approved` is exclusively
    `approve_postmortem`'s job (its own dedicated, auditable gate per
    API_DESIGN.md's rationale), not something a generic PATCH can do --
    enforced by this schema's type, not just a runtime check.
    """

    root_cause: str | None = None
    action_items: list[ActionItem] | None = None
    status: Literal["draft", "in_review"] | None = None
