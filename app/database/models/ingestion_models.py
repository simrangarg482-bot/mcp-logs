import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.session import Base


class IngestionJob(Base):
    """One run (full or incremental sync) of a single `connector_config`.

    See this module's docstring for why this references `connector_config_id`
    rather than carrying its own `source`/`source_config` columns.
    """

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        Index("ix_ingestion_jobs_org_status", "organization_id", "status"),
        Index(
            "ix_ingestion_jobs_connector_config_created_at_desc",
            "connector_config_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    connector_config_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("connector_configs.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="queued"
    )  # queued/running/succeeded/failed
    # Which pipeline stage failed, e.g. "fetch"/"normalize"/"chunk"/"embed" --
    # populated only when status == "failed", per PROJECT_PLAN.md section 4.5's
    # resume-from-stage design (a retry re-runs from here, not from scratch).
    failed_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    documents_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_embedded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Document(Base):
    """Unique constraint mirrors DATABASE_DESIGN.md's original idempotency key
    with `organization_id` added to the tuple
    `(organization_id, source, external_id, content_hash)`. A re-ingest with
    an unchanged hash conflicts harmlessly (upsert no-op); changed content
    gets a new row with `version` incremented, per DATABASE_DESIGN.md, rather
    than overwriting history.

    `acl_permission_code` is the document-level ACL gate from nullable, reusing the
    existing RBAC permission-code vocabulary rather than a separate grant
    table. `NULL` (the default) means no restriction beyond tenant/project
    scope. Nothing in ingestion populates this to a non-null value yet --
    see #007's flagged tradeoff -- the column exists so retrieval's hard
    filter has something real to enforce against.
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "source",
            "external_id",
            "content_hash",
            name="uq_documents_org_source_external_hash",
        ),
        Index("ix_documents_org_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)  # slack/github/jira/docs/...
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="proposed")  # proposed/published
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    acl_permission_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    metadata_entries: Mapped[list["DocumentMetadata"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentMetadata(Base):
    """EAV-style metadata for one document (author, team, repo, ...).

    Kept EAV rather than fixed columns because different sources produce
    genuinely different metadata shapes -- a Slack message has a channel and
    thread, a GitHub file has a repo and path -- and a fixed-column table
    would need a migration per new connector, cutting against connectors
    being pluggable (DATABASE_DESIGN.md's rationale, unchanged here). No
    `organization_id` on this table: it is meaningless without its parent
    `Document` row and is never queried except by `document_id`, so unlike
    `IncidentTimeline`/`Postmortem` there is no independent RLS-policy need
    for a direct organization column here.
    """

    __tablename__ = "document_metadata"
    __table_args__ = (
        Index("ix_document_metadata_document_id", "document_id"),
        Index("ix_document_metadata_key_value", "key", "value"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    document: Mapped["Document"] = relationship(back_populates="metadata_entries")
