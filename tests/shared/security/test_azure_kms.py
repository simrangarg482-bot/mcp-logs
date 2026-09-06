"""Tests for `app.shared.security.kms.AzureKeyVaultKeyManagementService` --
mocked at the `CryptographyClient` boundary (no real Key Vault, no Azure
credentials, no network access), per PROJECT_PLAN.md's explicit "do not make
the test suite depend on a live Azure Key Vault" requirement. A focused,
credentials-gated live integration test is a separate, out-of-scope concern
(see this module's own docstring on why one isn't included here).

`_FakeCryptographyClient` simulates Key Vault's wrap/unwrap semantics
closely enough to prove this class's own logic (algorithm choice, key_id/
key_version threading, error handling) is correct: a real wrap/unwrap round
trip through actual RSA-OAEP-256 is Azure's own tested behavior, not
something this codebase needs to re-verify.
"""

from __future__ import annotations

import uuid

import pytest

from app.shared.security.kms import AzureKeyVaultKeyManagementService, KmsUnavailableError


class _FakeWrapResult:
    def __init__(self, *, key_id: str, encrypted_key: bytes) -> None:
        self.key_id = key_id
        self.encrypted_key = encrypted_key


class _FakeUnwrapResult:
    def __init__(self, *, key: bytes) -> None:
        self.key = key


class _FakeCryptographyClient:
    """Simulates Key Vault's wrap/unwrap against one specific key_id (the
    real SDK's `CryptographyClient` is likewise constructed against one
    key -- see `AzureKeyVaultKeyManagementService._build_crypto_client`).
    "Wrapping" here is a trivial reversible XOR against the key_id's own
    bytes -- not real cryptography, just enough to prove unwrap only
    succeeds when targeted at the exact key_id that wrapped it.
    """

    instances: list["_FakeCryptographyClient"] = []

    def __init__(self, key_id: str, credential: object) -> None:
        self.key_id = key_id
        self.credential = credential
        self.wrap_calls: list[tuple[object, bytes]] = []
        self.unwrap_calls: list[tuple[object, bytes]] = []
        _FakeCryptographyClient.instances.append(self)

    def _xor(self, data: bytes) -> bytes:
        pad = self.key_id.encode("utf-8")
        return bytes(b ^ pad[i % len(pad)] for i, b in enumerate(data))

    async def wrap_key(self, algorithm, key: bytes):
        self.wrap_calls.append((algorithm, key))
        return _FakeWrapResult(key_id=self.key_id, encrypted_key=self._xor(key))

    async def unwrap_key(self, algorithm, encrypted_key: bytes):
        self.unwrap_calls.append((algorithm, encrypted_key))
        return _FakeUnwrapResult(key=self._xor(encrypted_key))

    async def __aenter__(self) -> "_FakeCryptographyClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_fake_client_instances():
    _FakeCryptographyClient.instances = []
    yield
    _FakeCryptographyClient.instances = []


@pytest.fixture()
def kms(monkeypatch) -> AzureKeyVaultKeyManagementService:
    monkeypatch.setattr(
        "azure.keyvault.keys.crypto.aio.CryptographyClient", _FakeCryptographyClient
    )
    return AzureKeyVaultKeyManagementService(
        vault_url="https://ekip-test.vault.azure.net",
        key_name="connector-secrets-kek",
        credential=object(),
    )


@pytest.mark.asyncio
async def test_generate_data_key_wraps_via_the_unversioned_key_id(kms) -> None:
    dek, encrypted_dek, key_version = await kms.generate_data_key()

    assert len(dek) == 32
    assert encrypted_dek != dek
    # `generate_data_key` calls Key Vault against the unversioned key id
    # (letting the vault resolve whichever version is currently enabled for
    # wrap), but the *returned* key_version is that resolved client's own
    # key_id -- here, the fake echoes back the id it was constructed with.
    assert key_version == "https://ekip-test.vault.azure.net/keys/connector-secrets-kek"
    assert len(_FakeCryptographyClient.instances) == 1
    assert _FakeCryptographyClient.instances[0].wrap_calls[0][1] == dek


@pytest.mark.asyncio
async def test_generate_then_decrypt_round_trips(kms) -> None:
    dek, encrypted_dek, key_version = await kms.generate_data_key()

    result = await kms.decrypt_data_key(encrypted_dek, key_version)

    assert result == dek


@pytest.mark.asyncio
async def test_decrypt_data_key_targets_the_exact_stored_key_version(kms) -> None:
    """Simulates a rotated vault: the DEK was wrapped under an OLD version's
    key_id, and `decrypt_data_key` must construct its `CryptographyClient`
    against that exact old version -- never the unversioned/"current" key
    id -- or a real rotated Key Vault would reject the unwrap.
    """
    old_version_id = "https://ekip-test.vault.azure.net/keys/connector-secrets-kek/old-version-abc"
    dek = b"\x01" * 32
    # Simulate an encrypted DEK that was wrapped under `old_version_id` by
    # constructing a fake client for that id directly (mirroring what
    # generate_data_key would have produced against that older version).
    wrapping_client = _FakeCryptographyClient(old_version_id, object())
    wrap_result = await wrapping_client.wrap_key(None, dek)

    result = await kms.decrypt_data_key(wrap_result.encrypted_key, old_version_id)

    assert result == dek
    # The client actually used for unwrap was constructed against the old
    # version's id, not the unversioned key id `kms` was configured with.
    unwrap_client = _FakeCryptographyClient.instances[-1]
    assert unwrap_client.key_id == old_version_id


@pytest.mark.asyncio
async def test_decrypt_data_key_rejects_a_missing_key_version(kms) -> None:
    with pytest.raises(ValueError, match="requires a stored key_version"):
        await kms.decrypt_data_key(b"some-bytes", "")


@pytest.mark.asyncio
async def test_generate_data_key_raises_if_wrap_returns_no_key_id(monkeypatch) -> None:
    class _NoKeyIdClient(_FakeCryptographyClient):
        async def wrap_key(self, algorithm, key: bytes):
            return _FakeWrapResult(key_id=None, encrypted_key=self._xor(key))

    monkeypatch.setattr("azure.keyvault.keys.crypto.aio.CryptographyClient", _NoKeyIdClient)
    kms = AzureKeyVaultKeyManagementService(
        vault_url="https://ekip-test.vault.azure.net", key_name="k", credential=object()
    )

    with pytest.raises(RuntimeError, match="no key_id"):
        await kms.generate_data_key()


@pytest.mark.asyncio
async def test_unauthorized_key_vault_access_fails_safely_with_no_fallback(monkeypatch) -> None:
    """Security regression: if the vault denies access (wrong/missing RBAC
    role, expired managed-identity token, etc.), the operation must raise --
    never silently return a usable-looking result, and never fall back to
    any other provider. `get_kms()` never constructs a `LocalKeyManagementService`
    once `kms_provider=azure` is selected (see that function's own
    docstring), so there is no fallback path for this class to accidentally
    take even if it wanted to.

    It must still raise, but as of the connector-registration/SSO-configure
    production incident this session diagnosed and fixed, the *type* it
    raises changed: a bare `azure.core.exceptions.AzureError` (of which
    `ClientAuthenticationError` is one) reaching a caller unhandled becomes
    an unhandled 500 at the API boundary that never passes back through
    `CORSMiddleware` -- indistinguishable, in a browser, from a CORS
    failure, which is exactly what made this bug so hard to diagnose live.
    `generate_data_key`/`decrypt_data_key` now catch `AzureError` and
    re-raise `KmsUnavailableError` instead, which `core.tenancy.service.
    register_connector`/`configure_sso` in turn catch and translate to a
    normal `ServiceUnavailableError` (503) -- see
    `tests/core/tenancy/test_service.py`'s coverage of that translation.
    """
    from azure.core.exceptions import ClientAuthenticationError

    class _DeniedClient(_FakeCryptographyClient):
        async def wrap_key(self, algorithm, key: bytes):
            raise ClientAuthenticationError(message="The user, group, or application does not have permission")

    monkeypatch.setattr("azure.keyvault.keys.crypto.aio.CryptographyClient", _DeniedClient)
    kms = AzureKeyVaultKeyManagementService(
        vault_url="https://ekip-test.vault.azure.net", key_name="k", credential=object()
    )

    with pytest.raises(KmsUnavailableError) as exc_info:
        await kms.generate_data_key()
    assert isinstance(exc_info.value.__cause__, ClientAuthenticationError)


@pytest.mark.asyncio
async def test_decrypt_data_key_wraps_azure_errors_the_same_way(monkeypatch) -> None:
    """`decrypt_data_key` shares the exact same failure mode as
    `generate_data_key` (both make a real Key Vault call) -- this pins the
    same translation on the unwrap path, not just wrap.
    """
    from azure.core.exceptions import ServiceRequestError

    class _UnreachableClient(_FakeCryptographyClient):
        async def unwrap_key(self, algorithm, encrypted_key: bytes):
            raise ServiceRequestError(message="Could not connect to the vault endpoint")

    monkeypatch.setattr("azure.keyvault.keys.crypto.aio.CryptographyClient", _UnreachableClient)
    kms = AzureKeyVaultKeyManagementService(
        vault_url="https://ekip-test.vault.azure.net", key_name="k", credential=object()
    )

    with pytest.raises(KmsUnavailableError) as exc_info:
        await kms.decrypt_data_key(b"whatever", "https://ekip-test.vault.azure.net/keys/k/v1")
    assert isinstance(exc_info.value.__cause__, ServiceRequestError)


@pytest.mark.asyncio
async def test_full_envelope_round_trip_through_azure_provider(kms) -> None:
    """End-to-end through `envelope.encrypt_secret`/`decrypt_secret` (not
    just this class in isolation) -- proves the Azure provider is a drop-in
    replacement for `LocalKeyManagementService` from envelope.py's point of
    view, satisfying the same `KeyManagementService` protocol.
    """
    from app.shared.security.envelope import decrypt_secret, encrypt_secret

    plaintext = f"github_pat_{uuid.uuid4().hex}"
    encrypted = await encrypt_secret(kms, plaintext)
    assert plaintext not in encrypted

    assert await decrypt_secret(kms, encrypted) == plaintext
