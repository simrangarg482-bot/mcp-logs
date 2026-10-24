"""It provides a read-only way to fetch incidents and approved/published postmortems 
without importing `core.incidents.service`, which indirectly depends on the Agents 
module. This prevents unwanted dependencies and circular import issues. Ingestion uses 
it to retrieve incidents and postmortems for search indexing. The main service re-exports 
these functions so existing callers continue working without changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.incidents import repository
from app.core.incidents.schemas import Incident, Postmortem


async def list_postmortems_for_ingestion(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    since: datetime | None,
    offset: int,
    limit: int,
) -> list[Postmortem]:
    """It fetches approved or published postmortems for a specific organization, ordered 
    oldest first, with pagination. It supports the ingestion system in indexing 
    postmortems so the Investigation Agent can retrieve them as evidence. It doesn’t 
    require a human identity or check user permissions because it is designed for a 
    scheduled background worker that performs read-only lookups.
    """
    rows = await repository.list_postmortems_for_ingestion(
        session,
        organization_id,
        statuses=("approved", "published"),
        since=since,
        offset=offset,
        limit=limit,
    )
    return [Postmortem.model_validate(row) for row in rows]


async def list_incidents_for_ingestion(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    since: datetime | None,
    offset: int,
    limit: int,
) -> list[tuple[Incident, str | None]]:
    """Return `(incident, resolution_root_cause)` pairs for
    `organization_id`, oldest-last-modified-first, offset-paginated --
    backs `app.ingestion.connectors.incidents.IncidentsConnector`. See
    `repository.list_incidents_for_ingestion`'s own docstring for the
    resolution join and the `updated_at`-vs-`created_at` ordering choice.

    Same "no `actor: Identity` parameter" exception as
    `list_postmortems_for_ingestion` above, for the identical reason: the
    caller is `ingestion`'s worker, a scheduled system job, not a
    per-request human identity.
    """
    rows = await repository.list_incidents_for_ingestion(
        session, organization_id, since=since, offset=offset, limit=limit
    )
    return [(Incident.model_validate(row.Incident), row.root_cause) for row in rows]
