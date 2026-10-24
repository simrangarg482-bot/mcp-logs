"""This module defines the document proposal and review system for EKIP. Agents can use 
`propose_document` to suggest runbook or knowledge-document updates, which remain pending 
until a human reviews them. Humans use `publish_document` to approve and publish 
proposals or `reject_document` to reject them, while `list_proposed_documents` retrieves 
pending proposals. Proposing a document does not require special permission because it is
reversible and awaits review, but the review-related operations require `knowledge:review` 
permission. All operations must respect tenant isolation, ensuring users can access only 
documents belonging to their organization. This interface supports the MCP tool, the 
`document://` resource, REST endpoints, and the future Knowledge Gap Agent.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.knowledge import repository
from app.core.knowledge.schemas import Document, DocumentProposalCreate, DocumentUpdate
from app.core.tenancy import service as tenancy_service
from app.core.users.service import require_permission, require_project_permission
from app.database.models.ingestion_models import Document as DocumentRow
from app.retrieval import service as retrieval_service
from app.retrieval.schemas import UpsertChunk
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity

logger = get_logger(__name__)

_REVIEW_PERMISSION = "knowledge:review"
_CONTENT_METADATA_KEY = "content"
_SOURCE_INCIDENT_METADATA_KEY = "source_incident_id"


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    """Tenant-isolation guard -- identical in spirit to the copies in
    `core.tenancy.service`/`core.incidents.service`/`core.audit.service`
    (now a fourth occurrence; still not extracted into `shared/`, per those
    modules' own notes on when that becomes worth doing).
    """
    if actor.organization_id != organization_id:
        logger.warning(
            "knowledge_cross_organization_denied",
            actor=actor.audit_tag,
            actor_organization_id=str(actor.organization_id),
            requested_organization_id=str(organization_id),
        )
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="knowledge.cross_organization_denied",
            detail={"organization_id": str(organization_id)},
        )


async def _get_owned_document(
    session: AsyncSession, organization_id: uuid.UUID, document_id: uuid.UUID
) -> DocumentRow:
    """Fetch a document ORM row, raising `NotFoundError` unless it exists,
    belongs to `organization_id`, and is not soft-deleted -- `NotFoundError`'s
    own docstring already scopes it to "does not exist (or is soft-deleted /
    hidden)", so a rejected (soft-deleted) proposal reads as not-found here,
    not as a still-visible-but-rejected row.
    """
    row = await repository.get_document_by_id(session, document_id)
    if row is None or row.organization_id != organization_id or row.deleted_at is not None:
        raise NotFoundError(
            "Document not found.",
            error_code="document.not_found",
            detail={"document_id": str(document_id)},
        )
    return row


async def _to_schema(session: AsyncSession, row: DocumentRow) -> Document:
    content = await repository.get_metadata_value(session, row.id, _CONTENT_METADATA_KEY)
    source_incident_id_raw = await repository.get_metadata_value(
        session, row.id, _SOURCE_INCIDENT_METADATA_KEY
    )
    return Document(
        id=row.id,
        organization_id=row.organization_id,
        project_id=row.project_id,
        title=row.title,
        status=row.status,
        version=row.version,
        content=content,
        source=row.source,
        source_url=row.source_url,
        source_incident_id=uuid.UUID(source_incident_id_raw) if source_incident_id_raw else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_schema_from_metadata(row: DocumentRow, metadata: dict[str, str]) -> Document:
    """Convert a collection row using metadata loaded in one bulk query."""
    source_incident_id_raw = metadata.get(_SOURCE_INCIDENT_METADATA_KEY)
    return Document(
        id=row.id,
        organization_id=row.organization_id,
        project_id=row.project_id,
        title=row.title,
        status=row.status,
        version=row.version,
        content=metadata.get(_CONTENT_METADATA_KEY),
        source=row.source,
        source_url=row.source_url,
        source_incident_id=uuid.UUID(source_incident_id_raw) if source_incident_id_raw else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _to_schemas_bulk(
    session: AsyncSession, rows: list[DocumentRow] | tuple[DocumentRow, ...]
) -> list[Document]:
    """Convert a bounded document page without a two-query-per-row loop."""
    metadata_by_document = await repository.get_metadata_values(
        session,
        [row.id for row in rows],
        [_CONTENT_METADATA_KEY, _SOURCE_INCIDENT_METADATA_KEY],
    )
    return [
        _to_schema_from_metadata(row, metadata_by_document.get(row.id, {})) for row in rows
    ]


async def propose_document(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: DocumentProposalCreate,
) -> Document:
    """Create a new proposed document (`status="proposed"`), awaiting human
    review via `publish_document`/`reject_document`.
    """
    _ensure_same_organization(actor, organization_id)

    project_id = data.project_id
    if project_id is None:
        default_project = await tenancy_service.get_default_project(session, actor, organization_id)
        project_id = default_project.id

    content_hash = hashlib.sha256(data.content.encode("utf-8")).hexdigest()
    row = await repository.insert_document(
        session,
        organization_id=organization_id,
        project_id=project_id,
        external_id=str(uuid.uuid4()),
        content_hash=content_hash,
        title=data.title,
    )
    await repository.insert_metadata(
        session, document_id=row.id, key=_CONTENT_METADATA_KEY, value=data.content
    )
    if data.source_incident_id is not None:
        await repository.insert_metadata(
            session,
            document_id=row.id,
            key=_SOURCE_INCIDENT_METADATA_KEY,
            value=str(data.source_incident_id),
        )

    await record_audit_event(
        session,
        actor,
        action="document.propose",
        resource_type="document",
        resource_id=row.id,
        metadata={"title": data.title},
    )
    logger.info("document_proposed", document_id=str(row.id), organization_id=str(organization_id))
    return await _to_schema(session, row)


async def get_document(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    """Fetch one document. Published documents are readable by anyone in
    the organization; a still-proposed one requires `knowledge:review`
    (API_DESIGN.md section 3's `document://` resource docstring: "published
    documents only, unless the requesting identity has knowledge:review").

    The permission check is project-scoped (`row.project_id`): a caller
    holding `knowledge:review` only on this document's project, but not at
    the organization level, may still view it. `Identity.has_permission`'s
    own semantics mean this changes nothing for callers with no per-project
    override -- they still fall back to the org-level check exactly as
    before.
    """
    _ensure_same_organization(actor, organization_id)
    row = await _get_owned_document(session, organization_id, document_id)
    if row.status != "published" and not actor.has_permission(
        _REVIEW_PERMISSION, project_id=row.project_id
    ):
        raise PermissionDeniedError(
            "This document has not been published yet.",
            error_code="document.not_published",
            detail={"document_id": str(document_id)},
        )
    return await _to_schema(session, row)


async def list_proposed_documents(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[Document]:
    """List every proposed document awaiting review (API_DESIGN.md:
    `GET /knowledge/proposed`).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _REVIEW_PERMISSION)

    rows = await repository.list_proposed_documents(
        session, organization_id, limit=limit, offset=offset
    )
    return await _to_schemas_bulk(session, list(rows))


async def list_published_documents(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    *,
    source: str | None = None,
    updated_since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Document]:
    """List a page of published documents for browsing (`GET /knowledge`)
    -- "browse ingested GitHub/Slack data". No `knowledge:review` gate,
    unlike `list_proposed_documents`: matches `get_document`'s existing rule
    that a published document is readable by anyone in the organization, so
    this is a plain org-scoped read, not a review-queue action.

    `limit`/`offset`: threaded straight through to
    `repository.list_published_documents` -- see that function's own
    docstring for why this became necessary.
    """
    _ensure_same_organization(actor, organization_id)

    rows = await repository.list_published_documents(
        session,
        organization_id,
        source=source,
        updated_since=updated_since,
        limit=limit,
        offset=offset,
    )
    return await _to_schemas_bulk(session, list(rows))


async def publish_document(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    """Publish a proposed document -- the human-review gate (API_DESIGN.md:
    `POST /knowledge/{document_id}/publish`) -- and embed it into the
    retrieval index in the same transaction ("triggering embedding into the
    retrieval index", per that endpoint's own documented behavior).

    Embeds the whole `content` as a single `UpsertChunk` (`collection=
    "documentation"`): unlike ingestion's connector pipeline, a manually
    proposed runbook has no raw-content-cleaning/multi-chunk pipeline to run
    through (`app.ingestion.processors` is off-limits to `core/` regardless
    -- see `repository.py`'s module docstring), and a review-quality runbook
    is typically short enough that one chunk is an honest, reasonable
    simplification rather than a silently cut corner. A future improvement
    could reuse a shared chunking utility if one is ever extracted out of
    `app.ingestion.processors` into somewhere both modules may import.

    The `knowledge:review` check is project-scoped to `row.project_id`
    (`require_project_permission`) -- fetching the row before checking
    (rather than the reverse, as this function did before project-scoped
    enforcement existed) is required to know which project to check against;
    an unauthorized caller now learns a nonexistent/foreign document 404s the
    same as always (`_get_owned_document` already tenant-scopes it), so this
    reordering does not newly leak document existence across organizations,
    only slightly changes 403-vs-404 precedence for a caller in the *same*
    organization who lacks the permission -- an accepted, minor behavior
    change in exchange for real per-project enforcement.
    """
    _ensure_same_organization(actor, organization_id)
    row = await _get_owned_document(session, organization_id, document_id)
    require_project_permission(actor, row.project_id, _REVIEW_PERMISSION)

    if row.status != "proposed":
        raise ConflictError(
            "Only a pending proposal can be published.",
            error_code="document.not_proposed",
            detail={"status": row.status},
        )

    content = await repository.get_metadata_value(session, row.id, _CONTENT_METADATA_KEY)
    updated = await repository.update_document_status(session, document_id, status="published")
    if updated is None:
        raise RuntimeError("Document disappeared mid-update.")  # unreachable: fetched above

    if content:
        chunk = UpsertChunk(
            document_id=row.id,
            organization_id=organization_id,
            project_id=row.project_id,
            collection="documentation",
            chunk_index=0,
            content=content,
            source_offset_start=0,
            source_offset_end=len(content),
        )
        await retrieval_service.upsert(session, [chunk])

    await record_audit_event(
        session,
        actor,
        action="document.publish",
        resource_type="document",
        resource_id=document_id,
        metadata={},
    )
    logger.info("document_published", document_id=str(document_id))
    return await _to_schema(session, updated)


async def update_document(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    data: DocumentUpdate,
) -> Document:
    """Edit a still-proposed document's `title`/`content` during human
    review (`PATCH /knowledge/{document_id}`) -- a new capability alongside
    publish/reject, gated by the same `knowledge:review` permission (same
    tier as approving/rejecting a proposal outright, per this module's own
    permission-design docstring).

    Only a `"proposed"` document may be edited here -- once published,
    content is considered final and already embedded into the retrieval
    index (`publish_document`); editing it through this function afterward
    would silently desynchronize the two without re-triggering that
    embedding step. A published document's content is not editable through
    this endpoint at all (raises ConflictError), matching `publish_document`/
    `reject_document`'s identical `status != "proposed"` guard.

    `exclude_unset`: omitting a field leaves it untouched. Editing `content`
    updates the same `document_metadata` `"content"` key `propose_document`
    wrote (via `repository.upsert_metadata`, which replaces the existing row
    in place rather than duplicating it -- see that function's own
    docstring). Editing `title` (or `content`, when `title` is untouched)
    bumps `Document.version`, matching the version-bump-on-content-change
    convention `Document`'s own docstring already establishes for ingestion
    re-syncs.
    """
    _ensure_same_organization(actor, organization_id)
    row = await _get_owned_document(session, organization_id, document_id)
    require_project_permission(actor, row.project_id, _REVIEW_PERMISSION)

    if row.status != "proposed":
        raise ConflictError(
            "Only a pending proposal can be edited.",
            error_code="document.not_proposed",
            detail={"status": row.status},
        )

    fields = data.model_dump(exclude_unset=True)
    if not fields:
        return await _to_schema(session, row)

    if "title" in fields:
        updated = await repository.update_document_title(session, document_id, title=fields["title"])
    else:
        updated = await repository.bump_document_version(session, document_id)
    if updated is None:
        raise RuntimeError("Document disappeared mid-update.")  # unreachable: fetched above

    if "content" in fields:
        await repository.upsert_metadata(
            session, document_id=document_id, key=_CONTENT_METADATA_KEY, value=fields["content"]
        )

    await record_audit_event(
        session,
        actor,
        action="document.update",
        resource_type="document",
        resource_id=document_id,
        metadata={"organization_id": str(organization_id), "changed_fields": list(fields.keys())},
    )
    logger.info("document_updated", document_id=str(document_id))
    return await _to_schema(session, updated)


async def reject_document(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    """Discard a proposal (API_DESIGN.md: `POST /knowledge/{document_id}/reject`).

    `shared.schemas.DocumentStatus` has only `"proposed"`/`"published"` --
    no third "rejected" value -- so rejection is represented as a soft
    delete (`deleted_at` set, `status` left at `"proposed"`) rather than
    silently inventing a status value this project's own vocabulary doesn't
    have. `_get_owned_document` already treats a soft-deleted row as
    not-found, so a rejected proposal cannot resurface through
    `get_document`/`list_proposed_documents` afterward.

    Project-scoped `knowledge:review` check, same reasoning and same
    fetch-before-check reordering as `publish_document`.
    """
    _ensure_same_organization(actor, organization_id)
    row = await _get_owned_document(session, organization_id, document_id)
    require_project_permission(actor, row.project_id, _REVIEW_PERMISSION)

    if row.status != "proposed":
        raise ConflictError(
            "Only a pending proposal can be rejected.",
            error_code="document.not_proposed",
            detail={"status": row.status},
        )

    updated = await repository.soft_delete_document(
        session, document_id, deleted_at=datetime.now(UTC)
    )
    if updated is None:
        raise RuntimeError("Document disappeared mid-update.")  # unreachable: fetched above

    # Purge the derived chunks/embeddings, not just the source row. The
    # soft delete above only hides the `documents` row from this module's
    # own reads; the chunk rows in `<collection>_chunks` (which carry their
    # own copy of `content` plus its embedding) are a separate table nothing
    # was cleaning up. `retrieval.pgvector.store` now also excludes
    # soft-deleted documents at query time, so this is defense in depth
    # rather than the only barrier -- but a filter alone would leave the
    # rejected content sitting in storage indefinitely, which is not what
    # "rejected" should mean. Runs in the same session/transaction as the
    # soft delete, so the two either both land or both roll back.
    purged_chunks = await _purge_document_chunks(session, document_id)

    # Deferred import: `core.graph.service` itself imports `core.knowledge.
    # repository` for live entity resolution during traversal, so a
    # module-level import here would be circular -- the same reasoning
    # `core.incidents.service.trigger_postmortem_generation` gives for its
    # own deferred `agents.service` import. This document is the source
    # endpoint of any stored `document --documents--> incident` edge
    # (`core.graph.contract`); rejecting it must remove those edges, not
    # just leave them to be caught by the next discovery pass, so a rejected
    # runbook cannot still surface as "related" through the graph.
    from app.core.graph import service as graph_service

    removed_edges = await graph_service.remove_edges_for_entity(
        session, organization_id=organization_id, entity_type="document", entity_id=document_id
    )

    # Same reasoning, one module further: `core.proactive.service` may hold
    # a finding whose evidence names this document (the `incident_
    # multi_document` detector's `supporting_document` role). A rejected
    # document must not keep inflating a finding's support count, so its
    # evidence row is removed and the finding's support recomputed/
    # deactivated here rather than left for the next scheduled detection
    # run alone -- the same "physical cleanup where a real lifecycle hook
    # exists" rule Priority 5's graph edge removal above already follows.
    # Deferred import for the identical circular-import reason as
    # `core.graph.service` above.
    from app.core.proactive import service as proactive_service

    findings_updated = await proactive_service.handle_evidence_entity_removed(
        session, organization_id=organization_id, entity_type="document", entity_id=document_id
    )

    await record_audit_event(
        session,
        actor,
        action="document.reject",
        resource_type="document",
        resource_id=document_id,
        metadata={
            "purged_chunk_collections": purged_chunks,
            "graph_edges_removed": removed_edges,
            "proactive_findings_updated": findings_updated,
        },
    )
    logger.info(
        "document_rejected",
        document_id=str(document_id),
        purged_chunks=purged_chunks,
        graph_edges_removed=removed_edges,
        proactive_findings_updated=findings_updated,
    )
    return await _to_schema(session, updated)


async def _purge_document_chunks(session: AsyncSession, document_id: uuid.UUID) -> list[str]:
    """Delete every chunk row derived from `document_id`, across all three
    collections, returning the collection names purged.

    Sweeps all collections rather than only `"documentation"` (the one
    `publish_document` writes): the same `documents` table is also populated
    by `app.ingestion`, whose pipeline classifies content into `"code"` and
    `"conversations"` too, so a document reaching this function could have
    chunks in any of them. `retrieval.service.delete` is a no-op for a
    collection with no matching rows, which is what makes sweeping all
    three safe and idempotent rather than wasteful.
    """
    purged: list[str] = []
    for collection in ("documentation", "code", "conversations"):
        await retrieval_service.delete(session, collection, document_id)
        purged.append(collection)
    return purged
