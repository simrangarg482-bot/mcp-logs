"""This module defines Pydantic data models for the knowledge document review process, 
including proposing and reviewing document updates. Its `Document` model matches the 
existing database document structure but is defined separately to avoid importing 
`app.ingestion`. This keeps `core/knowledge` independent of the ingestion and MCP 
modules, following the project's architecture rules.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.shared.schemas import DocumentStatus


class DocumentProposalCreate(BaseModel):
    """Request body for `propose_document`
    `propose_runbook_update` MCP tool: `{title, content, source_incident_id?}`).

    `project_id` is optional and not part of that documented tool contract,
    added here for REST/internal callers that want to scope a proposal to a
    specific project -- omitting it defaults to the organization's default
    project, mirroring `core.incidents.schemas.IncidentCreate`'s identical
    convention.
    """

    title: str
    content: str
    source_incident_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None


class DocumentUpdate(BaseModel):
    """Request body for `update_document` -- editing a still-proposed
    document during human review (`PATCH /knowledge/{document_id}`).

    `exclude_unset` semantics, matching `core.incidents.schemas.
    IncidentUpdate`/`PostmortemUpdate`: a field omitted from the request body
    is left untouched; only fields explicitly present get applied. Only
    `title`/`content` are editable here -- `status`/`project_id`/
    `source_incident_id` transition through their own dedicated actions
    (`publish_document`/`reject_document`), not a generic field-level PATCH.
    """

    title: str | None = None
    content: str | None = None


class Document(BaseModel):
    """A proposed or published document, as returned by core/knowledge's
    read surface.

    `content` is populated from the `document_metadata` `"content"` key --
    see `repository.py`'s module docstring for why that's a metadata row
    rather than a real column -- and is `None` only in the (currently
    unreached) case of fetching an ingestion-sourced row that never had one
    written under that key.
    """

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    title: str | None
    status: DocumentStatus
    version: int
    content: str | None = None
    source: str
    source_url: str | None = None
    source_incident_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
