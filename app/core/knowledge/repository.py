"""This module handles saving and reviewing proposed knowledge documents using the 
existing `documents` and `document_metadata` tables. Unlike ingestion, which saves 
automatically synced content, `core/knowledge` saves manually proposed content 
(`source="manual"`), so the two workflows remain separate. Since the `documents` table 
has no content field, the full document text is temporarily stored in `document_metadata` 
under the key `"content"`. A dedicated `documents.content` column is the recommended 
long-term solution.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.ingestion_models import Document, DocumentMetadata


async def insert_document(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    external_id: str,
    content_hash: str,
    title: str,
) -> Document:
    """Create one proposed document row (`source="manual"`,
    `status="proposed"`) and return it with server defaults populated.
    """
    row = Document(
        organization_id=organization_id,
        project_id=project_id,
        source="manual",
        external_id=external_id,
        content_hash=content_hash,
        title=title,
        source_url=None,
        status="proposed",
        version=1,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def insert_metadata(
    session: AsyncSession, *, document_id: uuid.UUID, key: str, value: str
) -> None:
    """Attach one EAV metadata fact to `document_id`."""
    session.add(DocumentMetadata(document_id=document_id, key=key, value=value))
    await session.flush()


async def get_document_by_id(session: AsyncSession, document_id: uuid.UUID) -> Document | None:
    """Fetch a single document by primary key, or None if absent."""
    return await session.get(Document, document_id)


async def get_metadata_value(
    session: AsyncSession, document_id: uuid.UUID, key: str
) -> str | None:
    """Fetch one EAV metadata value for `document_id`, or None if `key`
    isn't set on it.
    """
    stmt = select(DocumentMetadata.value).where(
        DocumentMetadata.document_id == document_id, DocumentMetadata.key == key
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_metadata_values(
    session: AsyncSession,
    document_ids: Sequence[uuid.UUID],
    keys: Sequence[str],
) -> dict[uuid.UUID, dict[str, str]]:
    """Bulk-load selected metadata keys for a page of documents.

    The review and knowledge-list endpoints previously called
    ``get_metadata_value`` twice per document. A tenant with thousands of
    connector documents therefore produced thousands of sequential remote
    database round trips for one HTTP request. Keep the single-document
    helper for detail views, but use this bounded query for collection
    endpoints.
    """
    if not document_ids or not keys:
        return {}
    stmt = select(
        DocumentMetadata.document_id,
        DocumentMetadata.key,
        DocumentMetadata.value,
    ).where(
        DocumentMetadata.document_id.in_(document_ids),
        DocumentMetadata.key.in_(keys),
    )
    rows = (await session.execute(stmt)).all()
    values: dict[uuid.UUID, dict[str, str]] = {}
    for document_id, key, value in rows:
        values.setdefault(document_id, {})[key] = value
    return values


async def list_proposed_documents(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[Document]:
    """It gets all documents that are waiting for review in an organization, showing the 
    newest first. It only includes documents manually suggested by a person or AI, and 
    skips deleted documents. Automatically imported documents are excluded, even if a 
    future bug accidentally marks them as waiting for review.
    """
    stmt = (
        select(Document)
        .where(
            Document.organization_id == organization_id,
            Document.status == "proposed",
            Document.source == "manual",
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_published_documents(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    source: str | None = None,
    updated_since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[Document]:
    """Return a page of non-deleted, published documents for
    `organization_id`, newest-updated first (`GET /knowledge` -- "browse
    ingested GitHub/Slack data"), optionally narrowed to one connector
    `source` ("github"/"slack"/"manual"/...) and/or documents touched since
    `updated_since`.

    Published-only, unlike `list_proposed_documents`: mirrors `service.
    get_document`'s existing rule that a published document is readable by
    anyone in the organization, with no `knowledge:review` gate -- this is a
    browsing surface for already-approved content, not the review queue.

    `limit`/`offset` before Stage 1 made
    ingested documents reach `status="published"` at all, this query always
    returned an empty or near-empty set, so being unbounded was harmless.
    Stage 1 made it return every ingested document in the organization in
    one response with no way to page through it -- this closes that gap.
    """
    stmt = (
        select(Document)
        .where(
            Document.organization_id == organization_id,
            Document.status == "published",
            Document.deleted_at.is_(None),
        )
        .order_by(Document.updated_at.desc())
    )
    if source is not None:
        stmt = stmt.where(Document.source == source)
    if updated_since is not None:
        stmt = stmt.where(Document.updated_at >= updated_since)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return result.scalars().all()


async def update_document_status(
    session: AsyncSession, document_id: uuid.UUID, *, status: str
) -> Document | None:
    """Transition `document_id.status`, returning the updated row or None
    if it doesn't exist.
    """
    row = await session.get(Document, document_id)
    if row is None:
        return None
    row.status = status
    await session.flush()
    await session.refresh(row)
    return row


async def update_document_title(
    session: AsyncSession, document_id: uuid.UUID, *, title: str
) -> Document | None:
    """Rename a document and bump its `version`, returning the updated row
    or None if it doesn't exist.

    Used by `service.update_document` (human-review editing, PATCH). Bumping
    `version` here mirrors `Document`'s own docstring convention for content
    changes -- an
    edited title/content during review is the same kind of "this document's
    content just changed" fact, just produced by a human reviewer instead of
    a re-ingest.
    """
    row = await session.get(Document, document_id)
    if row is None:
        return None
    row.title = title
    row.version += 1
    await session.flush()
    await session.refresh(row)
    return row


async def bump_document_version(session: AsyncSession, document_id: uuid.UUID) -> Document | None:
    """Increment `version` with no other field change -- used by
    `service.update_document` when only `content` (not `title`) was edited,
    so the version-bump-on-edit convention still applies without requiring a
    title write alongside it.
    """
    row = await session.get(Document, document_id)
    if row is None:
        return None
    row.version += 1
    await session.flush()
    await session.refresh(row)
    return row


async def upsert_metadata(
    session: AsyncSession, *, document_id: uuid.UUID, key: str, value: str
) -> None:
    """Set (insert or overwrite in place) one EAV metadata fact for
    `document_id`.

    Unlike `insert_metadata` (append-only -- correct at proposal time, when
    the key is known not to exist yet for a brand-new document), this looks
    up any existing row for `(document_id, key)` first and updates it rather
    than adding a second row: `document_metadata` has no unique constraint on
    that pair (see this module's docstring on why it's still EAV rather than
    fixed columns), so blindly re-inserting on every edit would silently
    accumulate duplicate rows for the same key instead of replacing the
    value `get_metadata_value` reads back.
    """
    stmt = select(DocumentMetadata).where(
        DocumentMetadata.document_id == document_id, DocumentMetadata.key == key
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is not None:
        row.value = value
    else:
        session.add(DocumentMetadata(document_id=document_id, key=key, value=value))
    await session.flush()


async def soft_delete_document(
    session: AsyncSession, document_id: uuid.UUID, *, deleted_at: datetime
) -> Document | None:
    """Mark `document_id` deleted (used for `reject_document` -- see
    service.py's docstring for why rejection is a soft delete rather than a
    third `status` value), returning the updated row or None if it doesn't
    exist.
    """
    row = await session.get(Document, document_id)
    if row is None:
        return None
    row.deleted_at = deleted_at
    await session.flush()
    await session.refresh(row)
    return row
