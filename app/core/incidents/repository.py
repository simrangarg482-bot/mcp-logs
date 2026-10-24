"""Persistence for core/incidents -- incidents, incident timeline, postmortems.

Owned by: core/incidents. Pure data access, same discipline as every other
repository.py in this codebase: one statement per function, ORM rows in/out,
no business rules, no ORM->Pydantic mapping.

`update_incident`/`update_postmortem` are generic, dict-driven field updaters
(taking `**fields`) rather than one narrowly-named function per field
(contrast `core/tenancy/repository.py`'s `update_connector_config_sync_status`):
incidents and postmortems have enough independently-updatable fields that a
narrow function per field would multiply faster than it's worth, and the
service layer already computes exactly which fields changed via
`model_dump(exclude_unset=True)`, so handing that dict straight through here
is the more DRY choice. `**fields: Any` is a deliberate, narrow exception to
this project's strict mypy config -- these two functions are intentionally
generic/dynamic.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.incidents.schemas import IncidentFilter
from app.database.models.core_models import Incident, IncidentTimeline, Postmortem

# --- Incidents -----------------------------------------------------------------


async def insert_incident(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    title: str,
    description: str,
    severity: str,
    reported_by: uuid.UUID,
) -> Incident:
    """Create one incident row and return it with server defaults populated."""
    row = Incident(
        organization_id=organization_id,
        project_id=project_id,
        title=title,
        description=description,
        severity=severity,
        status="open",
        reported_by=reported_by,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_incident_by_id(session: AsyncSession, incident_id: uuid.UUID) -> Incident | None:
    """Fetch a single incident by primary key, or None if absent."""
    return await session.get(Incident, incident_id)


async def list_incidents(
    session: AsyncSession, organization_id: uuid.UUID, query: IncidentFilter
) -> Sequence[Incident]:
    """Return incidents belonging to `organization_id`, newest first,
    filtered/paginated per `query`.
    """
    stmt = select(Incident).where(Incident.organization_id == organization_id)

    if query.status is not None:
        stmt = stmt.where(Incident.status == query.status)
    if query.severity is not None:
        stmt = stmt.where(Incident.severity == query.severity)
    if query.owner_team is not None:
        stmt = stmt.where(Incident.owner_team == query.owner_team)

    stmt = stmt.order_by(Incident.created_at.desc()).limit(query.limit).offset(query.offset)

    result = await session.execute(stmt)
    return result.scalars().all()


async def list_incidents_by_severity_since(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    severities: Sequence[str],
    since: datetime,
) -> Sequence[Incident]:
    """Every incident in `organization_id` with `severity` in `severities`,
    created at or after `since` -- the bounded, indexed candidate query
    `core.proactive`'s `recurring_incident_severity` detector runs once per
    organization (`ix_incidents_org_severity` covers the equality half; the
    `created_at` range narrows a window that is already small in practice).
    Not `list_incidents`/`IncidentFilter`: that shape is a single-severity,
    no-window API browsing filter, not a rolling-window multi-severity
    detection query.
    """
    stmt = select(Incident).where(
        Incident.organization_id == organization_id,
        Incident.severity.in_(severities),
        Incident.created_at >= since,
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_incidents_for_ingestion(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    since: datetime | None,
    offset: int,
    limit: int,
) -> Sequence[tuple[Incident, str | None]]:
    """Return `(incident, resolution_root_cause)` pairs for
    `organization_id`, ordered oldest-last-modified-first, offset-paginated
    -- backs `app.ingestion.connectors.incidents.IncidentsConnector`, the
    same "re-ingest core-domain data through the normal pipeline" pattern
    `list_postmortems_for_ingestion` already established for postmortems.

    `resolution_root_cause` is a `LEFT OUTER JOIN` to this incident's
    approved/published postmortem (`get_postmortem_by_incident_id`'s "one
    postmortem per incident" invariant makes this join single-valued), or
    `None` if none exists yet or none has been approved -- an incident is
    still returned either way (never made conditional on having a
    resolution: see `IncidentsConnector.normalize`'s own docstring on why
    resolution must stay optional).

    Ordered/filtered by `GREATEST(incidents.updated_at, postmortems.
    updated_at)`, not `incidents.updated_at` alone -- a deliberate departure
    from `list_postmortems_for_ingestion`'s plain `created_at` ordering,
    because an incident and its resolution do not become stale together:
    approving a postmortem (`core.incidents.service.approve_postmortem`)
    updates only the `postmortems` row, not its parent incident, so an
    incident closed *before* its postmortem was approved would never
    resurface for a later incremental sync (`since` filtering on
    `incidents.updated_at` alone) once that resolution became available --
    silently missing exactly the "especially on close/update so resolution
    information is included" case this connector exists for.
    `func.greatest` ignores a NULL argument (no postmortem row) rather than
    propagating it, so this is safe for incidents with no postmortem at
    all.
    """
    last_modified = func.greatest(Incident.updated_at, Postmortem.updated_at)
    stmt = (
        select(Incident, Postmortem.root_cause)
        .outerjoin(
            Postmortem,
            (Postmortem.incident_id == Incident.id)
            & (Postmortem.status.in_(("approved", "published"))),
        )
        .where(Incident.organization_id == organization_id, Incident.deleted_at.is_(None))
        .order_by(last_modified.asc())
        .offset(offset)
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(last_modified >= since)
    result = await session.execute(stmt)
    return result.all()


async def update_incident(
    session: AsyncSession, incident_id: uuid.UUID, **fields: Any
) -> Incident | None:
    """Apply `fields` to an incident row, returning the updated row or None
    if it doesn't exist. See this module's docstring for why this is a
    generic updater rather than one function per field.
    """
    row = await session.get(Incident, incident_id)
    if row is None:
        return None
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    await session.refresh(row)
    return row


# --- Timeline --------------------------------------------------------------------


async def insert_timeline_entry(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
    event_type: str,
    event_data: dict,
    actor: str,
) -> IncidentTimeline:
    """Create one timeline entry and return it with server defaults populated.

    `actor` is the caller's `Identity.audit_tag` (or an agent's equivalent
    tagged string) -- resolved by the service layer, not here, matching
    every other module's convention for the human-vs-AI tagged actor field.
    """
    row = IncidentTimeline(
        organization_id=organization_id,
        incident_id=incident_id,
        event_type=event_type,
        event_data=event_data,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_timeline_entries(
    session: AsyncSession, incident_id: uuid.UUID
) -> Sequence[IncidentTimeline]:
    """Return every timeline entry for `incident_id`, in chronological order
    (API_DESIGN.md: `GET /incidents/{id}/timeline` -- always read in order
    for one incident, matching the index this table already has).
    """
    stmt = (
        select(IncidentTimeline)
        .where(IncidentTimeline.incident_id == incident_id)
        .order_by(IncidentTimeline.occurred_at.asc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


# --- Postmortems -----------------------------------------------------------------


async def insert_postmortem(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
    root_cause: str | None,
    action_items: list[dict],
    generated_by: str,
) -> Postmortem:
    """Create one postmortem row a postmortem draft is never treated as
    final regardless of how it was generated and return it with server
    defaults populated.
    """
    row = Postmortem(
        organization_id=organization_id,
        incident_id=incident_id,
        status="draft",
        root_cause=root_cause,
        action_items=action_items,
        generated_by=generated_by,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_postmortem_by_id(
    session: AsyncSession, postmortem_id: uuid.UUID
) -> Postmortem | None:
    """Fetch a single postmortem by primary key, or None if absent."""
    return await session.get(Postmortem, postmortem_id)


async def get_postmortem_by_incident_id(
    session: AsyncSession, incident_id: uuid.UUID
) -> Postmortem | None:
    """Fetch the postmortem for a given incident, or None if none exists yet.

    Used to enforce "one postmortem per incident" before creating a new
    draft, and to back a future `GET /incidents/{id}/postmortem`-style
    lookup.
    """
    stmt = select(Postmortem).where(Postmortem.incident_id == incident_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_postmortems_by_organization(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    statuses: Sequence[str],
    limit: int,
) -> Sequence[Postmortem]:
    """Return the most recent postmortems for `organization_id` whose
    `status` is in `statuses`, newest first.

    Backs the Investigation Agent's "existing postmortems" evidence source there
    is no embedding-based search over postmortems (`retrieval_models.py`'s
    own module docstring: no "incidents"/postmortem collection exists yet),
    so this is a plain recency-ordered list, not a relevance-ranked one.
    Organization-scoped, not project-scoped: `postmortems` has no
    `project_id` column (DATABASE_DESIGN.md's original schema never gave it
    one), and a precedent from one project's incident can still be useful
    investigative context for another project's, unlike retrieval's
    per-project chunk filtering.
    """
    stmt = (
        select(Postmortem)
        .where(Postmortem.organization_id == organization_id, Postmortem.status.in_(statuses))
        .order_by(Postmortem.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_postmortems_for_ingestion(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    statuses: Sequence[str],
    since: datetime | None = None,
    offset: int = 0,
    limit: int,
) -> Sequence[Postmortem]:
    """Return postmortems for `organization_id` whose `status` is in
    `statuses`, oldest-`created_at`-first, offset-paginated -- backs the
    Milestone 9 runbooks/incident-report ingestion connector
    (`app.ingestion.connectors.runbooks`), NOT the Investigation Agent's
    evidence source above (that one is `list_postmortems_by_organization`:
    newest-first, no pagination, a small fixed `limit` -- a fundamentally
    different access pattern: "give me recent context" vs. "walk everything,
    resumably, oldest first" ).

    Ascending order, not descending: matches every other Milestone 9
    connector's own "ORDER BY ... ASC" incremental-sync convention
    (`JiraConnector`'s WIQL, `AzureDevOpsConnector`'s WIQL, `ConfluenceConnector`'s
    CQL), so a paginated walk started today and resumed tomorrow sees stable,
    non-overlapping pages.

    Offset-based, not keyset-based: postmortem approval is a rare,
    human-gated event (one per resolved incident, not a high-volume stream),
    so the usual "a concurrent insert shifts the window" offset-pagination
    risk is a low-priority, documented tradeoff here, not a correctness-
    critical gap -- unlike a chat/commit-firehose source, which is why
    those connectors don't use plain offsets.
    """
    stmt = (
        select(Postmortem)
        .where(Postmortem.organization_id == organization_id, Postmortem.status.in_(statuses))
        .order_by(Postmortem.created_at.asc())
        .offset(offset)
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(Postmortem.created_at >= since)
    result = await session.execute(stmt)
    return result.scalars().all()


async def update_postmortem(
    session: AsyncSession, postmortem_id: uuid.UUID, **fields: Any
) -> Postmortem | None:
    """Apply `fields` to a postmortem row, returning the updated row or None
    if it doesn't exist. See this module's docstring for why this is a
    generic updater rather than one function per field.
    """
    row = await session.get(Postmortem, postmortem_id)
    if row is None:
        return None
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    await session.refresh(row)
    return row
