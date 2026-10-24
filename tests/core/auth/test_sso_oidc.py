"""Mock-based tests for OIDC discovery, code exchange, and identity claims."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt as jose_jwt

from app.core.auth import service as auth_service
from app.core.exceptions import PermissionDeniedError
from app.core.tenancy.schemas import SSOConfiguration

ISSUER = "https://idp.example.test"
TOKEN_ENDPOINT = "https://idp.example.test/token"
JWKS_URI = "https://idp.example.test/keys"


class _FakeResponse:
    def __init__(
        self, payload=None, *, status_code: int = 200, json_error: Exception | None = None
    ):
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://idp.example.test")
            response = httpx.Response(self.status_code, request=request)
            response.raise_for_status()

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


def _patch_http(monkeypatch, *, get_responses, post_response=None):
    calls: list[tuple[str, str, dict | None]] = []

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            calls.append(("GET", url, None))
            if url not in get_responses:
                raise AssertionError(f"Unexpected GET {url}")
            return get_responses[url]

        async def post(self, url, *, data):
            calls.append(("POST", url, data))
            if url != TOKEN_ENDPOINT or post_response is None:
                raise AssertionError(f"Unexpected POST {url}")
            return post_response

    monkeypatch.setattr(auth_service.httpx, "AsyncClient", _FakeAsyncClient)
    return calls


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    auth_service._discovery_cache.clear()
    yield
    auth_service._discovery_cache.clear()


@pytest.fixture
def sso_config() -> SSOConfiguration:
    now = datetime.now(UTC)
    return SSOConfiguration(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        provider="okta",
        protocol="oidc",
        issuer_url=ISSUER,
        client_id="test-client-id",
        client_secret_ref="encrypted-envelope",
        created_at=now,
        updated_at=now,
    )


def _b64url_int(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _new_signing_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-kid",
        "use": "sig",
        "alg": "RS256",
        "n": _b64url_int(numbers.n),
        "e": _b64url_int(numbers.e),
    }
    return private_key, jwk


def _valid_claims(**overrides):
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "sub": "provider-user-123",
        "aud": "test-client-id",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "email": "person@example.test",
        "name": "Test Person",
        "groups": ["engineering", "operators"],
    }
    claims.update(overrides)
    return claims


async def _exchange_signed_token(
    monkeypatch, sso_config, *, claims=None, signing_key=None, jwks=None, token_payload=None
):
    private_key, signing_jwk = signing_key or _new_signing_key()
    id_token = jose_jwt.encode(
        claims if claims is not None else _valid_claims(),
        private_key,
        algorithm="RS256",
        headers={"kid": "test-kid"},
    )
    auth_service._discovery_cache[ISSUER] = (
        {"token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    calls = _patch_http(
        monkeypatch,
        get_responses={JWKS_URI: _FakeResponse(jwks or {"keys": [signing_jwk]})},
        post_response=_FakeResponse(token_payload or {"id_token": id_token}),
    )

    async def fake_resolve_client_secret(reference):
        assert reference == "encrypted-envelope"
        return "decrypted-test-secret"

    monkeypatch.setattr(auth_service, "_resolve_client_secret", fake_resolve_client_secret)
    result = await auth_service._exchange_code_for_claims(
        sso_config,
        code="authorization-code",
        code_verifier="pkce-verifier",
        redirect_uri="https://app.example.test/callback",
    )
    return result, calls


@pytest.mark.asyncio
async def test_discovery_fetches_and_caches_document(monkeypatch) -> None:
    expected = {"authorization_endpoint": "https://idp.example.test/authorize"}
    calls = _patch_http(
        monkeypatch,
        get_responses={f"{ISSUER}/.well-known/openid-configuration": _FakeResponse(expected)},
    )

    first = await auth_service._get_discovery_document(ISSUER)
    second = await auth_service._get_discovery_document(ISSUER)

    assert first == expected
    assert second == expected
    assert calls == [("GET", f"{ISSUER}/.well-known/openid-configuration", None)]


@pytest.mark.asyncio
async def test_expired_discovery_cache_entry_is_refetched(monkeypatch) -> None:
    discovery_url = f"{ISSUER}/.well-known/openid-configuration"
    refreshed = {"authorization_endpoint": "https://idp.example.test/new-authorize"}
    auth_service._discovery_cache[ISSUER] = (
        {"authorization_endpoint": "https://idp.example.test/old-authorize"},
        datetime.now(UTC) - auth_service._DISCOVERY_CACHE_TTL - timedelta(seconds=1),
    )
    calls = _patch_http(
        monkeypatch, get_responses={discovery_url: _FakeResponse(refreshed)}
    )

    result = await auth_service._get_discovery_document(ISSUER)

    assert result == refreshed
    assert calls == [("GET", discovery_url, None)]


@pytest.mark.asyncio
async def test_discovery_http_error_is_not_cached(monkeypatch) -> None:
    discovery_url = f"{ISSUER}/.well-known/openid-configuration"
    calls = _patch_http(monkeypatch, get_responses={discovery_url: _FakeResponse(status_code=503)})

    with pytest.raises(httpx.HTTPStatusError):
        await auth_service._get_discovery_document(ISSUER)
    assert ISSUER not in auth_service._discovery_cache
    assert calls[0][1] == discovery_url


@pytest.mark.asyncio
async def test_malformed_discovery_json_is_not_cached(monkeypatch) -> None:
    discovery_url = f"{ISSUER}/.well-known/openid-configuration"
    calls = _patch_http(
        monkeypatch,
        get_responses={
            discovery_url: _FakeResponse(json_error=json.JSONDecodeError("bad json", "{", 0))
        },
    )

    with pytest.raises(json.JSONDecodeError):
        await auth_service._get_discovery_document(ISSUER)
    assert ISSUER not in auth_service._discovery_cache
    assert calls[0][0] == "GET"


@pytest.mark.asyncio
async def test_missing_authorization_endpoint_fails_closed(monkeypatch) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"token_endpoint": TOKEN_ENDPOINT},
        datetime.now(UTC),
    )

    with pytest.raises(KeyError, match="authorization_endpoint"):
        await auth_service._discover_authorization_endpoint(ISSUER)


@pytest.mark.asyncio
async def test_exchange_verifies_signature_and_extracts_standard_groups(
    monkeypatch, sso_config
) -> None:
    result, calls = await _exchange_signed_token(monkeypatch, sso_config)

    assert result.sub == "provider-user-123"
    assert result.email == "person@example.test"
    assert result.groups == ("engineering", "operators")
    post = next(call for call in calls if call[0] == "POST")
    assert post[2] == {
        "grant_type": "authorization_code",
        "code": "authorization-code",
        "redirect_uri": "https://app.example.test/callback",
        "client_id": "test-client-id",
        "client_secret": "decrypted-test-secret",
        "code_verifier": "pkce-verifier",
    }


@pytest.mark.asyncio
async def test_exchange_rejects_token_http_error(monkeypatch, sso_config) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(monkeypatch, get_responses={}, post_response=_FakeResponse(status_code=401))
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(httpx.HTTPStatusError):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
        )


async def _fake_secret(reference: str) -> str:
    assert reference == "encrypted-envelope"
    return "decrypted-test-secret"


@pytest.mark.asyncio
async def test_exchange_rejects_malformed_token_response(monkeypatch, sso_config) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(
        monkeypatch,
        get_responses={},
        post_response=_FakeResponse(json_error=json.JSONDecodeError("bad json", "{", 0)),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(json.JSONDecodeError):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
        )


@pytest.mark.asyncio
async def test_exchange_rejects_missing_id_token(monkeypatch, sso_config) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(
        monkeypatch, get_responses={}, post_response=_FakeResponse({"access_token": "opaque"})
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(PermissionDeniedError, match="did not include an ID token"):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
        )


@pytest.mark.asyncio
async def test_exchange_rejects_malformed_id_token(monkeypatch, sso_config) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(
        monkeypatch,
        get_responses={JWKS_URI: _FakeResponse({"keys": []})},
        post_response=_FakeResponse({"id_token": "not-a-jwt"}),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(PermissionDeniedError, match="malformed ID token"):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
        )


@pytest.mark.asyncio
async def test_exchange_rejects_unknown_kid(monkeypatch, sso_config) -> None:
    _, signing_jwk = _new_signing_key()
    signing_jwk["kid"] = "different-kid"
    auth_service._discovery_cache[ISSUER] = (
        {"token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    private_key, _ = _new_signing_key()
    id_token = jose_jwt.encode(
        _valid_claims(), private_key, algorithm="RS256", headers={"kid": "missing-kid"}
    )
    _patch_http(
        monkeypatch,
        get_responses={JWKS_URI: _FakeResponse({"keys": [signing_jwk]})},
        post_response=_FakeResponse({"id_token": id_token}),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(PermissionDeniedError, match="matching signing key"):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
        )


@pytest.mark.asyncio
async def test_exchange_rejects_invalid_signature_with_matching_kid(
    monkeypatch, sso_config
) -> None:
    signing_key, _ = _new_signing_key()
    _, unrelated_jwk = _new_signing_key()
    unrelated_jwk["kid"] = "test-kid"
    id_token = jose_jwt.encode(
        _valid_claims(), signing_key, algorithm="RS256", headers={"kid": "test-kid"}
    )
    auth_service._discovery_cache[ISSUER] = (
        {"token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(
        monkeypatch,
        get_responses={JWKS_URI: _FakeResponse({"keys": [unrelated_jwk]})},
        post_response=_FakeResponse({"id_token": id_token}),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(PermissionDeniedError, match="signature or claims verification failed"):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_changes",
    [
        {"exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())},
        {"iss": "https://attacker.example.test"},
        {"aud": "another-client"},
    ],
    ids=["expired", "wrong-issuer", "wrong-audience"],
)
async def test_exchange_rejects_invalid_standard_claims(
    monkeypatch, sso_config, claim_changes
) -> None:
    with pytest.raises(PermissionDeniedError, match="signature or claims verification failed"):
        await _exchange_signed_token(monkeypatch, sso_config, claims=_valid_claims(**claim_changes))


@pytest.mark.asyncio
async def test_exchange_rejects_missing_email_claim(monkeypatch, sso_config) -> None:
    claims = _valid_claims()
    del claims["email"]
    with pytest.raises(PermissionDeniedError, match="verified email claim"):
        await _exchange_signed_token(monkeypatch, sso_config, claims=claims)


@pytest.mark.asyncio
async def test_exchange_currently_accepts_missing_issued_at_claim(monkeypatch, sso_config) -> None:
    claims = _valid_claims()
    del claims["iat"]

    result, _ = await _exchange_signed_token(monkeypatch, sso_config, claims=claims)

    # OIDC ID tokens require iat. The current decoder accepts this otherwise
    # valid token, documenting a claim-validation gap for the audit report.
    assert result.sub == "provider-user-123"


@pytest.mark.asyncio
async def test_exchange_currently_trusts_email_without_verified_email_claim(
    monkeypatch, sso_config
) -> None:
    claims = _valid_claims(email_verified=False)

    result, _ = await _exchange_signed_token(monkeypatch, sso_config, claims=claims)

    # Provisioning can match invitations/domains using this email, but the
    # current VerifiedIdPClaims contract drops email_verified and trusts it.
    assert result.email == "person@example.test"


@pytest.mark.asyncio
async def test_exchange_fails_closed_but_leaks_keyerror_for_missing_subject(
    monkeypatch, sso_config
) -> None:
    claims = _valid_claims()
    del claims["sub"]

    # This captures an error-handling defect: the signature and issuer/audience
    # still verify, but the missing required claim escapes as an unclassified KeyError.
    with pytest.raises(KeyError, match="sub"):
        await _exchange_signed_token(monkeypatch, sso_config, claims=claims)


@pytest.mark.asyncio
async def test_exchange_rejects_malformed_jwks_response(monkeypatch, sso_config) -> None:
    private_key, _ = _new_signing_key()
    id_token = jose_jwt.encode(
        _valid_claims(), private_key, algorithm="RS256", headers={"kid": "test-kid"}
    )
    auth_service._discovery_cache[ISSUER] = (
        {"token_endpoint": TOKEN_ENDPOINT, "jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    _patch_http(
        monkeypatch,
        get_responses={JWKS_URI: _FakeResponse(status_code=502)},
        post_response=_FakeResponse({"id_token": id_token}),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(httpx.HTTPStatusError):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
        )


@pytest.mark.asyncio
async def test_missing_token_endpoint_fails_closed(monkeypatch, sso_config) -> None:
    auth_service._discovery_cache[ISSUER] = (
        {"jwks_uri": JWKS_URI},
        datetime.now(UTC),
    )
    monkeypatch.setattr(auth_service, "_resolve_client_secret", _fake_secret)

    with pytest.raises(KeyError, match="token_endpoint"):
        await auth_service._exchange_code_for_claims(
            sso_config,
            code="code",
            code_verifier="verifier",
            redirect_uri="https://app.example.test/cb",
        )


@pytest.mark.asyncio
async def test_auth0_namespaced_group_claim_is_not_treated_as_standard_groups(
    monkeypatch, sso_config
) -> None:
    claims = _valid_claims()
    del claims["groups"]
    claims["https://example.test/claims/groups"] = ["engineering"]

    result, _ = await _exchange_signed_token(monkeypatch, sso_config, claims=claims)

    assert result.groups == ()
