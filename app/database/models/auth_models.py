"""Why it exists: To support secure refresh tokens that are stored as hashes, 
can be revoked, and support refresh-token rotation.
Cascade deletion: If a user or organization is deleted, 
its associated refresh-token records are automatically deleted 
because they are temporary session data, not permanent business records.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class RefreshToken(Base):
    """One issued refresh token 
    Rotation / reuse detection: every refresh token belongs to a `family_id`,
    shared by every token descended from one original login. When a token is
    used, core/auth's service issues a new token *in the same family* and
    marks this row `revoked_at` (rotation) rather than reusing the row. If a
    token whose `revoked_at` is already set is ever presented again, that is
    a reuse signal -- the entire `family_id` should be revoked immediately,
    since it indicates the token was very likely stolen and the legitimate
    client's copy has since been rotated away from underneath the attacker's
    copy. `family_id` (rather than walking a `replaced_by` linked list) makes
    "revoke this whole compromised chain" a single-column filter.

    Never stores the raw token value: `token_hash` is a cryptographic hash
    (computed by core/auth/service.py, not here) of the token actually handed
    to the client -- a stolen database row is useless without also finding a
    hash collision, matching the "never in plaintext" requirement.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_org", "user_id", "organization_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Shared by every token descended from one original login; not a foreign
    # key to anything -- it's a grouping id generated at first issuance, not
    # a reference to a row that itself exists.
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # SHA-256 (or equivalent) hash of the actual token value handed to the
    # client. Unique: two distinct tokens hashing to the same value would
    # otherwise be indistinguishable at lookup time.
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set on rotation (this token was exchanged for a new one) or explicit
    # revocation (logout, admin-forced termination, reuse-detected family
    # revocation). NULL means "still valid, if not expired."
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Deliberately no `updated_at`: the only mutation this row ever undergoes
    # is being revoked/rotated, and `revoked_at` itself already captures that
    # timestamp -- a separate generic `updated_at` would be redundant.
