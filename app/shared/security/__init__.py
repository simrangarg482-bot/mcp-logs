"""Envelope encryption for per-tenant connector credentials (PROJECT_PLAN.md
section 12.5, Milestone 10).

Owned by: shared/ -- like `shared/schemas` and `shared/config`, this is
genuinely cross-module: `core.tenancy.service.register_connector` encrypts a
credential at write time, `ingestion.service._execute_ingestion_job` decrypts
it at read time, and neither module owns the other, so the helper lives
where both can reach it without creating a `core -> ingestion` or
`ingestion -> core.tenancy`-shaped dependency for this alone (ingestion
already depends on `core.tenancy` for other reasons -- see
`ingestion.service`'s own module docstring -- but this specific piece has no
reason to add to that).

See `envelope.py` for the actual encrypt/decrypt functions and `kms.py` for
the `KeyManagementService` abstraction they're built on.

Deliberate simplification, flagged rather than silently assumed away:
PROJECT_PLAN.md section 12.5's prose describes `core/tenancy` as storing
"only a reference/identifier to the secret record" (implying a dedicated
secrets-record table, separate from `connector_configs`). This codebase has
no such table -- `connector_configs.credential_ref` is a single `Text`
column. Rather than adding a new table and a schema migration for this pass,
`register_connector` stores the full envelope-encrypted blob (encrypted DEK
+ nonce + ciphertext, serialized as JSON) directly in that same column. This
still delivers section 12.5's actual safety property in full ("the database
only ever stores the encrypted secret and the encrypted DEK, never a usable
plaintext credential nor the KEK itself") -- it just skips the extra
indirection layer the prose's phrasing implies. A dedicated secrets-record
table remains a reasonable future refinement if a need for it (e.g.
secret rotation independent of `connector_configs` rows, or one secret
shared by multiple configs) becomes concrete.
"""

from __future__ import annotations

from app.shared.security.envelope import decrypt_secret, encrypt_secret
from app.shared.security.kms import (
    KeyManagementService,
    KmsUnavailableError,
    LocalKeyManagementService,
    get_kms,
)
from app.shared.security.tokens import generate_opaque_token, hash_opaque_token

__all__ = [
    "KeyManagementService",
    "KmsUnavailableError",
    "LocalKeyManagementService",
    "decrypt_secret",
    "encrypt_secret",
    "generate_opaque_token",
    "get_kms",
    "hash_opaque_token",
]
