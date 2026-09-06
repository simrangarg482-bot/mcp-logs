"""Auth router -- SSO/PKCE login, refresh, logout, and identity lookup.

Owned by: app/api. Wraps core/auth/service.py's real OIDC Authorization
Code + PKCE flow. API_DESIGN.md section 1's `/auth/login`
(username+password exchange) / `/auth/refresh` / `/auth/me` table predates
that flow's implementation; `/auth/refresh` and `/auth/me` are preserved
as-is (they still match what core/auth exposes), but `/auth/login` becomes
a two-step redirect flow (`/auth/{org_slug}/login` then `/auth/callback`),
matching what core/auth actually implements rather than the older sketch.

PKCE note: `code_verifier` is generated server-side by `begin_sso_login` and
returned directly in `SSOAuthorizationRedirect` to the caller (a public
client, e.g. a browser SPA) -- per the OAuth2 PKCE spec for public clients,
it is the *caller's* job to stash it (e.g. sessionStorage keyed by `state`)
across the redirect round-trip and resupply it verbatim in
`SSOCallbackRequest`. This router does no server-side state->code_verifier
storage of its own; there is nothing to store, since the schema already
requires the caller to send it back.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import CurrentIdentity, DbSession
from app.api.rate_limit import rate_limit_by_ip
from app.core.audit.service import record_audit_event
from app.core.auth import service as auth_service
from app.core.auth.schemas import (
    LoginRequest,
    LoginResponse,
    LogoutAllResponse,
    OrganizationSelectionRequest,
    OrganizationsListResponse,
    RefreshRequest,
    SessionTokens,
    SignupRequest,
    SSOAuthorizationRedirect,
    SSOCallbackRequest,
    SwitchOrganizationRequest,
)
from app.core.exceptions import ValidationError
from app.core.users import service as users_service
from app.core.users.schemas import UserProfile

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/{org_slug}/login", response_model=SSOAuthorizationRedirect)
async def begin_login(
    org_slug: str, redirect_uri: str, session: DbSession
) -> SSOAuthorizationRedirect:
    """Start an SSO login. `redirect_uri` is supplied by the caller (its own
    callback URL) -- core/auth does not hardcode or guess it (see
    `begin_sso_login`'s docstring).
    """
    return await auth_service.begin_sso_login(session, org_slug, redirect_uri=redirect_uri)


@router.post("/callback", response_model=SessionTokens)
async def complete_login(
    data: SSOCallbackRequest, redirect_uri: str, session: DbSession
) -> SessionTokens:
    """Complete an SSO login. `redirect_uri` must be identical to the one
    used in `begin_login`, per the OAuth2 spec.
    """
    return await auth_service.complete_sso_login(session, data, redirect_uri=redirect_uri)


_SIGNUP_RATE_LIMIT = rate_limit_by_ip(scope="auth.signup", requests_per_minute=10)
# Phase 6.5: deliberately tighter than signup -- nothing previously bounded
# repeated login attempts at all, the exact gap credential-stuffing/brute-
# force attacks exploit. Per-IP (not per-email/per-user): the attacker
# controls which email they submit, so keying on the submitted email would
# let them spread attempts across guessed emails from one IP with no
# throttling at all -- IP is the one dimension the caller doesn't get to
# choose per request.
_LOGIN_RATE_LIMIT = rate_limit_by_ip(scope="auth.login", requests_per_minute=10)


@router.post(
    "/signup",
    response_model=SessionTokens,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_SIGNUP_RATE_LIMIT)],
)
async def signup(data: SignupRequest, session: DbSession) -> SessionTokens:
    """Self-service email/password account creation -- a parallel path
    alongside the SSO flow above, not a replacement for it. See
    `auth_service.signup`'s docstring for exactly what it does and does not
    support (always a brand-new organization; no join-existing-org flow).
    """
    return await auth_service.signup(session, data)


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(_LOGIN_RATE_LIMIT)])
async def login(data: LoginRequest, session: DbSession) -> LoginResponse:
    """Email/password login, counterpart to `signup`.

    Returns `SessionTokens` (`status: "complete"`) for a single-organization
    user, unchanged from before -- or `OrganizationSelectionRequired`
    (`status: "organization_selection_required"`) for a multi-organization
    one, which no longer gets silently logged into whichever organization
    came back first. See `auth_service.login_with_password`'s docstring.
    """
    return await auth_service.login_with_password(session, data)


@router.post(
    "/select-organization", response_model=SessionTokens, dependencies=[Depends(_LOGIN_RATE_LIMIT)]
)
async def select_organization(data: OrganizationSelectionRequest, session: DbSession) -> SessionTokens:
    """Complete a multi-organization login: exchange the `selection_token`
    an `OrganizationSelectionRequired` login response carried, plus the
    chosen `organization_id`, for real `SessionTokens`. Same rate limit as
    `/auth/login` -- this is still an unauthenticated, credential-adjacent
    endpoint (the selection_token stands in for the password check that
    already happened), so it gets the same per-IP throttling.
    """
    return await auth_service.select_organization(session, data)


@router.post("/switch-organization", response_model=SessionTokens)
async def switch_organization(
    data: SwitchOrganizationRequest, actor: CurrentIdentity, session: DbSession
) -> SessionTokens:
    """Re-scope an already-authenticated session to a different organization
    the caller also belongs to. `actor.user_id` comes from the caller's own
    verified access token (`CurrentIdentity`), never from the request body --
    `auth_service.switch_organization` independently re-verifies membership
    in `data.organization_id` before issuing anything.
    """
    if actor.user_id is None:
        raise ValidationError(
            "Only a user identity can switch organizations.", error_code="user.no_profile"
        )
    return await auth_service.switch_organization(session, user_id=actor.user_id, data=data)


@router.get("/organizations", response_model=OrganizationsListResponse)
async def list_my_organizations(actor: CurrentIdentity, session: DbSession) -> OrganizationsListResponse:
    """List every organization the current caller belongs to -- derived from
    `actor.user_id` (the verified caller's own identity), never a
    client-supplied user id, so this can only ever answer "which
    organizations am I a member of."
    """
    if actor.user_id is None:
        raise ValidationError(
            "Only a user identity has organization memberships.", error_code="user.no_profile"
        )
    return await auth_service.list_available_organizations(session, actor.user_id)


@router.post("/refresh", response_model=SessionTokens)
async def refresh_session(data: RefreshRequest, session: DbSession) -> SessionTokens:
    return await auth_service.refresh(session, data)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_session(data: RefreshRequest, session: DbSession) -> Response:
    await auth_service.logout(session, data)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", response_model=LogoutAllResponse)
async def logout_all_sessions(actor: CurrentIdentity, session: DbSession) -> LogoutAllResponse:
    """"Logout everywhere" -- revoke every one of the caller's own sessions
    (`core.auth.service.revoke_all_sessions`) within their own organization.

    Requires `actor.user_id` -- like `GET /auth/me`, a service/agent identity
    has no sessions of its own to revoke, so it gets a clean `ValidationError`
    rather than calling `revoke_all_sessions` with a nonsensical id. See
    `LogoutAllResponse`'s own docstring for exactly what "logged out
    everywhere" does and does not guarantee about a still-live access token.
    """
    if actor.user_id is None:
        raise ValidationError(
            "Only a user identity has sessions to revoke.", error_code="user.no_profile"
        )
    revoked_count = await auth_service.revoke_all_sessions(
        session, actor.user_id, actor.organization_id
    )
    await record_audit_event(
        session,
        actor,
        action="user.logout_all_sessions",
        resource_type="user",
        resource_id=actor.user_id,
        metadata={"revoked_session_count": revoked_count},
    )
    return LogoutAllResponse(
        message="Successfully logged out from all sessions",
        revoked_session_count=revoked_count,
    )


@router.get("/me", response_model=UserProfile)
async def get_me(actor: CurrentIdentity, session: DbSession) -> UserProfile:
    """Resolve the current identity's full profile (API_DESIGN.md:
    `GET /auth/me`). Requires `actor.user_id` -- a service/agent identity
    calling this (there is no legitimate reason one would, since only a
    human logs in via this router) gets a clean `ValidationError` rather
    than an obscure attribute failure inside `get_user_profile`.
    """
    if actor.user_id is None:
        raise ValidationError(
            "Only a user identity has a profile.", error_code="user.no_profile"
        )
    return await users_service.get_user_profile(session, actor.user_id, actor.organization_id)
