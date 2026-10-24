"""`agent_memories` stores persistent memories for AI agents, with permissions 
controlling who can retrieve each memory.

- Embedding on the same row: The memory text and its vector embedding are stored together
. Deleting a memory therefore cannot leave behind a separate, searchable embedding.
- Separate from chunk tables: Memories can be private to a user, unlike the existing 
document chunks, which are organization-wide knowledge.
- Tenant isolation: `organization_id` lets PostgreSQL RLS ensure one organization's 
memories aren't accessed by another.
- Foreign key behavior:
  - `organization_id → RESTRICT`: prevents deleting an organization while its memories 
  remain.
  - `owner_user_id → CASCADE`: if the user is truly deleted, their memories are deleted 
  too.
  - `project_id → SET NULL`: if a project is deleted, the memory becomes unretrievable 
  at project scope.
  - `supersedes_memory_id → SET NULL`: deleting an older memory doesn't require deleting 
  the newer one.
In short: This table gives agents durable, searchable memory without allowing private 
memories to leak or deleted memories to remain retrievable.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base

#: Must match `app.retrieval.embedding.EMBEDDING_DIMENSION` and
#: `retrieval_models._EMBEDDING_DIMENSION` -- duplicated rather than
#: imported, the same "cross-module constant, documented not shared"
#: convention those two already use for this exact value.
_EMBEDDING_DIMENSION = 384


class AgentMemory(Base):
    """One curated, reusable piece of remembered information.

    Deliberately NOT a message log. `agent_executions` already records what
    was asked and how the agent performed (and backs `GET /ask/history`);
    this table holds the small number of durable statements worth recalling
    later, each explicitly created rather than harvested from every turn.
    See `docs/AGENT_MEMORY.md` for the memory-vs-history distinction.
    """

    __tablename__ = "agent_memories"
    __table_args__ = (
        # The retrieval query filters by (organization_id, status) and then
        # by scope/owner before ordering by vector distance -- this index
        # serves that leading filter. No ANN index (HNSW/IVFFlat) on
        # `embedding`: per-organization memory counts are small by design
        # (memory is curated, not harvested), so an exact nearest-neighbour
        # scan over an already-tenant-filtered handful of rows is both
        # faster and exactly accurate. Revisit only with real volume
        # evidence -- adding an ANN index here would also make recall
        # approximate, which is a poor trade for a permission-sensitive set.
        Index("ix_agent_memories_org_status", "organization_id", "status"),
        Index("ix_agent_memories_owner_user_id", "owner_user_id"),
        Index("ix_agent_memories_org_project", "organization_id", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    #: `"user"` | `"project"`. Set to `"user"` together with a non-null
    #: `owner_user_id`, or `"project"` together with a non-null `project_id`
    #: -- enforced in `core.memory.schemas`, not by a database CHECK, matching
    #: this schema's existing convention of keeping status/scope vocabularies
    #: in the Pydantic layer (`documents.status`, `incidents.status`, ...).
    #: `"organization"` is deliberately absent -- see `docs/AGENT_MEMORY.md`.
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    #: The owning person, for `scope="user"`. NULL for every other scope.
    #: A user-scoped memory is retrievable ONLY by this user.
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    #: The owning project, for `scope="project"`. NULL for every other scope.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    #: Coarse category, e.g. `"preference"` / `"fact"` /
    #: `"investigation_conclusion"`. Vocabulary lives in
    #: `core.memory.schemas.MemoryType`.
    memory_type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: NOT NULL on purpose. A memory with no embedding would be silently
    #: unretrievable -- present in listings, invisible to every recall query.
    #: Requiring it means embedding failure surfaces at creation time as a
    #: loud error instead of as a memory that mysteriously never surfaces.
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIMENSION), nullable=False)
    #: Provenance. `source_type` is `"explicit"` when a human asked for this
    #: to be remembered, or names the artifact it was derived from
    #: (`"investigation"`, `"incident"`). NULL/absent rather than invented
    #: when there is genuinely no upstream source.
    source_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: `Identity.audit_tag` of whoever created it (`"user:<uuid>"` /
    #: `"agent:<name>"`) -- the same tagged-actor convention
    #: `incident_timeline.actor` and `audit_logs.actor` use, and deliberately
    #: not an FK, for the same reason: an agent has no `users` row.
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    #: `"active"` | `"superseded"` | `"deleted"`. Only `"active"` rows are
    #: ever returned by recall. `"deleted"` is a tombstone kept just long
    #: enough to make repeated deletion idempotent and observable -- the row's
    #: `content` and `embedding` are cleared at that point, so nothing
    #: recallable survives it (see `core.memory.repository.soft_delete`).
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    #: Set on the OLD memory when a new one replaces it, pointing forward is
    #: deliberately not modelled: the new row points BACK at what it
    #: replaced, so the chain is discoverable from the current record without
    #: a second write to the old one beyond its status change.
    supersedes_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_memories.id", ondelete="SET NULL"), nullable=True
    )
    memory_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    #: Updated opportunistically when a memory is actually injected into an
    #: agent context -- intended to support future relevance/pruning work.
    #: Nothing reads it yet; it is recorded rather than guessed at later,
    #: since the information is unrecoverable after the fact.
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
