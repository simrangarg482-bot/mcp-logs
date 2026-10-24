"""`input_summary` is deliberately JSONB, not the raw prompt/context: per
DATABASE_DESIGN.md, "not the full prompt -- a structured summary for
observability, to avoid storing sensitive full context indefinitely" -- this
table is an execution-outcome log, not a prompt cache.

`confidence_score` uses `Numeric(asdecimal=False)` so SQLAlchemy hands back a
plain Python `float` (matching every other confidence-adjacent value in this
codebase -- `Settings.confidence_threshold`, `ScoredChunk.score` -- rather
than a `decimal.Decimal` the rest of the app would need to convert at every
read site).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class AgentExecution(Base):
    """agent_executions stores one record for each agent run.
       It tracks:
       - Which agent ran and what triggered it.
       - Its confidence score and whether it succeeded.
       - A summary of its input.
       Why? The Knowledge Gap Agent analyzes repeated low-confidence runs on 
       similar topics to identify missing documentation.
       Difference: agent_executions tracks agent activity; audit_logs tracks 
       human-auditable actions — who did what and when.
    """

    __tablename__ = "agent_executions"
    __table_args__ = (
        Index("ix_agent_executions_agent_name_started_at", "agent_name", "started_at"),
        Index("ix_agent_executions_org_started_at", "organization_id", "started_at"),
        Index("ix_agent_executions_user_started_at", "user_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    #: The human user who triggered this execution over REST, if any --
    #: `NULL` for MCP/scheduled executions and for any execution that predates
    #: this column. Added so a per-user "question history" view (the
    #: frontend's `GET /ask/history`) can filter this same table rather than
    #: needing a second, duplicate log of what a human actually asked.
    #: `ondelete="SET NULL"`, not `RESTRICT` like `organization_id` above:
    #: deleting a user account must never be blocked by their own old
    #: execution history, the same reasoning `RefreshToken`'s FKs already use.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_source: Mapped[str] = mapped_column(Text, nullable=False)  # mcp/core_api/scheduled
    input_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(
        Numeric(asdecimal=False), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: (AI usage/cost telemetry). All four `NULL` for any
    #: execution that predates this column, and for any execution whose LLM
    #: call(s) never returned `usage_metadata` (e.g. a mocked/test LLM
    #: response) -- absence here means "not captured," never "zero tokens
    #: spent." `model_used` records `Settings.agent_llm_model` at the time
    #: of the call, not a per-node value -- `app.agents.llm.get_llm()` is a
    #: single global model setting today (see that module's own docstring),
    #: so every LLM call within one graph execution already uses the same
    #: model; this column exists so historical executions remain correctly
    #: attributed after a future model change, not to support multiple
    #: models within one execution (which `app.agents.telemetry.
    #: summarize_usage` handles defensively if it ever happens, but nothing
    #: in this codebase does that today).
    model_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)


class KnowledgeGapReport(Base):
    """knowledge_gap_reports stores each recommendation created by the Knowledge 
    Gap Agent, including the suggested topic, supporting execution IDs, and recommended 
    action.
    """

    __tablename__ = "knowledge_gap_reports"
    __table_args__ = (
        Index("ix_knowledge_gap_reports_org_status", "organization_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    suggested_topic: Mapped[str] = mapped_column(Text, nullable=False)
    topic_embedding: Mapped[list] = mapped_column(JSONB, nullable=False)
    supporting_execution_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    suggested_action: Mapped[str] = mapped_column(Text, nullable=False)  # new_runbook/update_existing
    related_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
