"""This module manages incidents and their postmortems, including creating, updating,
viewing timelines, and approving resolutions. It ensures users can access or modify only 
data belonging to their own organization, checks permissions before changes, and records 
audit events. If no project is specified when creating an incident, it gets the 
organization’s default project. Postmortems become approved when approved; publishing 
is left for a future workflow. Separate permissions are used for editing postmortems 
and approving them to keep access secure.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.incidents import repository
from app.core.incidents.reads import (
    list_incidents_for_ingestion as list_incidents_for_ingestion,
    list_postmortems_for_ingestion as list_postmortems_for_ingestion,
)
from app.core.incidents.schemas import (
    ActionItem,
    Incident,
    IncidentCreate,
    IncidentFilter,
    IncidentUpdate,
    Postmortem,
    PostmortemUpdate,
    TimelineEntry,
    TimelineNoteCreate,
)
from app.core.tenancy import service as tenancy_service
from app.core.users.service import require_permission, require_project_permission
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity, InvestigationResult

logger = get_logger(__name__)

_INCIDENT_READ_PERMISSION = "incident:read"
_INCIDENT_WRITE_PERMISSION = "incident:write"
_POSTMORTEM_WRITE_PERMISSION = "postmortem:write"
_POSTMORTEM_APPROVE_PERMISSION = "postmortem:approve"


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    """Tenant-isolation guard -- identical in spirit to
    `core.tenancy.service._ensure_same_organization`; not extracted into a
    shared helper yet (this is only the second occurrence outside that
    module; a third would make the case for promoting it to shared/).
    """
    if actor.organization_id != organization_id:
        logger.warning(
            "incidents_cross_organization_denied",
            actor=actor.audit_tag,
            actor_organization_id=str(actor.organization_id),
            requested_organization_id=str(organization_id),
        )
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="incident.cross_organization_denied",
            detail={"organization_id": str(organization_id)},
        )


async def _get_owned_incident(
    session: AsyncSession, organization_id: uuid.UUID, incident_id: uuid.UUID
):
    """Fetch an incident ORM row, raising NotFoundError unless it both
    exists and belongs to `organization_id` -- a write-time tenant-isolation
    guard of the same shape as `core.tenancy.service.register_connector`'s
    `project_id` check: guessing another organization's incident id must
    never succeed, not even to discover that it exists.
    """
    row = await repository.get_incident_by_id(session, incident_id)
    if row is None or row.organization_id != organization_id:
        raise NotFoundError(
            "Incident not found.",
            error_code="incident.not_found",
            detail={"incident_id": str(incident_id)},
        )
    return row


async def _get_owned_postmortem(
    session: AsyncSession, organization_id: uuid.UUID, postmortem_id: uuid.UUID
):
    """Postmortem equivalent of `_get_owned_incident`."""
    row = await repository.get_postmortem_by_id(session, postmortem_id)
    if row is None or row.organization_id != organization_id:
        raise NotFoundError(
            "Postmortem not found.",
            error_code="postmortem.not_found",
            detail={"postmortem_id": str(postmortem_id)},
        )
    return row


# --- Incidents -----------------------------------------------------------------


async def create_incident(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: IncidentCreate,
) -> Incident:
    """Create a new incident, defaulting `project_id` to the organization's
    default project if `data.project_id` is omitted.

    Requires `actor.user_id` -- `incidents.reported_by` is a required
    reference to a `users` row, so only a `USER`-kind identity can report one.
    """
    _ensure_same_organization(actor, organization_id)

    if actor.user_id is None:
        raise ValidationError(
            "Only a user identity can report an incident.",
            error_code="incident.invalid_actor",
        )

    if data.project_id is not None:
        project_id = data.project_id
    else:
        default_project = await tenancy_service.get_default_project(
            session, actor, organization_id
        )
        project_id = default_project.id

    require_project_permission(actor, project_id, _INCIDENT_WRITE_PERMISSION)

    row = await repository.insert_incident(
        session,
        organization_id=organization_id,
        project_id=project_id,
        title=data.title,
        description=data.description,
        severity=data.severity,
        reported_by=actor.user_id,
    )
    await record_audit_event(
        session,
        actor,
        action="incident.create",
        resource_type="incident",
        resource_id=row.id,
        metadata={
            "organization_id": str(organization_id),
            "project_id": str(project_id),
            "severity": data.severity,
        },
    )
    return Incident.model_validate(row)


async def get_incident(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, incident_id: uuid.UUID
) -> Incident:
    """This function fetches an incident and raises `NotFoundError` if it doesn't exist 
    or belongs to another organization. It checks the user's `incident:read` permission 
    before allowing access, based on their project role or organization-level permissions.
    Checking whether the incident exists first ensures unauthorized users cannot discover
    whether another organization's incident exists.
    """
    _ensure_same_organization(actor, organization_id)
    row = await _get_owned_incident(session, organization_id, incident_id)
    require_project_permission(actor, row.project_id, _INCIDENT_READ_PERMISSION)
    return Incident.model_validate(row)


async def list_incidents(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, query: IncidentFilter
) -> list[Incident]:
    """Return incidents belonging to `organization_id`, filtered/paginated
    per `query`.

    Gated by an org-level `incident:read` check (no `project_id` --
    `IncidentFilter` has no project scoping, so this always spans every
    project in the organization; see `get_incident`'s docstring for the
    vulnerability this closes).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _INCIDENT_READ_PERMISSION)
    rows = await repository.list_incidents(session, organization_id, query)
    return [Incident.model_validate(row) for row in rows]


async def update_incident(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
    patch: IncidentUpdate,
) -> Incident:
    """Apply a partial update (API_DESIGN.md: `PATCH /incidents/{id}`).

    Not a strict state machine -- the docs don't specify legal incident
    status transitions the way they do for postmortems -- but one automatic
    bookkeeping fact is applied regardless of what the caller sends:
    transitioning *into* `resolved`/`closed` stamps `resolved_at` if it isn't
    already set, and transitioning back to `open`/`investigating` clears it,
    since `resolved_at` should always reflect the incident's current
    resolution state, not just whatever it happened to be the first time it
    was resolved.
    """
    _ensure_same_organization(actor, organization_id)
    existing = await _get_owned_incident(session, organization_id, incident_id)
    require_project_permission(actor, existing.project_id, _INCIDENT_WRITE_PERMISSION)

    fields = patch.model_dump(exclude_unset=True)
    if not fields:
        return Incident.model_validate(existing)

    new_status = fields.get("status")
    if new_status in ("resolved", "closed") and existing.resolved_at is None:
        fields["resolved_at"] = datetime.now(timezone.utc)
    elif new_status in ("open", "investigating") and existing.resolved_at is not None:
        # Only *reopening* an already-resolved incident clears the stamp.
        # Without the `is not None` guard this fired on every open/
        # investigating patch, writing `resolved_at = NULL -> NULL` and
        # listing a phantom `resolved_at` in the audit event's changed_fields.
        fields["resolved_at"] = None

    row = await repository.update_incident(session, incident_id, **fields)
    if row is None:
        raise RuntimeError("Incident disappeared mid-update.")  # unreachable: fetched above

    await record_audit_event(
        session,
        actor,
        action="incident.update",
        resource_type="incident",
        resource_id=incident_id,
        metadata={"organization_id": str(organization_id), "changed_fields": list(fields.keys())},
    )
    return Incident.model_validate(row)


# --- Timeline --------------------------------------------------------------------


async def add_timeline_note(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
    data: TimelineNoteCreate,
) -> TimelineEntry:
    """Add a manual, human-authored timeline note (API_DESIGN.md).

    `event_type` is always `"note"` here -- agent-authored entries
    (`event_type="investigation"`) are a separate write path,
    `record_investigation_result`, not something this human-facing function
    produces.
    """
    _ensure_same_organization(actor, organization_id)
    existing = await _get_owned_incident(session, organization_id, incident_id)
    require_project_permission(actor, existing.project_id, _INCIDENT_WRITE_PERMISSION)

    row = await repository.insert_timeline_entry(
        session,
        organization_id=organization_id,
        incident_id=incident_id,
        event_type="note",
        event_data={"note": data.note},
        actor=actor.audit_tag,
    )
    await record_audit_event(
        session,
        actor,
        action="incident.timeline_note.add",
        resource_type="incident_timeline",
        resource_id=row.id,
        metadata={"incident_id": str(incident_id)},
    )
    return TimelineEntry.model_validate(row)


async def get_timeline(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, incident_id: uuid.UUID
) -> list[TimelineEntry]:
    """Return an incident's timeline, in chronological order.

    Gated by the same `incident:read` check as `get_incident` -- a
    timeline entry can include investigation evidence and root-cause detail,
    so it must not be readable by anyone who couldn't read the incident
    itself.
    """
    _ensure_same_organization(actor, organization_id)
    incident = await _get_owned_incident(session, organization_id, incident_id)
    require_project_permission(actor, incident.project_id, _INCIDENT_READ_PERMISSION)

    rows = await repository.list_timeline_entries(session, incident_id)
    return [TimelineEntry.model_validate(row) for row in rows]


async def record_investigation_result(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
    investigation: InvestigationResult,
) -> TimelineEntry:
    """This function saves the Investigation Agent’s findings to an incident’s timeline 
    so they can be used later by the Postmortem Agent to identify the root cause. It 
    stores evidence, hypotheses, suggested teams, next steps, and review/critique results 
    as JSON, making the timeline a permanent record without needing to fetch data from 
    another table. It doesn’t check permissions again because the agent action was 
    already authorized, and it stores only structured critique tags, not the model’s raw 
    reasoning.
    """
    _ensure_same_organization(actor, organization_id)
    await _get_owned_incident(session, organization_id, incident_id)

    event_data = {
        "evidence": [item.model_dump(mode="json") for item in investigation.evidence],
        "hypotheses": [h.model_dump(mode="json") for h in investigation.hypotheses],
        "suggested_owner_team": investigation.suggested_owner_team,
        "suggested_next_steps": investigation.suggested_next_steps,
        "review_status": investigation.review_status,
        "critique_verdict": investigation.critique_verdict,
        "revision_count": investigation.revision_count,
        "critique_issues": investigation.critique_issues,
    }
    row = await repository.insert_timeline_entry(
        session,
        organization_id=organization_id,
        incident_id=incident_id,
        event_type="investigation",
        event_data=event_data,
        actor=actor.audit_tag,
    )
    return TimelineEntry.model_validate(row)


# --- Postmortems -----------------------------------------------------------------


async def create_postmortem(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
    *,
    root_cause: str | None,
    action_items: list[ActionItem] | None = None,
) -> Postmortem:
    """Create a postmortem draft for `incident_id`.

    Not gated by `require_permission`: this is the persistence half of
    "trigger draft generation" core/incidents
    calls `agents.generate_postmortem`, which returns computed content only;
    core/incidents then persists it, since agents never writes to this table
    directly. `generated_by` is always `actor.audit_tag`, so whichever
    identity triggered this (a human, or eventually `agent:postmortem_agent`)
    is the exact same string recorded as having generated it.

    Raises ConflictError if a postmortem already exists for this incident --
    one postmortem per incident (DATABASE_DESIGN.md).
    """
    _ensure_same_organization(actor, organization_id)
    await _get_owned_incident(session, organization_id, incident_id)

    existing_postmortem = await repository.get_postmortem_by_incident_id(session, incident_id)
    if existing_postmortem is not None:
        raise ConflictError(
            "A postmortem already exists for this incident.",
            error_code="postmortem.already_exists",
            detail={"incident_id": str(incident_id)},
        )

    row = await repository.insert_postmortem(
        session,
        organization_id=organization_id,
        incident_id=incident_id,
        root_cause=root_cause,
        action_items=[item.model_dump() for item in (action_items or [])],
        generated_by=actor.audit_tag,
    )
    await record_audit_event(
        session,
        actor,
        action="postmortem.create",
        resource_type="postmortem",
        resource_id=row.id,
        metadata={"incident_id": str(incident_id)},
    )
    return Postmortem.model_validate(row)


async def trigger_postmortem_generation(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    incident_id: uuid.UUID,
) -> Postmortem:
    """triggers the Postmortem Agent to generate a draft for an incident and saves it to 
    the database. It only runs if the incident is resolved or closed, the user has 
    `postmortem:write` permission, and no postmortem already exists. It checks for an 
    existing postmortem before making costly AI calls. The draft is attributed to the 
    Postmortem Agent, while a separate audit event records which human requested it, 
    keeping responsibility clear.
    """
    _ensure_same_organization(actor, organization_id)
    incident = await _get_owned_incident(session, organization_id, incident_id)

    if incident.status not in ("resolved", "closed"):
        raise ConflictError(
            "Postmortem generation requires a resolved or closed incident.",
            error_code="postmortem.incident_not_resolved",
            detail={"incident_id": str(incident_id), "status": incident.status},
        )

    require_project_permission(actor, incident.project_id, _POSTMORTEM_WRITE_PERMISSION)

    existing_postmortem = await repository.get_postmortem_by_incident_id(session, incident_id)
    if existing_postmortem is not None:
        raise ConflictError(
            "A postmortem already exists for this incident.",
            error_code="postmortem.already_exists",
            detail={"incident_id": str(incident_id)},
        )

    # Deferred import: agents/ already depends on core/incidents at module
    # level (e.g. `agents.investigation.evidence`, `agents.service.
    # triage_incident`), so a module-level import of `agents.service` here
    # would be a circular import at load time -- the same reasoning
    # `agents.graph.build_graph` gives for its own deferred node imports.
    from app.agents.service import generate_postmortem

    root_cause, action_items = await generate_postmortem(session, incident_id, actor)

    agent_actor = Identity.for_agent("postmortem_agent", organization_id)
    postmortem = await create_postmortem(
        session,
        agent_actor,
        organization_id,
        incident_id,
        root_cause=root_cause,
        action_items=action_items,
    )

    await record_audit_event(
        session,
        actor,
        action="postmortem.generate_requested",
        resource_type="postmortem",
        resource_id=postmortem.id,
        metadata={"incident_id": str(incident_id)},
    )
    return postmortem


async def get_postmortem(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, postmortem_id: uuid.UUID
) -> Postmortem:
    """Fetch one postmortem (draft or published).

    Approved/published postmortems are readable by anyone in the
    organization; a still-draft/in-review one requires `postmortem:write`
    or `postmortem:approve` -- mirroring `core.knowledge.service.
    get_document`'s identical published-vs-proposed gate. Unlike `list_
    recent_postmortems`'s own "read-only, no gate" note just below (which
    only ever returns already-reviewed postmortems, so needs no gate), this
    function can return a draft -- and until this fix, it did so to any org
    member holding a raw postmortem id, with no permission check at all.
    """
    _ensure_same_organization(actor, organization_id)
    row = await _get_owned_postmortem(session, organization_id, postmortem_id)
    if row.status not in ("approved", "published"):
        incident = await repository.get_incident_by_id(session, row.incident_id)
        project_id = incident.project_id if incident is not None else None
        if not (
            actor.has_permission(_POSTMORTEM_WRITE_PERMISSION, project_id=project_id)
            or actor.has_permission(_POSTMORTEM_APPROVE_PERMISSION, project_id=project_id)
        ):
            raise PermissionDeniedError(
                "This postmortem has not been reviewed yet.",
                error_code="postmortem.not_reviewed",
                detail={"postmortem_id": str(postmortem_id)},
            )
    return Postmortem.model_validate(row)


async def get_postmortem_by_incident(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, incident_id: uuid.UUID
) -> Postmortem:
    """Look up the postmortem already attached to `incident_id`.

    Raises `NotFoundError` if none exists yet -- an ordinary 404 a caller is
    expected to catch to mean "no postmortem yet, offer to generate one,"
    not a genuine error state; matching every other "fetch by id" function
    in this module (`_get_owned_incident`, `_get_owned_postmortem`), none of
    which return `None` for a not-yet-existing resource, so this doesn't
    introduce a second style.

    Closes a real, previously-flagged gap: `trigger_postmortem_generation`'s
    409 "already exists" response never told the caller the existing
    postmortem's id (only the incident id, which the caller already had) --
    there was no REST way at all to go from "I have an incident id" to "is
    there already a postmortem, and if so what's its id" without this.
    Backs `GET /incidents/{incident_id}/postmortem`.

    Applies the exact same read gate `get_postmortem` does (approved/
    published open to any org member; draft/in-review requires
    `postmortem:write` or `postmortem:approve`) -- this is the same
    resource, just looked up by a different key, so it must not be a wider
    door into the same data.
    """
    _ensure_same_organization(actor, organization_id)
    await _get_owned_incident(session, organization_id, incident_id)

    row = await repository.get_postmortem_by_incident_id(session, incident_id)
    if row is None or row.organization_id != organization_id:
        raise NotFoundError(
            "No postmortem exists for this incident yet.",
            error_code="postmortem.not_found",
            detail={"incident_id": str(incident_id)},
        )

    if row.status not in ("approved", "published"):
        incident = await repository.get_incident_by_id(session, incident_id)
        project_id = incident.project_id if incident is not None else None
        if not (
            actor.has_permission(_POSTMORTEM_WRITE_PERMISSION, project_id=project_id)
            or actor.has_permission(_POSTMORTEM_APPROVE_PERMISSION, project_id=project_id)
        ):
            raise PermissionDeniedError(
                "This postmortem has not been reviewed yet.",
                error_code="postmortem.not_reviewed",
                detail={"postmortem_id": str(row.id)},
            )
    return Postmortem.model_validate(row)


async def list_recent_postmortems(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    *,
    limit: int = 5,
) -> list[Postmortem]:
    """Return the most recent reviewed postmortems for `organization_id`
    (`approved`/`published` only -- an unreviewed `draft` hasn't been vetted
    for correctness, so surfacing it as investigative "existing knowledge"
    for a *different* incident could propagate an unreviewed root cause).

    Backs the Investigation Agent's "existing postmortems" evidence source
    (`agents.investigation.evidence`) -- a read-only lookup, so no
    `require_permission` gate, matching `get_incident`/`list_incidents`.
    """
    _ensure_same_organization(actor, organization_id)
    rows = await repository.list_postmortems_by_organization(
        session, organization_id, statuses=("approved", "published"), limit=limit
    )
    return [Postmortem.model_validate(row) for row in rows]


# `list_postmortems_for_ingestion`/`list_incidents_for_ingestion` live in
# `core.incidents.reads` (imported and re-exported at the top of this file),
# not here, so `ingestion.connectors.runbooks`/`ingestion.connectors.
# incidents` can each depend on a module that never imports `agents` -- see
# that module's docstring for the full reasoning.


async def update_postmortem(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    postmortem_id: uuid.UUID,
    patch: PostmortemUpdate,
) -> Postmortem:
    """Human edits during review (API_DESIGN.md: `PATCH /postmortems/{id}`).

    Raises ConflictError unless the postmortem is currently
    `draft`/`in_review` -- an approved or published postmortem is a closed
    state machine, mirroring the discipline already used for invitations
    (`core.tenancy.service.revoke_invitation`).
    """
    _ensure_same_organization(actor, organization_id)
    existing = await _get_owned_postmortem(session, organization_id, postmortem_id)
    if existing.status not in ("draft", "in_review"):
        raise ConflictError(
            "Only a draft or in-review postmortem can be edited.",
            error_code="postmortem.not_editable",
            detail={"status": existing.status},
        )

    incident = await repository.get_incident_by_id(session, existing.incident_id)
    project_id = incident.project_id if incident is not None else None
    require_permission(actor, _POSTMORTEM_WRITE_PERMISSION, project_id=project_id)

    fields = patch.model_dump(exclude_unset=True)
    if not fields:
        return Postmortem.model_validate(existing)

    row = await repository.update_postmortem(session, postmortem_id, **fields)
    if row is None:
        raise RuntimeError("Postmortem disappeared mid-update.")  # unreachable: fetched above

    await record_audit_event(
        session,
        actor,
        action="postmortem.update",
        resource_type="postmortem",
        resource_id=postmortem_id,
        metadata={"organization_id": str(organization_id), "changed_fields": list(fields.keys())},
    )
    return Postmortem.model_validate(row)


async def approve_postmortem(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, postmortem_id: uuid.UUID
) -> Postmortem:
    """Approve a postmortem -- the mandatory human-review gate
    (API_DESIGN.md: `POST /postmortems/{id}/approve`; ARCHITECTURE.md section
    5: nothing an agent generates reaches "knowledge" without this).

    Requires `actor.user_id`: only a human can approve -- an agent approving
    its own draft would defeat the entire point of a human-review gate.
    Raises ConflictError unless the postmortem is currently
    `draft`/`in_review` (approving straight from `draft`, with no
    intermediate edit, is allowed -- a reviewer who has nothing to change
    shouldn't be forced through a no-op PATCH first).
    """
    _ensure_same_organization(actor, organization_id)

    if actor.user_id is None:
        raise ValidationError(
            "Only a user identity can approve a postmortem.",
            error_code="postmortem.invalid_actor",
        )

    existing = await _get_owned_postmortem(session, organization_id, postmortem_id)
    if existing.status not in ("draft", "in_review"):
        raise ConflictError(
            "Only a draft or in-review postmortem can be approved.",
            error_code="postmortem.not_reviewable",
            detail={"status": existing.status},
        )

    incident = await repository.get_incident_by_id(session, existing.incident_id)
    project_id = incident.project_id if incident is not None else None
    require_permission(actor, _POSTMORTEM_APPROVE_PERMISSION, project_id=project_id)

    row = await repository.update_postmortem(
        session, postmortem_id, status="approved", reviewed_by=actor.user_id
    )
    if row is None:
        raise RuntimeError("Postmortem disappeared mid-approval.")  # unreachable: fetched above

    await record_audit_event(
        session,
        actor,
        action="postmortem.approve",
        resource_type="postmortem",
        resource_id=postmortem_id,
        metadata={"organization_id": str(organization_id)},
    )
    return Postmortem.model_validate(row)
