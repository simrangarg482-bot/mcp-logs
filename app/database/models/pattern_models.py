"""These two tables help EKIP detect patterns in incidents and show the real records 
that support them.
- proactive_findings stores a detected pattern, for example: “3 high-severity incidents 
occurred in this project in the last 14 days.”
- proactive_finding_evidence stores references to the incidents or documents supporting
 that finding, not copies of their content.
Why separate from knowledge_graph_edges? A graph edge represents a relationship between
 entities. A proactive finding is an interpretation of multiple records, so it has its own tables.
Why no foreign key on entity_id? It can refer to different entity types, such as 
incidents or documents. EKIP checks that each referenced record still exists and that 
the user has permission to access it before showing it.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.session import Base


class ProactiveFinding(Base):
    """One derived, evidence-backed pattern."""

    __tablename__ = "proactive_findings"
    __table_args__ = (
        # The stable logical identity of a finding: the same underlying
        # pattern, in the same canonical scope, is always the same row.
        # Repeated detection therefore converges rather than accumulating
        # near-duplicates -- the same reasoning
        # `uq_knowledge_graph_edges_logical_identity` documents for edges.
        UniqueConstraint(
            "organization_id", "fingerprint", name="uq_proactive_findings_fingerprint"
        ),
        Index("ix_proactive_findings_org_status", "organization_id", "status"),
        Index("ix_proactive_findings_org_type", "organization_id", "finding_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    # A narrowing scope, not the authorization boundary by itself -- the
    # same role `knowledge_graph_edges.project_id` plays. Every finding type
    # implemented so far always sets this (both are single-project-scoped
    # patterns); nullable because a future org-wide pattern type is a
    # legitimate shape this schema should not need to change to support.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    finding_type: Mapped[str] = mapped_column(Text, nullable=False)
    # "active" | "inactive" -- see `core.proactive.contract.FindingStatus`.
    # Deliberately only two values: nothing in this pass represents a human
    # "dismiss" action, so a third "resolved" state would be unearned
    # vocabulary (PROJECT_STATUS.md's own "do not create unnecessary state
    # machines" rule, restated in this priority's own spec section 17).
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    # Deterministic, generated from structured fields -- never an LLM call,
    # never raw source content (a document body, an incident description).
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # Stable canonical grouping key -- see `core.proactive.contract` for how
    # each finding type constructs it. Never a timestamp, a random id, or
    # anything else that would defeat convergence on repeated detection.
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    # Recomputed on every detection run against CURRENT source state --
    # never merely incremented. A caller-facing read may recompute this
    # again, narrower, against only evidence that caller can see (see
    # `core.proactive.service`'s "mixed visibility" handling).
    support_count: Mapped[int] = mapped_column(nullable=False)
    # Tagged actor string, e.g. "agent:pattern_detection_agent" -- same
    # convention as `incident_timeline.actor`/`knowledge_graph_edges.
    # created_by`; identifies which detector produced/last touched this row.
    detector_name: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    evidence: Mapped[list["ProactiveFindingEvidence"]] = relationship(
        back_populates="finding", cascade="all, delete-orphan"
    )


class ProactiveFindingEvidence(Base):
    """One reference from a finding to the real entity that supports it.

    No `organization_id` column of its own -- meaningless without its
    parent finding, and never queried except by `finding_id`. Same shape
    (and same RLS treatment: scoped via a join, not a direct column) as
    `document_metadata`. Deleting a finding cascades to its evidence
    (`ondelete="CASCADE"` below): unlike a graph edge, an evidence row has
    no independent meaning once its finding is gone.
    """

    __tablename__ = "proactive_finding_evidence"
    __table_args__ = (
        # Repeated detection must not accumulate duplicate evidence rows for
        # the same (finding, entity, role) -- this is the DB-level half of
        # this priority's idempotency requirement; `core.proactive.
        # repository.replace_evidence` is the application-level half.
        UniqueConstraint(
            "finding_id",
            "entity_type",
            "entity_id",
            "role",
            name="uq_proactive_finding_evidence_identity",
        ),
        Index("ix_proactive_finding_evidence_finding_id", "finding_id"),
        Index("ix_proactive_finding_evidence_entity", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("proactive_findings.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # e.g. "supporting_incident", "anchor_incident", "supporting_document".
    # Convention (see `core.proactive.service`): a role prefixed
    # "supporting_" counts toward `support_count`'s recomputation; any other
    # role (e.g. "anchor_...") identifies the entity the finding is about
    # without itself being one of the repeated occurrences being counted.
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    finding: Mapped[ProactiveFinding] = relationship(back_populates="evidence")
