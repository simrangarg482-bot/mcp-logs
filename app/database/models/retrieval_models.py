"""Tenant security: Every chunk stores organization_id, project_id, and 
acl_permission_code directly, allowing retrieval to filter results by organization, 
project, and document permissions.
Incident search: Incidents enter the normal ingestion pipeline and become searchable 
chunks, rather than using a separate embedding system.
Hybrid search: content_tsv supports PostgreSQL full-text keyword search, while 
embeddings support semantic search. Results can be combined to improve retrieval.
Automatic indexing: content_tsv is a stored generated column that stays synchronized 
with the chunk's content and supports a GIN index for efficient text search.
"""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.database.session import Base

_EMBEDDING_DIMENSION = 384  # ENGINEERING_DECISIONS.md #006


def _base_chunk_table_args(table: str) -> tuple:
    """The index/constraint set every `<collection>_chunks` table shares,
    factored out of `_ChunkColumns.__table_args__` so `CodeChunk` can extend
    it with its own `repo_full_name` index without re-declaring the shared
    three.
    """
    return (
        UniqueConstraint("document_id", "chunk_index", name=f"uq_{table}_document_chunk_index"),
        Index(f"ix_{table}_org_project", "organization_id", "project_id"),
        Index(f"ix_{table}_content_tsv", "content_tsv", postgresql_using="gin"),
    )


class _ChunkColumns:
    """Mixin: the column set, indexes, and constraint every
    `<collection>_chunks` table shares. Not a `Base` subclass itself -- each
    concrete class below combines this mixin with `Base` and supplies its
    own `__tablename__`.
    """

    @declared_attr
    def __table_args__(cls):  # noqa: N805 -- SQLAlchemy declarative convention
        return _base_chunk_table_args(cls.__tablename__)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    # CASCADE (unlike organization_id/project_id above): a chunk is
    # meaningless once its parent document is gone, matching
    # DATABASE_DESIGN.md's original `<collection>_chunks.document_id ON
    # DELETE CASCADE`.
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBEDDING_DIMENSION), nullable=False)
    source_offset_start: Mapped[int] = mapped_column(Integer, nullable=False)
    source_offset_end: Mapped[int] = mapped_column(Integer, nullable=False)
    # Denormalized from the parent document at upsert time -- see this
    # module's docstring and ENGINEERING_DECISIONS.md #007.
    acl_permission_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Generated column backing lexical search -- see this module's docstring.
    content_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True), nullable=False
    )


class _RepoScopedChunkColumns(_ChunkColumns):
    """Mixin for the two collections GitHub-sourced content actually lands
    in (`"code"` for source files chunked by function/class boundary,
    `"documentation"` for issues/PRs/commit messages/READMEs -- ingestion's
    processing pipeline classifies most real GitHub connector output as
    `ContentType == "document"`, not `"code"`, since most repos have more
    prose than source). `ConversationChunk` (Slack/Teams) never carries a
    GitHub repo, so it does not get this column.

    Adds `repo_full_name`, denormalized from `document_metadata`'s
    GitHub-connector-only `repo` entry at upsert time -- see
    `UpsertChunk.repo_full_name`'s docstring. `NULL` for every
    non-GitHub-sourced chunk (nothing else sets it).
    """

    @declared_attr
    def __table_args__(cls):  # noqa: N805 -- SQLAlchemy declarative convention
        table = cls.__tablename__
        return (
            *_base_chunk_table_args(table),
            Index(f"ix_{table}_org_repo_full_name", "organization_id", "repo_full_name"),
        )

    repo_full_name: Mapped[str | None] = mapped_column(Text, nullable=True)


class DocumentationChunk(_RepoScopedChunkColumns, Base):
    """Chunks classified as `ContentType == "document"` by ingestion's
    processing pipeline (long-form docs, READMEs, chunked by heading
    section) -- PROJECT_PLAN.md section 8.2's "documentation" collection.
    """

    __tablename__ = "documentation_chunks"


class CodeChunk(_RepoScopedChunkColumns, Base):
    """Chunks classified as `ContentType == "code"` (chunked by
    function/class boundary) -- section 8.2's "code" collection.
    """

    __tablename__ = "code_chunks"


class ConversationChunk(_ChunkColumns, Base):
    """Chunks classified as `ContentType == "chat"` (one chunk per message,
    per `chunk_document`'s chat strategy) -- section 8.2's "conversations"
    collection (this table's name for "chat").
    """

    __tablename__ = "conversations_chunks"


class IncidentChunk(_ChunkColumns, Base):
    """Chunks classified as `ContentType == "incident"` (one chunk per
    incident, per `chunk_document`'s incident strategy -- same "already one
    coherent unit, don't fragment it" reasoning as the chat strategy) --
    section 8.2's "incidents" collection.
    """

    __tablename__ = "incidents_chunks"
