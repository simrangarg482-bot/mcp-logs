"""SQLAlchemy model for `knowledge_graph_edges` -- the derived relationship
layer.

Owned by: database/ (definition) + core/graph (write access).

WHAT THIS TABLE IS, AND EMPHATICALLY IS NOT
    It stores relationships between existing entities, never entity
    content. There is no title, description, document text or postmortem body
    here -- only `(entity_type, entity_id)` references whose source of truth
    remains `incidents`/`postmortems`/`documents`/`projects`. That is what
    makes the graph rebuildable and what stops it becoming a second,
    unauthorized copy of organizational knowledge.

ONLY NON-FOREIGN-KEY RELATIONSHIPS ARE STORED HERE
    This is the load-bearing design decision of Priority 5. Relationships
    that Postgres already enforces with a foreign key --
    `postmortems.incident_id`, `incidents.project_id`,
    `incident_timeline.incident_id`, `documents.project_id` -- are ALREADY a
    perfectly maintained graph. Copying them into this table could only ever
    add staleness and a leak path (exactly the Priority 3 failure mode:
    derived rows outliving their source). So `core.graph.service` resolves
    those relationships LIVE from the source tables at traversal time, with
    `provenance="foreign_key"`, and never writes them here.

    What lands in this table is only what has no FK to ride on:

      document --documents-->  incident   (from the `source_incident_id`
                                           entry in `document_metadata`, a
                                           real relationship the MCP
                                           `propose_runbook_update` tool
                                           already writes and which nothing
                                           could query in reverse before)
      incident --related_to--> incident   (symmetric; explicit/manual, since
                                           nothing in the schema records it)

    See `app.core.graph.contract` for the validated vocabulary.

NO `confidence` COLUMN
    Deliberately absent. Both implemented provenance kinds are certain: a
    deterministic extraction read a real stored id, and a manual edge was
    asserted by an authorized human. A confidence score would be decoration
    -- a number nothing computes and nothing could act on. If probabilistic
    edge discovery is ever added, that is when a meaningful confidence column
    should arrive, together with whatever computes it.

TENANT ISOLATION
    Carries `organization_id` directly, so migration `a7b8c9d0e1f2` can put
    it in the same `tenant_isolation` RLS policy set as every other
    direct-`organization_id` table (`c7d4e8f19a2b`). `project_id` is a
    denormalized hint for query narrowing only -- it is never the
    authorization boundary. Authorization is decided by resolving each
    endpoint entity against its own live row and permission rules (see
    `core.graph.service`), because an edge's own columns cannot know whether
    the caller may see the entities it points at.

DELIBERATELY NO FOREIGN KEYS ON THE ENDPOINTS
    `source_entity_id`/`target_entity_id` are plain UUIDs, not foreign keys,
    because a single column cannot reference four different tables. This is
    the same polymorphic-reference tradeoff `audit_logs.resource_id` already
    makes in this schema (also a plain UUID with a `resource_type`
    discriminator). The consequence is faced head-on rather than ignored: an
    edge can outlive the entity it points at, so traversal NEVER trusts an
    edge row on its own -- it resolves both endpoints against their live
    source tables and drops anything that no longer resolves or is no longer
    visible. Physical cleanup on deletion is the second barrier, not the
    only one.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class KnowledgeGraphEdge(Base):
    """One derived relationship between two existing EKIP entities."""

    __tablename__ = "knowledge_graph_edges"
    __table_args__ = (
        # The deduplication identity: one logical relationship per
        # (org, source, type, target). Deliberately EXCLUDES provenance --
        # if the same relationship is discovered twice by different means,
        # that is the same fact, not two facts, and an upsert should
        # converge rather than accumulate. Excluding `status` too, so a
        # re-discovered edge revives its existing row instead of colliding
        # with a soft-deleted twin.
        UniqueConstraint(
            "organization_id",
            "source_entity_type",
            "source_entity_id",
            "relationship_type",
            "target_entity_type",
            "target_entity_id",
            name="uq_knowledge_graph_edges_logical_identity",
        ),
        # Traversal always starts from a known entity within one org, and
        # always filters `status` -- this index serves exactly that lookup.
        Index(
            "ix_kg_edges_org_source",
            "organization_id",
            "source_entity_type",
            "source_entity_id",
            "status",
        ),
        # The reverse direction, for symmetric relationships and for
        # answering "what points AT this entity".
        Index(
            "ix_kg_edges_org_target",
            "organization_id",
            "target_entity_type",
            "target_entity_id",
            "status",
        ),
        # Used by lifecycle cleanup, which removes every edge touching a
        # deleted entity from either end.
        Index("ix_kg_edges_org_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    #: Denormalized narrowing hint, NOT an authorization boundary -- see the
    #: module docstring. Nullable because not every edge is project-scoped
    #: (and `SET NULL` rather than `CASCADE` so a deleted project cannot
    #: silently take unrelated edges with it; the edge then simply resolves
    #: to nothing, because its endpoints will not resolve either).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )

    #: Vocabulary enforced in `core.graph.contract`, not by a database CHECK
    #: -- matching this schema's existing convention for every other
    #: status/type column (`documents.status`, `incidents.status`, ...).
    source_entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    #: `"deterministic_extraction"` (read from a real stored field) or
    #: `"manual"` (asserted by an authorized human). Never invented: an edge
    #: with no honest provenance is not created.
    provenance_type: Mapped[str] = mapped_column(Text, nullable=False)
    #: What the extraction read, when that is itself an addressable row --
    #: e.g. the `document_metadata` row carrying `source_incident_id`. NULL
    #: for `"manual"`, where the provenance is the actor in `created_by`.
    provenance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    #: `"active"` | `"deleted"`. Only `"active"` edges are ever traversed.
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    #: `Identity.audit_tag` of whoever/whatever created the edge -- the same
    #: tagged-actor convention as `incident_timeline.actor`/`audit_logs.actor`,
    #: and for the same reason (a discovery pass runs as an agent identity
    #: with no `users` row).
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    #: Minimal, non-sensitive structured attributes only -- e.g. which
    #: metadata key an extraction read. Never document text, prompts,
    #: memory, or credentials (see `docs/KNOWLEDGE_GRAPH.md`).
    edge_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
