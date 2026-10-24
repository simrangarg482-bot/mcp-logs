"""Owned by: core/audit. Pure data access: functions here take an AsyncSession,
issue exactly one statement, and return ORM rows (or None). They never commit,
never map to Pydantic, and never enforce business rules -- that is the
service's job.

Append-only by contract (DATABASE_DESIGN.md): this module exposes only INSERT
and SELECT. No update/delete function exists, so the append-only guarantee is
structural, not merely a convention someone must remember.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.schemas import AuditLogQuery
from app.database.models.core_models import AuditLog


async def insert(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID | None,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    event_metadata: dict | None,
) -> AuditLog:
    """Append one audit row and return it with server-side defaults populated.

    `organization_id` is nullable here purely to mirror the ORM column
    (see this module's docstring); the service layer always passes
    `actor.organization_id`, which is never None for any `Identity` that
    exists today. `actor` is the caller's `Identity.audit_tag` (resolved by
    the service, not here). The row is flushed and refreshed so DB-generated
    columns (`id` via gen_random_uuid(), `occurred_at` via now()) are
    available to the caller; the surrounding transaction is committed by the
    service / session scope, not by this function.
    """
    row = AuditLog(
        organization_id=organization_id,
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        event_metadata=event_metadata,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_entries(
    session: AsyncSession,
    organization_id: uuid.UUID,
    query: AuditLogQuery,
) -> Sequence[AuditLog]:
    """Return audit rows belonging to `organization_id` matching `query`,
    newest first.

    `organization_id` is a mandatory, separate filter -- not part of `query`
    -- matching `core.incidents.repository.list_incidents`'s shape; every
    other filter in `query` is optional and AND-combined on top of it.
    Results are ordered by `occurred_at` descending (matching the
    `ix_audit_logs_org_occurred_at_desc` index, which leads with
    `organization_id` for exactly this query shape) and paginated via
    limit/offset.
    """
    stmt = select(AuditLog).where(AuditLog.organization_id == organization_id)

    if query.resource_type is not None:
        stmt = stmt.where(AuditLog.resource_type == query.resource_type)
    if query.resource_id is not None:
        stmt = stmt.where(AuditLog.resource_id == query.resource_id)
    if query.actor is not None:
        stmt = stmt.where(AuditLog.actor == query.actor)

    stmt = stmt.order_by(AuditLog.occurred_at.desc()).limit(query.limit).offset(query.offset)

    result = await session.execute(stmt)
    return result.scalars().all()