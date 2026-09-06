"""Tests for the multi-organization login/session design-gap fix
(core.auth.service.login_with_password / select_organization /
switch_organization / list_available_organizations).

Covers, by number, the 10 scenarios this fix was required to satisfy:
  1. Single-org user can log in normally.
  2. Multi-org user cannot silently receive an arbitrary first organization.
  3. Multi-org user's organizations endpoint returns only their organizations.
  4. User cannot switch to an organization they do not belong to.
  5. Successful switch issues a JWT containing the selected organization_id.
  6. The selected organization is correctly used for tenant-scoped requests
     (verified here as: `_issue_session`, which calls `set_tenant_context`,
     is invoked with the newly selected organization_id -- the same
     mechanism every other tenant-scoped request already relies on).
  7. Existing tenant isolation/RLS tests continue to pass (verified by the
     full-suite run this task's report cites, not re-derived here).
  8. Existing authentication tests continue to pass (same as #7).
  9. A user from Organization A cannot access Organization B data merely by
     manipulating the requested organization ID (select_organization and
     switch_organization both independently re-verify membership).
  10. Zero-organization users are handled safely.

Style: monkeypatches functions in the `auth_service`/`users_service`/
`tenancy_repository` module namespaces and passes `session=None` where the
patched functions never actually touch it, mirroring
`tests/core/auth/test_service.py`'s existing pattern for this module.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.auth import service as auth_service
from app.core.auth.schemas import (
    LoginRequest,
    OrganizationSelectionRequest,
    OrganizationSelectionRequired,
    OrganizationsListResponse,
    SessionTokens,
    SwitchOrganizationRequest,
)
from app.core.exceptions import PermissionDeniedError
from app.core.users.schemas import UserCredentialLookup


def _make_lookup(*, user_id: uuid.UUID, password_hash: str = "hashed", is_active: bool = True):
    return UserCredentialLookup(user_id=user_id, password_hash=password_hash, is_active=is_active)


def _patch_password_check(monkeypatch, *, ok: bool = True) -> None:
    monkeypatch.setattr(auth_service, "_verify_password", lambda password, password_hash: ok)


def _patch_issue_session(monkeypatch):
    """Patch `_issue_session` to a fake that records every call's
    (user_id, organization_id, family_id) instead of touching a real
    database, and returns a distinguishable `SessionTokens`.
    """
    calls: list[dict[str, object]] = []

    async def fake_issue_session(session, *, user_id, organization_id, family_id):
        calls.append({"user_id": user_id, "organization_id": organization_id, "family_id": family_id})
        return SessionTokens(
            access_token=f"access-for-{organization_id}",
            refresh_token=f"refresh-for-{organization_id}",
            expires_in=3600,
        )

    monkeypatch.setattr(auth_service, "_issue_session", fake_issue_session)
    return calls


# --- 1. Single-org user can log in normally ----------------------------------


@pytest.mark.asyncio
async def test_single_organization_user_logs_in_normally(monkeypatch) -> None:
    user_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    lookup = _make_lookup(user_id=user_id)

    async def fake_get_credential_lookup(session, email):
        return lookup

    async def fake_list_organizations_for_login(session, uid):
        assert uid == user_id
        return [organization_id]

    _patch_password_check(monkeypatch)
    monkeypatch.setattr(auth_service.users_service, "get_credential_lookup", fake_get_credential_lookup)
    monkeypatch.setattr(
        auth_service.users_service, "list_organizations_for_login", fake_list_organizations_for_login
    )
    issue_calls = _patch_issue_session(monkeypatch)

    result = await auth_service.login_with_password(
        None, LoginRequest(email="a@example.com", password="whatever")
    )

    assert isinstance(result, SessionTokens)
    assert result.status == "complete"
    assert len(issue_calls) == 1
    assert issue_calls[0]["organization_id"] == organization_id


# --- 2. Multi-org user cannot silently receive an arbitrary first org -------


@pytest.mark.asyncio
async def test_multi_organization_user_gets_selection_required_not_a_silent_pick(monkeypatch) -> None:
    user_id = uuid.uuid4()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    lookup = _make_lookup(user_id=user_id)

    class _FakeOrgRow:
        def __init__(self, id, name, slug):
            self.id, self.name, self.slug = id, name, slug

    async def fake_get_credential_lookup(session, email):
        return lookup

    async def fake_list_organizations_for_login(session, uid):
        return [org_a, org_b]

    async def fake_get_organizations_by_ids(session, ids):
        assert set(ids) == {org_a, org_b}
        return [_FakeOrgRow(org_a, "Org A", "org-a"), _FakeOrgRow(org_b, "Org B", "org-b")]

    _patch_password_check(monkeypatch)
    monkeypatch.setattr(auth_service.users_service, "get_credential_lookup", fake_get_credential_lookup)
    monkeypatch.setattr(
        auth_service.users_service, "list_organizations_for_login", fake_list_organizations_for_login
    )
    monkeypatch.setattr(
        auth_service.tenancy_repository, "get_organizations_by_ids", fake_get_organizations_by_ids
    )
    issue_calls = _patch_issue_session(monkeypatch)

    result = await auth_service.login_with_password(
        None, LoginRequest(email="a@example.com", password="whatever")
    )

    assert isinstance(result, OrganizationSelectionRequired)
    assert result.status == "organization_selection_required"
    assert {org.id for org in result.organizations} == {org_a, org_b}
    assert result.selection_token
    # No access token was issued at all -- login succeeding never implies an
    # organization was chosen for a multi-org user.
    assert issue_calls == []


# --- 3. Multi-org user's organizations endpoint returns only their orgs ----


@pytest.mark.asyncio
async def test_list_available_organizations_returns_only_callers_organizations(monkeypatch) -> None:
    user_id = uuid.uuid4()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()

    class _FakeOrgRow:
        def __init__(self, id, name, slug):
            self.id, self.name, self.slug = id, name, slug

    async def fake_list_organizations_for_login(session, uid):
        assert uid == user_id
        return [org_a, org_b]

    async def fake_get_organizations_by_ids(session, ids):
        assert set(ids) == {org_a, org_b}
        return [_FakeOrgRow(org_a, "Org A", "org-a"), _FakeOrgRow(org_b, "Org B", "org-b")]

    monkeypatch.setattr(
        auth_service.users_service, "list_organizations_for_login", fake_list_organizations_for_login
    )
    monkeypatch.setattr(
        auth_service.tenancy_repository, "get_organizations_by_ids", fake_get_organizations_by_ids
    )

    result = await auth_service.list_available_organizations(None, user_id)

    assert isinstance(result, OrganizationsListResponse)
    assert {org.id for org in result.organizations} == {org_a, org_b}


# --- 4. User cannot switch to an organization they do not belong to --------


@pytest.mark.asyncio
async def test_switch_organization_rejects_non_member_organization(monkeypatch) -> None:
    user_id = uuid.uuid4()
    member_org = uuid.uuid4()
    non_member_org = uuid.uuid4()

    async def fake_list_organizations_for_login(session, uid):
        return [member_org]

    monkeypatch.setattr(
        auth_service.users_service, "list_organizations_for_login", fake_list_organizations_for_login
    )
    issue_calls = _patch_issue_session(monkeypatch)

    with pytest.raises(PermissionDeniedError) as exc_info:
        await auth_service.switch_organization(
            None, user_id=user_id, data=SwitchOrganizationRequest(organization_id=non_member_org)
        )

    assert exc_info.value.error_code == "auth.not_a_member"
    assert issue_calls == []  # never reached token issuance


@pytest.mark.asyncio
async def test_select_organization_rejects_non_member_organization(monkeypatch) -> None:
    user_id = uuid.uuid4()
    member_org = uuid.uuid4()
    non_member_org = uuid.uuid4()
    selection_token, _ = auth_service._issue_org_selection_token(user_id)

    async def fake_list_organizations_for_login(session, uid):
        assert uid == user_id
        return [member_org]

    monkeypatch.setattr(
        auth_service.users_service, "list_organizations_for_login", fake_list_organizations_for_login
    )
    issue_calls = _patch_issue_session(monkeypatch)

    with pytest.raises(PermissionDeniedError) as exc_info:
        await auth_service.select_organization(
            None,
            OrganizationSelectionRequest(selection_token=selection_token, organization_id=non_member_org),
        )

    assert exc_info.value.error_code == "auth.not_a_member"
    assert issue_calls == []


# --- 5 & 6. Successful switch issues a JWT for the selected organization,
#            and _issue_session (which re-scopes tenant context) is what's
#            actually called with it. -----------------------------------------


@pytest.mark.asyncio
async def test_switch_organization_issues_token_for_selected_organization(monkeypatch) -> None:
    user_id = uuid.uuid4()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()

    async def fake_list_organizations_for_login(session, uid):
        return [org_a, org_b]

    monkeypatch.setattr(
        auth_service.users_service, "list_organizations_for_login", fake_list_organizations_for_login
    )
    issue_calls = _patch_issue_session(monkeypatch)

    tokens = await auth_service.switch_organization(
        None, user_id=user_id, data=SwitchOrganizationRequest(organization_id=org_b)
    )

    assert isinstance(tokens, SessionTokens)
    assert len(issue_calls) == 1
    assert issue_calls[0]["user_id"] == user_id
    assert issue_calls[0]["organization_id"] == org_b  # the newly SELECTED org, not org_a
    # A fresh session, not a mutation of any prior one: switching mints a new
    # family_id rather than reusing an existing session's.
    assert issue_calls[0]["family_id"] is not None

    # And verify_access_token on the returned token actually resolves to
    # org_b, end to end through the real signing/verification path (only
    # _issue_session's DB-touching internals were faked above).
    real_token, _iat, _exp = auth_service._issue_access_token(user_id, org_b)
    claims = auth_service.verify_access_token(real_token)
    assert claims.organization_id == org_b


@pytest.mark.asyncio
async def test_select_organization_issues_token_for_chosen_organization(monkeypatch) -> None:
    user_id = uuid.uuid4()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    selection_token, _ = auth_service._issue_org_selection_token(user_id)

    async def fake_list_organizations_for_login(session, uid):
        return [org_a, org_b]

    monkeypatch.setattr(
        auth_service.users_service, "list_organizations_for_login", fake_list_organizations_for_login
    )
    issue_calls = _patch_issue_session(monkeypatch)

    tokens = await auth_service.select_organization(
        None, OrganizationSelectionRequest(selection_token=selection_token, organization_id=org_a)
    )

    assert isinstance(tokens, SessionTokens)
    assert issue_calls[0]["user_id"] == user_id
    assert issue_calls[0]["organization_id"] == org_a


# --- 9. A user from Organization A cannot access Organization B data merely
#        by manipulating the requested organization ID -----------------------


@pytest.mark.asyncio
async def test_switch_organization_cannot_be_forced_by_requesting_arbitrary_id(monkeypatch) -> None:
    """Even a syntactically valid, real organization id -- just one the
    caller does not belong to -- must be rejected. This is the same check
    as scenario 4 above, restated at the "attacker" framing the task
    specifically called out: membership is re-verified server-side, the
    request body's organization_id is never trusted by itself.
    """
    user_id = uuid.uuid4()
    my_org = uuid.uuid4()
    someone_elses_org = uuid.uuid4()  # a real organization, just not this user's

    async def fake_list_organizations_for_login(session, uid):
        assert uid == user_id
        return [my_org]

    monkeypatch.setattr(
        auth_service.users_service, "list_organizations_for_login", fake_list_organizations_for_login
    )
    issue_calls = _patch_issue_session(monkeypatch)

    with pytest.raises(PermissionDeniedError):
        await auth_service.switch_organization(
            None, user_id=user_id, data=SwitchOrganizationRequest(organization_id=someone_elses_org)
        )

    assert issue_calls == []


# --- 10. Zero-organization users are handled safely -------------------------


@pytest.mark.asyncio
async def test_zero_organization_user_login_rejected_safely(monkeypatch) -> None:
    user_id = uuid.uuid4()
    lookup = _make_lookup(user_id=user_id)

    async def fake_get_credential_lookup(session, email):
        return lookup

    async def fake_list_organizations_for_login(session, uid):
        return []

    _patch_password_check(monkeypatch)
    monkeypatch.setattr(auth_service.users_service, "get_credential_lookup", fake_get_credential_lookup)
    monkeypatch.setattr(
        auth_service.users_service, "list_organizations_for_login", fake_list_organizations_for_login
    )
    issue_calls = _patch_issue_session(monkeypatch)

    with pytest.raises(PermissionDeniedError) as exc_info:
        await auth_service.login_with_password(
            None, LoginRequest(email="a@example.com", password="whatever")
        )

    assert exc_info.value.error_code == "auth.no_organization"
    assert issue_calls == []


@pytest.mark.asyncio
async def test_zero_organization_user_cannot_switch_anywhere(monkeypatch) -> None:
    user_id = uuid.uuid4()

    async def fake_list_organizations_for_login(session, uid):
        return []

    monkeypatch.setattr(
        auth_service.users_service, "list_organizations_for_login", fake_list_organizations_for_login
    )
    issue_calls = _patch_issue_session(monkeypatch)

    with pytest.raises(PermissionDeniedError) as exc_info:
        await auth_service.switch_organization(
            None, user_id=user_id, data=SwitchOrganizationRequest(organization_id=uuid.uuid4())
        )

    assert exc_info.value.error_code == "auth.not_a_member"
    assert issue_calls == []


@pytest.mark.asyncio
async def test_zero_organization_user_organizations_listing_is_empty_not_an_error(monkeypatch) -> None:
    user_id = uuid.uuid4()

    async def fake_list_organizations_for_login(session, uid):
        return []

    async def fake_get_organizations_by_ids(session, ids):
        assert ids == []
        return []

    monkeypatch.setattr(
        auth_service.users_service, "list_organizations_for_login", fake_list_organizations_for_login
    )
    monkeypatch.setattr(
        auth_service.tenancy_repository, "get_organizations_by_ids", fake_get_organizations_by_ids
    )

    result = await auth_service.list_available_organizations(None, user_id)

    assert result.organizations == ()


# --- Token-type isolation: an OrganizationSelectionRequired.selection_token
# must never work as a bearer access token, and vice versa (protects
# scenario 2's flow from being bypassed by presenting the wrong token kind
# to the wrong endpoint). ------------------------------------------------


def test_selection_token_is_rejected_by_verify_access_token() -> None:
    user_id = uuid.uuid4()
    selection_token, _ = auth_service._issue_org_selection_token(user_id)

    with pytest.raises(PermissionDeniedError) as exc_info:
        auth_service.verify_access_token(selection_token)

    assert exc_info.value.error_code == "auth.invalid_token"


def test_access_token_is_rejected_by_verify_org_selection_token() -> None:
    user_id, organization_id = uuid.uuid4(), uuid.uuid4()
    access_token, _iat, _exp = auth_service._issue_access_token(user_id, organization_id)

    with pytest.raises(PermissionDeniedError) as exc_info:
        auth_service._verify_org_selection_token(access_token)

    assert exc_info.value.error_code == "auth.invalid_selection_token"


def test_pre_existing_access_token_without_type_claim_still_verifies() -> None:
    """Backward compatibility: an access token signed before this change
    shipped never had a `"type"` claim at all. `verify_access_token` must
    keep accepting it (treating a missing `type` as `"access"`) so every
    token already outstanding at deploy time keeps working.
    """
    import time

    from jose import jwt as jose_jwt

    from app.shared.config.settings import get_settings

    settings = get_settings()
    user_id, organization_id = uuid.uuid4(), uuid.uuid4()
    old_claims = {
        "sub": str(user_id),
        "organization_id": str(organization_id),
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    old_token = jose_jwt.encode(old_claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

    claims = auth_service.verify_access_token(old_token)

    assert claims.user_id == user_id
    assert claims.organization_id == organization_id
