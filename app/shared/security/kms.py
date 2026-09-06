"""Key Management Service abstraction (PROJECT_PLAN.md section 12.5).

`KeyManagementService` is the seam between "envelope encryption logic"
(`envelope.py`, which never changes) and "who actually holds the
key-encryption-key" (this file, which is exactly what changes when this
codebase moves from local development to a real production deployment).

Two implementations:

  - `LocalKeyManagementService`: a deliberate, explicitly-flagged
    development/test-only stand-in. It holds its KEK as a plain settings
    value (`connector_secret_master_key`) rather than in a separate,
    hardware-backed trust boundary -- meaning, unlike section 12.5's stated
    property ("a database compromise alone does not expose customer
    connector credentials -- the KMS is a separate trust boundary that must
    also be compromised"), a compromise of this application's own runtime
    environment (which already has the KEK in memory/settings) *would* be
    sufficient to decrypt every stored credential.
  - `AzureKeyVaultKeyManagementService` (Phase 3): the real production
    provider. The KEK (an RSA key) never leaves Key Vault; this class only
    ever sends/receives *wrapped* data-encryption-keys over the wire, via
    Key Vault's own `wrap_key`/`unwrap_key` cryptographic operations.
    Authenticates via `azure.identity.aio.DefaultAzureCredential`, which
    resolves to managed/workload identity automatically in Azure and to a
    developer's own `az login` session locally -- no Azure client secret is
    ever configured as an application setting.

Both satisfy the same `KeyManagementService` protocol, so no caller of
`envelope.encrypt_secret`/`decrypt_secret` (or anything above them --
`core.tenancy.service.register_connector`/`configure_sso`,
`core.mcp_oauth.service`, `ingestion.service`) needs to know or care which
provider is actually selected; `get_kms()` (this module's only public
factory) is the one place that decides, from `Settings.kms_provider`.

Both `generate_data_key`/`decrypt_data_key` are `async def`: the Azure
provider's wrap/unwrap calls are real network I/O against Key Vault, and
this codebase is async end-to-end (FastAPI + asyncpg) -- a synchronous
Key Vault SDK call here would block the event loop for the round-trip
duration on every connector/SSO secret encrypt or decrypt. The local
provider does no I/O at all but implements the same async signature so
every caller can `await` either provider identically.

**Key versioning** (`key_version`, the third element `generate_data_key`
returns and the second argument `decrypt_data_key` takes): local KEKs never
rotate, so `LocalKeyManagementService` treats this as an opaque, ignored
value. A real KMS key *does* rotate, and Key Vault's wrap/unwrap operations
are inherently per-key-*version* (there is no "unwrap with whichever version
is current" call) -- `AzureKeyVaultKeyManagementService.generate_data_key`
returns the exact versioned key id Key Vault used to wrap that DEK, stored
alongside the ciphertext (`envelope.py`'s serialized envelope) so a later
`decrypt_data_key` call can target that same version even after the vault's
"current" version has since rotated forward. See `envelope.py`'s docstring
for exactly how this is stored, and why it stays backward-compatible with
already-stored envelopes that predate this field.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.shared.config.settings import get_settings

_NONCE_SIZE_BYTES = 12  # AES-GCM's standard/recommended nonce size.
_DEK_SIZE_BYTES = 32  # AES-256.

# `LocalKeyManagementService` has exactly one KEK and no version concept --
# every DEK it wraps is tagged with this fixed, non-secret placeholder
# rather than `None`, so `decrypt_data_key`'s signature stays uniform across
# both providers (a real key_version is never falsy-but-meaningful).
_LOCAL_KEY_VERSION = "local"


class KeyManagementService(Protocol):
    """Wraps/unwraps a per-secret data-encryption-key (DEK) using a
    key-encryption-key (KEK) this interface deliberately never exposes to
    its callers -- `envelope.py` only ever sees plaintext/encrypted DEKs and
    an opaque `key_version` string, never the KEK itself, so a real
    KMS-backed implementation can keep the KEK entirely server-side without
    changing this contract.
    """

    async def generate_data_key(self) -> tuple[bytes, bytes, str]:
        """Generate a fresh random DEK, returning `(plaintext_dek,
        encrypted_dek, key_version)`. Called once per secret encrypted -- a
        fresh DEK per secret, not one DEK reused across every secret, so
        compromising one decrypted DEK never exposes any other secret.
        `key_version` identifies which KMS key (and, for a real KMS, which
        version of it) produced `encrypted_dek` -- opaque to every caller
        except this same provider's own `decrypt_data_key`.
        """
        ...

    async def decrypt_data_key(self, encrypted_dek: bytes, key_version: str | None) -> bytes:
        """Unwrap a previously-encrypted DEK back to its plaintext bytes,
        using the same `key_version` `generate_data_key` returned for it.
        """
        ...


class LocalKeyManagementService:
    """Development/test-only stand-in -- see module docstring for exactly
    what property this does and does not provide. Never select this for a
    production deployment; `get_settings()`'s own validation refuses to
    start the app with `environment=production` and `kms_provider=local`
    (see `Settings._reject_local_kms_in_production`).
    """

    def __init__(self, kek: bytes) -> None:
        if len(kek) != _DEK_SIZE_BYTES:
            raise ValueError(
                f"KEK must be exactly {_DEK_SIZE_BYTES} bytes, got {len(kek)}."
            )
        self._kek = kek

    async def generate_data_key(self) -> tuple[bytes, bytes, str]:
        dek = os.urandom(_DEK_SIZE_BYTES)
        nonce = os.urandom(_NONCE_SIZE_BYTES)
        ciphertext = AESGCM(self._kek).encrypt(nonce, dek, None)
        # Nonce is not secret -- it's packed alongside the ciphertext (a
        # standard AES-GCM convention) since `decrypt_data_key` needs it back
        # and there is nowhere else to carry it in this interface's shape.
        encrypted_dek = nonce + ciphertext
        return dek, encrypted_dek, _LOCAL_KEY_VERSION

    async def decrypt_data_key(self, encrypted_dek: bytes, key_version: str | None) -> bytes:
        # `key_version` is intentionally ignored -- this provider has never
        # had more than one KEK, so there is nothing to select between; the
        # parameter only exists to satisfy `KeyManagementService` uniformly.
        del key_version
        nonce, ciphertext = (
            encrypted_dek[:_NONCE_SIZE_BYTES],
            encrypted_dek[_NONCE_SIZE_BYTES:],
        )
        return AESGCM(self._kek).decrypt(nonce, ciphertext, None)


class AzureKeyVaultKeyManagementService:
    """Production `KeyManagementService` backed by Azure Key Vault.

    Envelope-wrap-key pattern: Key Vault has no "generate a random data key"
    operation the way AWS KMS does, so the DEK is generated locally (same
    `os.urandom(32)` the local provider uses) and only ever *wrapped*/
    *unwrapped* by Key Vault -- the RSA key-encryption-key itself never
    leaves the vault; this class only ever sends/receives ciphertext and a
    key id string over the wire.

    A fresh `CryptographyClient` is created per call rather than held open
    across calls -- deliberately, not an oversight: every caller of
    `encrypt_secret`/`decrypt_secret` in this codebase runs at
    connector-registration, SSO-configuration, or once-per-ingestion-sync
    time, never in a per-API-request hot path, so the extra per-call client
    construction (and the token-acquisition it triggers, itself cached
    internally by `DefaultAzureCredential`) is immaterial here -- and it
    avoids this class needing to manage a client-lifecycle/close() story of
    its own. Revisit only if a future caller puts this on an actual hot
    path.
    """

    _WRAP_ALGORITHM_NAME = "RSA-OAEP-256"

    def __init__(self, *, vault_url: str, key_name: str, credential: object) -> None:
        """`credential` is typed `object` here (not the real Azure type) so
        this module's public surface never needs `azure.identity` imported
        at type-check time either -- only `_build_crypto_client` (below,
        called at actual call time) touches the Azure SDK, keeping every
        other module in this codebase free to import `kms.py` without
        pulling in Azure at all, even when `KMS_PROVIDER=azure`.
        """
        self._key_id = f"{vault_url.rstrip('/')}/keys/{key_name}"
        self._credential = credential

    async def generate_data_key(self) -> tuple[bytes, bytes, str]:
        from azure.core.exceptions import AzureError
        from azure.keyvault.keys.crypto import KeyWrapAlgorithm

        dek = os.urandom(_DEK_SIZE_BYTES)
        try:
            async with self._build_crypto_client(self._key_id) as crypto_client:
                result = await crypto_client.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, dek)
        except AzureError as exc:
            # Any Key Vault network/auth failure (unreachable vault, no valid
            # `DefaultAzureCredential` source available -- e.g. this process
            # is not actually running on Azure and has no explicit service
            # principal configured) lands here. Re-raised as `KmsUnavailableError`
            # rather than left as a raw Azure SDK exception so callers that
            # only import this module (not the Azure SDK) can catch it by
            # name, and so it never reaches the API boundary as an opaque,
            # untranslated 500 (see that class's own docstring for why this
            # matters -- it was previously indistinguishable from a CORS
            # failure to the browser, since an unhandled exception here
            # never passes back through `CORSMiddleware`).
            raise KmsUnavailableError(
                "Azure Key Vault is unreachable or this process could not "
                "authenticate to it (DefaultAzureCredential found no usable "
                "credential source)."
            ) from exc
        if not result.key_id:
            raise RuntimeError(
                "Azure Key Vault wrap_key returned no key_id -- cannot record which "
                "key version wrapped this secret, so it could never be decrypted later."
            )
        return dek, result.encrypted_key, result.key_id

    async def decrypt_data_key(self, encrypted_dek: bytes, key_version: str | None) -> bytes:
        from azure.core.exceptions import AzureError
        from azure.keyvault.keys.crypto import KeyWrapAlgorithm

        if not key_version:
            raise ValueError(
                "Azure Key Vault KMS requires a stored key_version to unwrap a DEK -- "
                "got none (encrypted under a different provider, or a corrupt envelope?)."
            )
        # `key_version` here is the *full versioned key id* `generate_data_key`
        # returned (e.g. "https://vault.vault.azure.net/keys/name/abc123") --
        # pinning to that exact version even if the vault's current version
        # has since rotated forward (PROJECT_PLAN.md's key-rotation
        # requirement: old ciphertext must remain decryptable).
        try:
            async with self._build_crypto_client(key_version) as crypto_client:
                result = await crypto_client.unwrap_key(
                    KeyWrapAlgorithm.rsa_oaep_256, encrypted_dek
                )
        except AzureError as exc:
            # Same translation as `generate_data_key` above -- see that
            # branch's comment for why this must not reach a caller as a
            # raw, untranslated Azure SDK exception.
            raise KmsUnavailableError(
                "Azure Key Vault is unreachable or this process could not "
                "authenticate to it (DefaultAzureCredential found no usable "
                "credential source)."
            ) from exc
        return result.key

    def _build_crypto_client(self, key_id: str):
        from azure.keyvault.keys.crypto.aio import CryptographyClient

        return CryptographyClient(key_id, self._credential)


class KmsUnavailableError(RuntimeError):
    """Raised when a real KMS backend (currently: Azure Key Vault) cannot
    complete a wrap/unwrap operation for a reason that has nothing to do
    with the caller's request -- the vault is unreachable, or this process
    has no credential `DefaultAzureCredential` can use (no managed identity,
    no service-principal env vars; the common case for a deployment that
    isn't actually running on Azure infrastructure).

    Deliberately a plain `RuntimeError`, not `app.core.exceptions.EKIPError`:
    this module sits below `app.core` (every `app.core` service is free to
    import `app.shared.security`, never the other way around -- see
    ARCHITECTURE.md section 3 / this repo's import-linter contracts), so it
    cannot depend on `core`'s exception hierarchy without inverting that
    layering. Callers in `app.core` (e.g. `core.tenancy.service.
    register_connector`/`configure_sso`) catch this and re-raise as their
    own `ServiceUnavailableError` -- the same translation-at-the-boundary
    pattern `EKIPError`'s own docstring already describes for REST/MCP, just
    one layer further down. Letting this (or the raw Azure SDK exception it
    replaces) propagate unhandled is exactly the bug this type exists to
    close: an un-translated exception here reaches FastAPI's `ServerErrorMiddleware`
    as a bare 500 that never passes back through `CORSMiddleware`, which is
    indistinguishable, in a browser, from a CORS failure -- not a real one.
    """


class LocalKmsRequiredInProductionError(RuntimeError):
    """Raised by `get_kms()` if `Settings.kms_provider` somehow resolves to
    `"local"` in a `production` environment -- `Settings` itself already
    validates against this at settings-construction time
    (`_reject_local_kms_in_production`), so reaching this branch would mean
    that guard was bypassed (e.g. `Settings` constructed directly with
    validation skipped), not a normal runtime path. A second, redundant
    check here rather than trusting the settings layer alone: this is the
    one function a KMS_PROVIDER downgrade could slip through unnoticed via,
    so it fails loudly on its own, independent of `Settings`'s validator.
    """


@lru_cache
def get_kms() -> KeyManagementService:
    """Cached accessor -- same `@lru_cache`-wrapped-singleton pattern
    `shared.config.settings.get_settings` already established, so every
    caller shares one KMS provider instance rather than reconstructing it
    (and, for the local provider, re-parsing `connector_secret_master_key`)
    on every call.

    Selects the provider from `Settings.kms_provider` -- never falls back
    from `"azure"` to `"local"` for any reason (PROJECT_PLAN.md's explicit
    "production must never silently downgrade secret protection"
    requirement): a misconfigured or unreachable Key Vault must fail this
    call (or the first real operation against the returned client), not
    quietly hand back a weaker provider.
    """
    settings = get_settings()

    if settings.kms_provider == "azure":
        from azure.identity.aio import DefaultAzureCredential

        return AzureKeyVaultKeyManagementService(
            vault_url=settings.azure_key_vault_url,
            key_name=settings.azure_key_vault_key_name,
            credential=DefaultAzureCredential(),
        )

    if settings.environment == "production":
        # Belt-and-suspenders: Settings' own validator should already have
        # refused to construct this configuration at all (see
        # `_reject_local_kms_in_production`) -- this branch existing at all
        # means that guard was somehow bypassed.
        raise LocalKmsRequiredInProductionError(
            "Refusing to use LocalKeyManagementService with environment=production. "
            "Set KMS_PROVIDER=azure (and its required azure_key_vault_* settings)."
        )

    kek = bytes.fromhex(settings.connector_secret_master_key)
    return LocalKeyManagementService(kek)
