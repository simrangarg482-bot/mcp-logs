"""Focused tests for SSO provisioning delegation and EKIP session handling."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from jose import jwt as jose_jwt

from app.core.auth import service as auth_service
from app.core.auth.schemas import RefreshRequest, VerifiedIdPClaims
from app.core.exceptions import PermissionDeniedError
from app.core.tenancy.schemas import ProvisioningDecision
from app.shared.security import KmsUnavailableError, hash_opaque_token

_JWT_SECRET = "unit-test-only-signing-key-with-32-bytes-minimum"


def _settings():
    return SimpleNamespace(
        jwt_secret_key=_JWT_SECRET,
        jwt_algorithm="HS256",
        jwt_expiry_minutes=15,
        org_selection_token_expiry_minutes=10,
    )


@pytest.mark.asyncio
async def test_resolve_client_secret_rejects_invalid_or_missing_envelope() -> None:
    with pytest.raises(ValueError):
        await auth_service._resolve_client_secret("not-an-encrypted-envelope")
    with pytest.raises(ValueError):
        await auth_service._resolve_client_secret("")


@pytest.mark.asyncio
async def test_resolve_client_secret_does_not_hide_missing_kms_configuration(monkeypatch) -> None:
    def fail_to_get_kms():
        raise KmsUnavailableError("test KMS configuration unavailable")

    monkeypatch.setattr(auth_service, "get_kms", fail_to_get_kms)

    with pytest.raises(KmsUnavailableError, match="configuration unavailable"):
        await auth_service._resolve_client_secret("encrypted-envelope")


class _RefreshRow:
    def __init__(self, *, organization_id: uuid.UUID, revoked_at=None, expires_at=None) -> None:
        now = datetime.now(UTC)
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.organization_id = organization_id
        self.family_id = uuid.uuid4()
        self.revoked_at = revoked_at
        self.expires_at = expires_at or now + timedelta(days=30)


@pytest.mark.asyncio
async def test_issue_session_persists_only_refresh_token_hash(monkeypatch) -> None:
    user_id, organization_id, family_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tenant_context: list[uuid.UUID] = []
    inserted: dict[str, object] = {}

    async def fake_set_tenant_context(session, org_id):
        tenant_context.append(org_id)

    async def fake_insert_refresh_token(session, **kwargs):
        inserted.update(kwargs)

    monkeypatch.setattr(auth_service, "get_settings", _settings)
    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant_context)
    monkeypatch.setattr(auth_service.repository, "insert_refresh_token", fake_insert_refresh_token)

    tokens = await auth_service._issue_session(
        None, user_id=user_id, organization_id=organization_id, family_id=family_id
    )
    claims = jose_jwt.decode(tokens.access_token, _JWT_SECRET, algorithms=["HS256"])

    assert tenant_context == [organization_id]
    assert claims["sub"] == str(user_id)
    assert claims["organization_id"] == str(organization_id)
    assert claims["type"] == "access"
    assert inserted["user_id"] == user_id
    assert inserted["organization_id"] == organization_id
    assert inserted["family_id"] == family_id
    assert inserted["token_hash"] == hash_opaque_token(tokens.refresh_token)
    assert inserted["token_hash"] != tokens.refresh_token


def test_verify_access_token_accepts_valid_signed_token(monkeypatch) -> None:
    user_id, organization_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(auth_service, "get_settings", _settings)
    token, _, _ = auth_service._issue_access_token(user_id, organization_id)

    claims = auth_service.verify_access_token(token)

    assert claims.user_id == user_id
    assert claims.organization_id == organization_id


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": str(uuid.uuid4()), "organization_id": str(uuid.uuid4()), "type": "access"},
        {
            "sub": str(uuid.uuid4()),
            "organization_id": str(uuid.uuid4()),
            "type": "org_selection",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
    ],
    ids=["expired-token", "wrong-token-purpose"],
)
def test_verify_access_token_rejects_expired_or_wrong_purpose_tokens(monkeypatch, claims) -> None:
    monkeypatch.setattr(auth_service, "get_settings", _settings)
    if claims.get("type") == "access":
        claims["iat"] = int((datetime.now(UTC) - timedelta(minutes=20)).timestamp())
        claims["exp"] = int((datetime.now(UTC) - timedelta(minutes=5)).timestamp())
    token = jose_jwt.encode(claims, _JWT_SECRET, algorithm="HS256")

    with pytest.raises(PermissionDeniedError):
        auth_service.verify_access_token(token)


def test_verify_access_token_rejects_bad_signature(monkeypatch) -> None:
    monkeypatch.setattr(auth_service, "get_settings", _settings)
    claims = {
        "sub": str(uuid.uuid4()),
        "organization_id": str(uuid.uuid4()),
        "type": "access",
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    token = jose_jwt.encode(claims, "another-signing-key-with-at-least-32-bytes", algorithm="HS256")

    with pytest.raises(PermissionDeniedError):
        auth_service.verify_access_token(token)


@pytest.mark.asyncio
async def test_refresh_rotates_token_in_same_family(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    row = _RefreshRow(organization_id=organization_id)
    call_order: list[str] = []

    async def fake_resolve_org(session, token_hash):
        assert token_hash == hash_opaque_token("old-refresh-token")
        call_order.append("resolve_org")
        return organization_id

    async def fake_set_tenant(session, org_id):
        call_order.append("set_tenant")
        assert org_id == organization_id

    async def fake_get_row(session, token_hash):
        call_order.append("read_row")
        return row

    async def fake_revoke(session, row_id, *, revoked_at):
        call_order.append("revoke_old")
        assert row_id == row.id
        row.revoked_at = revoked_at

    async def fake_issue(session, *, user_id, organization_id, family_id):
        call_order.append("issue_new")
        assert user_id == row.user_id
        assert organization_id == row.organization_id
        assert family_id == row.family_id
        return "new-session"

    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant)
    monkeypatch.setattr(
        auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve_org
    )
    monkeypatch.setattr(auth_service.repository, "get_refresh_token_by_hash", fake_get_row)
    monkeypatch.setattr(auth_service.repository, "revoke_refresh_token", fake_revoke)
    monkeypatch.setattr(auth_service, "_issue_session", fake_issue)

    result = await auth_service.refresh(None, RefreshRequest(refresh_token="old-refresh-token"))

    assert result == "new-session"
    assert row.revoked_at is not None
    assert call_order == ["resolve_org", "set_tenant", "read_row", "revoke_old", "issue_new"]


@pytest.mark.asyncio
async def test_refresh_replay_revokes_token_family(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    row = _RefreshRow(organization_id=organization_id, revoked_at=datetime.now(UTC))
    revoked_families: list[uuid.UUID] = []

    async def fake_resolve_org(session, token_hash):
        return organization_id

    async def fake_set_tenant(session, org_id):
        assert org_id == organization_id

    async def fake_get_row(session, token_hash):
        return row

    async def fake_revoke_family(session, family_id, *, revoked_at):
        revoked_families.append(family_id)
        return 1

    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant)
    monkeypatch.setattr(
        auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve_org
    )
    monkeypatch.setattr(auth_service.repository, "get_refresh_token_by_hash", fake_get_row)
    monkeypatch.setattr(auth_service.repository, "revoke_family", fake_revoke_family)

    with pytest.raises(PermissionDeniedError, match="session has been revoked"):
        await auth_service.refresh(None, RefreshRequest(refresh_token="replayed-token"))
    assert revoked_families == [row.family_id]


@pytest.mark.asyncio
async def test_refresh_rejects_expired_and_unknown_tokens(monkeypatch) -> None:
    organization_id = uuid.uuid4()
    expired_row = _RefreshRow(
        organization_id=organization_id,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    async def fake_resolve_org(session, token_hash):
        return organization_id if token_hash == hash_opaque_token("expired-token") else None

    async def fake_set_tenant(session, org_id):
        return None

    async def fake_get_row(session, token_hash):
        return expired_row

    monkeypatch.setattr(auth_service, "set_tenant_context", fake_set_tenant)
    monkeypatch.setattr(
        auth_service.repository, "resolve_refresh_token_organization_id", fake_resolve_org
    )
    monkeypatch.setattr(auth_service.repository, "get_refresh_token_by_hash", fake_get_row)

    with pytest.raises(PermissionDeniedError, match="expired"):
        await auth_service.refresh(None, RefreshRequest(refresh_token="expired-token"))
    with pytest.raises(PermissionDeniedError, match="Invalid refresh token"):
        await auth_service.refresh(None, RefreshRequest(refresh_token="unknown-token"))


@pytest.mark.asyncio
async def test_sso_provisioning_denial_is_owned_by_tenancy_even_if_email_exists(
    monkeypatch,
) -> None:
    """An existing account/email alone must not make SSO provisioning allowed."""
    organization_id = uuid.uuid4()
    idp_claims = VerifiedIdPClaims(sub="subject", email="existing@example.test")
    user_creation_calls: list[str] = []
    role_calls: list[dict[str, object]] = []

    async def fake_get_mapping(session, org_id, subject):
        return None

    async def fake_evaluate(session, *, organization_id, email, groups):
        assert email == "existing@example.test"
        assert groups == ()
        return ProvisioningDecision(allowed=False, reason="no_matching_policy")

    async def fake_get_or_create_user(*args, **kwargs):
        user_creation_calls.append("get_or_create")
        return uuid.uuid4()

    async def fake_assign_role(*args, **kwargs):
        role_calls.append(kwargs)

    monkeypatch.setattr(auth_service.repository, "get_external_identity_mapping", fake_get_mapping)
    monkeypatch.setattr(auth_service.tenancy_service, "evaluate_provisioning", fake_evaluate)
    monkeypatch.setattr(auth_service.users_service, "get_or_create_user", fake_get_or_create_user)
    monkeypatch.setattr(auth_service.users_service, "assign_role", fake_assign_role)

    with pytest.raises(PermissionDeniedError, match="has not been provisioned"):
        await auth_service._resolve_or_provision_user(
            None,
            organization_id=organization_id,
            idp_subject=idp_claims.sub,
            idp_claims=idp_claims,
        )
    assert user_creation_calls == []
    assert role_calls == []


@pytest.mark.asyncio
async def test_sso_provisioning_assigns_role_from_tenancy_decision(monkeypatch) -> None:
    organization_id, user_id, role_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    invitation_id = uuid.uuid4()
    idp_claims = VerifiedIdPClaims(sub="subject", email="new@example.test", groups=("Engineering",))
    calls: list[tuple[str, object]] = []

    async def fake_get_mapping(session, org_id, subject):
        return None

    async def fake_evaluate(session, *, organization_id, email, groups):
        assert groups == ("Engineering",)
        return ProvisioningDecision(
            allowed=True,
            grants_role_id=role_id,
            matched_invitation_id=invitation_id,
            reason="invitation_match",
        )

    async def fake_get_or_create_user(session, *, email, display_name):
        calls.append(("create", email))
        return user_id

    async def fake_assign_role(session, *, user_id, organization_id, role_id):
        calls.append(("role", role_id))

    async def fake_accept_invitation(session, passed_invitation_id):
        calls.append(("invitation", passed_invitation_id))

    async def fake_insert_mapping(session, *, organization_id, user_id, idp_subject):
        calls.append(("mapping", idp_subject))

    monkeypatch.setattr(auth_service.repository, "get_external_identity_mapping", fake_get_mapping)
    monkeypatch.setattr(auth_service.tenancy_service, "evaluate_provisioning", fake_evaluate)
    monkeypatch.setattr(auth_service.users_service, "get_or_create_user", fake_get_or_create_user)
    monkeypatch.setattr(auth_service.users_service, "assign_role", fake_assign_role)
    monkeypatch.setattr(auth_service.tenancy_service, "accept_invitation", fake_accept_invitation)
    monkeypatch.setattr(
        auth_service.repository, "insert_external_identity_mapping", fake_insert_mapping
    )

    result = await auth_service._resolve_or_provision_user(
        None, organization_id=organization_id, idp_subject=idp_claims.sub, idp_claims=idp_claims
    )

    assert result == user_id
    assert calls == [
        ("create", "new@example.test"),
        ("role", role_id),
        ("invitation", invitation_id),
        ("mapping", "subject"),
    ]


@pytest.mark.asyncio
async def test_sso_provisioning_reuses_existing_external_identity_mapping(monkeypatch) -> None:
    user_id = uuid.uuid4()

    async def fake_get_mapping(session, org_id, subject):
        return SimpleNamespace(user_id=user_id)

    async def should_not_evaluate(*args, **kwargs):
        raise AssertionError("mapped identities should not be provisioned again")

    monkeypatch.setattr(auth_service.repository, "get_external_identity_mapping", fake_get_mapping)
    monkeypatch.setattr(auth_service.tenancy_service, "evaluate_provisioning", should_not_evaluate)

    result = await auth_service._resolve_or_provision_user(
        None,
        organization_id=uuid.uuid4(),
        idp_subject="existing-subject",
        idp_claims=VerifiedIdPClaims(sub="existing-subject", email="user@example.test"),
    )

    assert result == user_id
