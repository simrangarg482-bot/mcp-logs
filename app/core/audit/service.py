"""This is the main service layer for `core/audit`. It applies business rules, converts
 database records into Pydantic schemas, and calls `repository.py` for database
   operations. It receives an existing database session from the caller so the audit 
   record and the actual change are committed or rolled back together. It also 
   automatically gets `organization_id` from the actor's `Identity`, ensuring audit 
   records belong to the correct organization. Previously, `query_audit_log` had no 
   organization-level restriction, so this update closes that security gap before the 
   API or MCP layer starts using it.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import repository
from app.core.audit.schemas import AuditLogEntry, AuditLogQuery
from app.core.exceptions import PermissionDeniedError
from app.core.users.service import require_permission
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

# Phase 2C addition: `query_audit_log` previously had no permission gate at
# all beyond tenant match -- harmless while nothing called it, but exposing
# a full org-wide audit trail to any authenticated member the moment a REST
# route calls this would be a real, new authorization gap, not an existing
# one. Follows this codebase's `resource:verb` permission-code convention
# (matching `observability:read`, `knowledge:review`).
_AUDIT_READ_PERMISSION = "audit:read"


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    """Tenant-isolation guard -- identical in spirit to the copies in
    `core.tenancy.service` and `core.incidents.service`. This is now the
    third occurrence of this exact function; per the note left in
    `core.incidents.service`'s module docstring, a third occurrence is the
    point at which extracting it into a shared helper becomes worth doing.
    Not done in this change -- that would mean also editing
    `core.tenancy.service` and `core.incidents.service`, which is outside
    this task's scope (extending core/audit) -- but flagged here rather than
    silently duplicated a third time without comment.
    """
    if actor.organization_id != organization_id:
        logger.warning(
            "audit_cross_organization_denied",
            actor=actor.audit_tag,
            actor_organization_id=str(actor.organization_id),
            requested_organization_id=str(organization_id),
        )
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="audit.cross_organization_denied",
            detail={"organization_id": str(organization_id)},
        )


async def record_audit_event(
    session: AsyncSession,
    actor: Identity,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    metadata: dict | None = None,
) -> None:
    """Append one entry to the append-only audit trail.

    Called by every mutating core/ operation. The `actor` string persisted is 
    `Identity.audit_tag` -- the single source of
    the `user:<id>` / `agent:<name>` format -- so the recorded actor always
    matches the identity that performed the action. The row's
    `organization_id` is `actor.organization_id` -- every `Identity` in this
    codebase carries one, so this is unconditional, not an `if` check.

    Returns None: the audit trail is a side effect of a domain operation, not
    a resource the caller reads back inline. Use `query_audit_log` to read.
    """
    await repository.insert(
        session,
        organization_id=actor.organization_id,
        actor=actor.audit_tag,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        event_metadata=metadata,
    )
    # Structured breadcrumb; the durable record is the row itself.
    logger.info(
        "audit_event_recorded",
        actor=actor.audit_tag,
        organization_id=str(actor.organization_id),
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
    )


async def query_audit_log(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    query: AuditLogQuery,
) -> list[AuditLogEntry]:
    """Read `organization_id`'s audit trail, newest first, filtered/paginated
    per `query`.

    Requires `actor` and `organization_id` (new in this change -- see this
    module's docstring) so a cross-organization read is denied the same way
    every other org-scoped read in this codebase is denied, rather than being
    an oversight waiting for its first caller to exploit it.

    Also requires `audit:read`
    the audit trail can reveal every consequential action taken in an
    organization, which is admin-facing information, not something every
    org member should be able to read by default.

    Maps ORM rows into `AuditLogEntry` so the transactional ORM objects never
    cross the module boundary.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _AUDIT_READ_PERMISSION)
    rows = await repository.list_entries(session, organization_id, query)
    return [AuditLogEntry.model_validate(row) for row in rows]