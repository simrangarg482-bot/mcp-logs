"""Public interface for core/auth -- SSO login, session issuance/verification,
refresh rotation (PROJECT_PLAN.md section 3.3-3.4, section 9.1).

Owned by: core/auth. Depends on core/tenancy (to resolve an organization's SSO
configuration) and core/users (to resolve/create the mapped user), per
PROJECT_PLAN.md section 9.1 -- the same cross-submodule dependency pattern
already used by core/tenancy/service.py (core/users, core/audit).

Status of this file, stated plainly rather than left to be discovered later:
`_discover_authorization_endpoint` and `_exchange_code_for_claims` are now
real implementations -- OIDC discovery-document fetching (cached in-process),
a real token-endpoint exchange via `httpx`, and ID token signature
verification against the issuer's JWKS via `python-jose`, matching a
key by its `kid` header. This has NOT been run against a live IdP
(Entra ID/Okta/Auth0/Google Workspace) -- there is no registered test
application available to verify it end-to-end, so treat this as
spec-correct-by-inspection, not battle-tested, until it's actually exercised
against a real provider.

`_resolve_client_secret` now calls `shared/security`'s envelope-encryption
helper (PROJECT_PLAN.md section 12.5) to decrypt `SSOConfiguration.
client_secret_ref`, matching `core.tenancy.service.configure_sso`'s
encrypt-at-write half of the same split -- this was a real, live gap until
fixed (the module existed and was already used by `register_connector`/
`mcp_oauth`, `core/auth` just hadn't been updated to use it), not a design
decision left open.

Group-claim extraction (`_exchange_code_for_claims`'s `groups` field) checks
the standard `groups` claim, which Entra ID and Okta commonly populate when
configured to. Auth0 typically uses a custom namespaced claim instead, and
Google Workspace does not expose group membership via ID token claims at all
(it requires a separate Admin SDK call) -- group-based provisioning rules
(`core/tenancy`'s `rule_type="group"`) will work as-is for Entra ID/Okta-style
claims and will simply never match for providers that don't send a `groups`
claim in this shape. This is a known, flagged limitation, not a guarantee of
uniform four-provider group support.

Just-in-time provisioning (PROJECT_PLAN.md section 3.3, step 6) is now
delegated, not guessed at here: `_resolve_or_provision_user` asks
core/tenancy's `evaluate_provisioning` whether a login is allowed to
provision an account (and which role it should get), then asks core/users to
actually create/resolve the user and grant that role. This supersedes the
previous stopgap ("a `users` row with this email already exists" = invited),
per the SSO-provisioning-policy design recorded in ENGINEERING_DECISIONS.md:
core/auth verifies authentication and extracts identity claims; it does not
decide who is allowed to join an organization.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

import bcrypt
import httpx
from jose import JWTError, jwt as jose_jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import repository
from app.core.auth.schemas import (
    LoginRequest,
    LoginResponse,
    OrganizationSelectionRequest,
    OrganizationSelectionRequired,
    OrganizationsListResponse,
    OrganizationSummary,
    RefreshRequest,
    SessionTokens,
    SignupRequest,
    SSOAuthorizationRedirect,
    SSOCallbackRequest,
    SwitchOrganizationRequest,
    TokenClaims,
    VerifiedIdPClaims,
)
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.tenancy import repository as tenancy_repository
from app.core.tenancy import service as tenancy_service
from app.core.tenancy.schemas import InvitationAcceptRequest, OrganizationCreate, SSOConfiguration
from app.core.users import service as users_service
from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.config.settings import get_settings
from app.shared.security import decrypt_secret, get_kms, hash_opaque_token

def _hash_password(password: str) -> str:
    """Hash a plaintext password for `users.password_hash` (email/password
    auth path, `signup`/`login_with_password`).

    Calls the `bcrypt` package directly rather than through `passlib`
    (`passlib[bcrypt]` is still the declared dependency, pyproject.toml):
    the installed `bcrypt` (5.x) removed the `__about__` attribute
    `passlib==1.7.4`'s bcrypt-backend detection reads to calibrate itself,
    which makes passlib's own self-test raise before ever hashing anything
    -- a real, verified version incompatibility between those two packages'
    currently-resolved versions, not something specific to this code. Since
    `bcrypt` itself works correctly standalone, calling it directly avoids
    the broken shim instead of pinning an older `bcrypt` to route around it.

    bcrypt truncates its input at 72 bytes -- a property of the algorithm
    itself, not a limitation added here; `SignupRequest.password`'s
    `min_length=8` bounds the other end, but no maximum is enforced, so a
    password longer than 72 bytes still hashes successfully, it just has
    every byte past the 72nd silently ignored by the algorithm.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def _verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a hash `_hash_password` produced.
    `bcrypt.checkpw` is constant-time; see `login_with_password`'s docstring
    for what this function's result is and isn't safe to branch on.
    """
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))

logger = get_logger(__name__)

# Refresh-token lifetime is not yet a Settings field (only jwt_expiry_minutes,
# for the access token, exists there today) -- a reasonable default is used
# here, called out as a natural small follow-up addition to
# shared/config/settings.py rather than made here, to keep this migration to
# one file's concern.
_REFRESH_TOKEN_LIFETIME = timedelta(days=30)

# In-process cache of each issuer's OIDC discovery document, keyed by
# issuer_url. A discovery document changes rarely, so refetching it on every
# single login would add an avoidable round trip; the TTL just bounds how
# long a provider-side change (e.g. a key rotation reflected in a new
# jwks_uri) takes to be picked up. NOTE: per-process only -- if core/auth
# ever runs across multiple worker processes, each keeps its own copy. A
# shared cache (Redis, already a dependency per ENGINEERING_DECISIONS.md
# #002) would avoid duplicate fetches across processes; not wired up here to
# keep this change scoped to the OIDC adapter itself, not a caching redesign.
_DISCOVERY_CACHE_TTL = timedelta(hours=1)
_discovery_cache: dict[str, tuple[dict, datetime]] = {}


# --- SSO login: begin -----------------------------------------------------------


def _assert_redirect_uri_allowed(redirect_uri: str) -> None:
    """Reject a `redirect_uri` whose origin isn't in `cors_allowed_origins`
    -- the same trusted-frontend-origin allowlist already governing which
    origins may call this API from a browser (PROJECT_PLAN.md's CORS
    config, `Settings.cors_allowed_origins`).

    Phase 3 production-hardening addition, flagged by a security audit:
    both `/auth/{org_slug}/login` and `/auth/callback` previously accepted
    `redirect_uri` from the caller with no server-side check at all. OAuth's
    own security model relies on the IdP rejecting a `redirect_uri` that
    wasn't pre-registered for this client -- real protection, but not a
    substitute for this app also checking it, since a client registration
    permitting multiple origins (e.g. staging + production, or several
    historical frontend deployments) would otherwise let a caller redirect a
    completed login to any one of them, not just the origin the user
    actually started from. Checked before `begin_sso_login` ever contacts
    the IdP and before `complete_sso_login` ever exchanges a code, so an
    unrecognized `redirect_uri` never gets that far either way.
    """
    settings = get_settings()
    parsed = urlparse(redirect_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in settings.cors_allowed_origins:
        raise PermissionDeniedError(
            "redirect_uri is not an allowed origin for this deployment.",
            error_code="auth.redirect_uri_not_allowed",
            detail={"redirect_uri": redirect_uri},
        )


async def begin_sso_login(
    session: AsyncSession, org_slug: str, *, redirect_uri: str
) -> SSOAuthorizationRedirect:
    """Start an SSO login for the organization identified by `org_slug`.

    `redirect_uri` is supplied by the caller (the future api/ layer), which
    knows its own base URL from the incoming request -- core/auth does not
    hardcode or guess it, so the same code works regardless of deployment
    hostname. Its origin must still be one of `cors_allowed_origins` (see
    `_assert_redirect_uri_allowed`). Raises NotFoundError (propagated from
    `tenancy_service.get_organization_sso_config`) if the slug or its SSO
    configuration doesn't exist.
    """
    _assert_redirect_uri_allowed(redirect_uri)
    sso_config = await tenancy_service.get_organization_sso_config(session, org_slug)
    authorization_endpoint = await _discover_authorization_endpoint(sso_config.issuer_url)

    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _pkce_challenge(code_verifier)
    state = secrets.token_urlsafe(32)

    authorization_url = _build_authorization_url(
        authorization_endpoint,
        client_id=sso_config.client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
    )
    return SSOAuthorizationRedirect(
        authorization_url=authorization_url, state=state, code_verifier=code_verifier
    )


def _pkce_challenge(code_verifier: str) -> str:
    """Derive the S256 PKCE code_challenge from a code_verifier (RFC 7636)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _build_authorization_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
) -> str:
    """Build the IdP authorization URL. Pure string construction -- no
    network call, unlike the two OIDC seams below.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{authorization_endpoint}?{urlencode(params)}"


async def _get_discovery_document(issuer_url: str) -> dict:
    """Fetch (and cache) an issuer's OIDC discovery document.

    All four supported providers publish one at
    `{issuer_url}/.well-known/openid-configuration`, which is exactly why one
    code path can handle all four (PROJECT_PLAN.md section 3.3) -- this
    function doesn't special-case any provider.
    """
    cached = _discovery_cache.get(issuer_url)
    if cached is not None:
        document, cached_at = cached
        if datetime.now(timezone.utc) - cached_at < _DISCOVERY_CACHE_TTL:
            return document

    discovery_url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(discovery_url)
        response.raise_for_status()
        document = response.json()

    _discovery_cache[issuer_url] = (document, datetime.now(timezone.utc))
    return document


async def _discover_authorization_endpoint(issuer_url: str) -> str:
    """Resolve `issuer_url`'s OIDC `authorization_endpoint` via its
    (cached) discovery document.
    """
    document = await _get_discovery_document(issuer_url)
    return document["authorization_endpoint"]


# --- SSO login: complete --------------------------------------------------------


async def complete_sso_login(
    session: AsyncSession, data: SSOCallbackRequest, *, redirect_uri: str
) -> SessionTokens:
    """Complete an SSO login after the IdP has redirected back with a code.

    Matching `data.state` against the value issued by `begin_sso_login` is
    expected to already have happened in the caller (the api/ layer looks up
    the stashed `code_verifier` by `state` before ever calling this function
    -- if the state doesn't match anything stashed, there is nothing to look
    up, and the caller rejects before reaching here). `redirect_uri` must be
    identical to the one used in `begin_sso_login`, per the OAuth2 spec, and
    (like `begin_sso_login`) its origin must be one of `cors_allowed_origins`
    -- see `_assert_redirect_uri_allowed`.
    """
    _assert_redirect_uri_allowed(redirect_uri)
    sso_config = await tenancy_service.get_organization_sso_config(session, data.org_slug)

    idp_claims = await _exchange_code_for_claims(
        sso_config,
        code=data.code,
        code_verifier=data.code_verifier,
        redirect_uri=redirect_uri,
    )

    user_id = await _resolve_or_provision_user(
        session,
        organization_id=sso_config.organization_id,
        idp_subject=idp_claims.sub,
        idp_claims=idp_claims,
    )

    family_id = uuid.uuid4()
    tokens = await _issue_session(
        session, user_id=user_id, organization_id=sso_config.organization_id, family_id=family_id
    )
    logger.info(
        "sso_login_completed",
        user_id=str(user_id),
        organization_id=str(sso_config.organization_id),
    )
    return tokens


# --- Email/password auth: signup + login -----------------------------------------
#
# A second, parallel authentication mechanism alongside SSO -- not a
# replacement for it, and not built by weakening it. Every function above
# this section is untouched. This exists because EKIP's only login path
# until now required a real OIDC identity provider already configured for an
# organization (`sso_configurations`); a self-service user with no IdP of
# their own has had no way to create an account at all. Both functions below
# end by calling the exact same `_issue_session` every SSO login already
# uses, so a password-authenticated session is indistinguishable, to every
# downstream check (REST, MCP, RLS), from an SSO-authenticated one.


async def signup(session: AsyncSession, data: SignupRequest) -> SessionTokens:
    """Create a brand-new organization and its first (admin) user, and log
    them in.

    There is no "join an existing organization via signup" flow -- every
    signup creates a fresh `organizations` row (reusing `core.tenancy.
    service.create_organization`, the same function SSO auto-provisioning
    and the dev bootstrap scripts already call) together with a "General"
    default project. A second person joining that organization later is a
    separate, already-built flow (`core.tenancy.service.create_invitation`/
    `accept_invitation`), not something this function does.

    Raises `ConflictError` if `data.email` already has an account (whether
    password- or SSO-provisioned -- `get_credential_lookup` doesn't
    distinguish, since "an account with this email exists" is true either
    way) or if `data.organization_slug` is already taken (surfaced by
    `create_organization` itself).
    """
    existing = await users_service.get_credential_lookup(session, data.email)
    if existing is not None:
        raise ConflictError(
            "An account with this email already exists.",
            error_code="auth.email_taken",
            detail={"email": data.email},
        )

    organization = await tenancy_service.create_organization(
        session,
        OrganizationCreate(name=data.organization_name, slug=data.organization_slug),
    )

    user_id = await users_service.get_or_create_user(
        session, email=data.email, display_name=data.display_name
    )
    await users_service.set_password(
        session, user_id=user_id, password_hash=_hash_password(data.password)
    )

    role_id = await users_service.ensure_admin_role(session)
    await users_service.assign_role(
        session, user_id=user_id, organization_id=organization.id, role_id=role_id
    )

    tokens = await _issue_session(
        session, user_id=user_id, organization_id=organization.id, family_id=uuid.uuid4()
    )
    logger.info(
        "password_signup_completed",
        user_id=str(user_id),
        organization_id=str(organization.id),
    )
    return tokens


async def login_with_password(session: AsyncSession, data: LoginRequest) -> LoginResponse:
    """Authenticate with an email + password (`signup`'s counterpart).

    A wrong password and an unknown/SSO-only (no password set) email both
    fail identically -- a generic "invalid credentials" `PermissionDeniedError`
    -- so this endpoint cannot be used to enumerate which emails have an
    account. `CryptContext.verify` itself is timing-safe; the branches above
    it are not constant-time relative to each other, but neither leaks
    anything beyond "invalid," which is the only fact either branch reveals.

    Multi-organization design-gap fix (previously: silently logged into
    whichever organization `resolve_organization_for_login`/
    `get_first_organization_id` happened to return -- both left completely
    unchanged; this now uses the new, additive `list_organizations_for_login`
    instead so it can actually tell "exactly one" apart from "more than
    one"):
      - Zero organizations: unchanged behavior, `auth.no_organization`.
      - Exactly one: unchanged behavior, log straight in with that
        organization -- a single-organization user notices nothing different.
      - More than one: does NOT pick one. Returns
        `OrganizationSelectionRequired` instead of `SessionTokens` -- no
        access token is issued yet, only a short-lived `selection_token`
        that proves this password check already succeeded. The caller must
        then call `select_organization` with a chosen organization_id, which
        independently re-verifies membership before issuing real tokens.
    """
    lookup = await users_service.get_credential_lookup(session, data.email)
    if (
        lookup is None
        or lookup.password_hash is None
        or not _verify_password(data.password, lookup.password_hash)
    ):
        raise PermissionDeniedError(
            "Invalid email or password.", error_code="auth.invalid_credentials"
        )
    if not lookup.is_active:
        raise PermissionDeniedError(
            "This account is inactive.", error_code="user.inactive"
        )

    organization_ids = await users_service.list_organizations_for_login(session, lookup.user_id)
    if len(organization_ids) == 0:
        raise PermissionDeniedError(
            "This account is not a member of any organization.",
            error_code="auth.no_organization",
        )

    if len(organization_ids) == 1:
        organization_id = organization_ids[0]
        tokens = await _issue_session(
            session, user_id=lookup.user_id, organization_id=organization_id, family_id=uuid.uuid4()
        )
        logger.info(
            "password_login_completed",
            user_id=str(lookup.user_id),
            organization_id=str(organization_id),
        )
        return tokens

    selection_token, _selection_expires_at = _issue_org_selection_token(lookup.user_id)
    organizations = await tenancy_repository.get_organizations_by_ids(session, organization_ids)
    logger.info(
        "password_login_organization_selection_required",
        user_id=str(lookup.user_id),
        organization_count=len(organizations),
    )
    return OrganizationSelectionRequired(
        selection_token=selection_token,
        organizations=tuple(
            OrganizationSummary(id=org.id, name=org.name, slug=org.slug) for org in organizations
        ),
    )


async def accept_invitation_with_password(
    session: AsyncSession, invitation_id: uuid.UUID, data: InvitationAcceptRequest
) -> SessionTokens:
    """Accept a password-organization invitation: prove control of the
    invited email address via `data.token`, provision a real account with
    `data.password` set, assign the invitation's `grants_role_id`, mark the
    invitation consumed, and log the new user in -- Phase 7.5's fix for a
    confirmed gap where `core.tenancy.service.accept_invitation` (still
    called at the end of this function, unchanged) only ever flipped the
    invitation's own status flag, provisioning nothing: calling it directly
    permanently consumed the invitation with no account, no role, and no
    session to show for it.

    Deliberately NOT added to `core.tenancy.service.accept_invitation`
    itself: that function is also the SSO-login-time acceptance path
    (`_resolve_or_provision_user` below calls it with no token at all,
    correctly -- an SSO login's signed `id_token` already proves identity, a
    separate token would be redundant), and `core.tenancy` cannot import
    `core.auth` (this module) to reach `_issue_session`/password hashing
    without creating a circular import (`core.auth` already depends on
    `core.tenancy`, per this module's own docstring) -- so the password-
    specific orchestration lives here instead, calling into
    `core.tenancy`'s already-existing, unmodified validation/status-update
    function once its own checks pass.

    Raises the same `NotFoundError`/`ConflictError` shapes `accept_invitation`
    itself would for a missing/non-pending/expired invitation (checked here
    too, before provisioning a user for an invitation that's about to be
    rejected anyway), plus `PermissionDeniedError` for a token mismatch --
    deliberately the same exception type/status (403) `login_with_password`
    uses for "invalid credentials," so this endpoint can't be used to
    distinguish "wrong token" from any other failure shape either.
    """
    invitation = await tenancy_repository.get_invitation_by_id(session, invitation_id)
    if invitation is None:
        raise NotFoundError(
            "Invitation not found.",
            error_code="invitation.not_found",
            detail={"invitation_id": str(invitation_id)},
        )
    if invitation.token_hash is None or not secrets.compare_digest(
        hash_opaque_token(data.token), invitation.token_hash
    ):
        raise PermissionDeniedError(
            "Invalid or missing invitation token.",
            error_code="invitation.invalid_token",
        )
    if invitation.status != "pending":
        raise ConflictError(
            "This invitation is no longer pending.",
            error_code="invitation.not_pending",
            detail={"status": invitation.status},
        )
    if invitation.expires_at <= datetime.now(timezone.utc):
        raise ConflictError(
            "This invitation has expired.",
            error_code="invitation.expired",
        )

    user_id = await users_service.get_or_create_user(
        session, email=invitation.email, display_name=data.display_name or invitation.email
    )
    await users_service.set_password(session, user_id=user_id, password_hash=_hash_password(data.password))
    await users_service.assign_role(
        session,
        user_id=user_id,
        organization_id=invitation.organization_id,
        role_id=invitation.grants_role_id,
    )
    # Re-validates status/expiry itself and marks `status="accepted"` --
    # harmless, intentional double-checking rather than trusting the checks
    # above stayed true for the entire duration of the provisioning calls.
    await tenancy_service.accept_invitation(session, invitation_id)

    tokens = await _issue_session(
        session, user_id=user_id, organization_id=invitation.organization_id, family_id=uuid.uuid4()
    )
    logger.info(
        "invitation_accepted_with_password",
        user_id=str(user_id),
        organization_id=str(invitation.organization_id),
        invitation_id=str(invitation_id),
    )
    return tokens


async def _resolve_client_secret(client_secret_ref: str) -> str:
    """Resolve a stored secret *reference* into the actual usable client
    secret needed for the token exchange.

    `shared/security`'s envelope-encryption helper (PROJECT_PLAN.md section
    12.5 -- a DEK per secret, itself encrypted by a KEK held in a managed
    KMS) now exists and is used elsewhere (`core.tenancy.service.
    register_connector`/`configure_sso`, `core.mcp_oauth.service`) -- this
    function previously predated that module and simply returned
    `client_secret_ref` unchanged, as if it were already the plaintext
    secret, which this module's own docstring flagged loudly rather than
    hid. Now that `configure_sso` envelope-encrypts the client secret at
    write time (see its docstring), this is the matching decrypt-at-read
    half of that same split, mirroring `ingestion.service.
    _execute_ingestion_job`'s identical call for connector credentials.
    """
    return await decrypt_secret(get_kms(), client_secret_ref)


async def _exchange_code_for_claims(
    sso_config: SSOConfiguration, *, code: str, code_verifier: str, redirect_uri: str
) -> VerifiedIdPClaims:
    """Exchange an authorization code for a verified ID token's claims.

    POSTs to the discovered `token_endpoint`, then verifies the returned ID
    token's signature against the issuer's JWKS (matching the token's `kid`
    header to the right key) before trusting any of its claims -- `jwt.decode`
    additionally validates standard claims (`iss`, `aud`, `exp`) as part of
    verification, so a token that passes this call has already been checked
    against all three, not just its signature.

    See this module's docstring for two known, flagged gaps: client secret
    handling isn't yet backed by real encryption (`_resolve_client_secret`),
    and `groups` extraction only covers the Entra ID/Okta-style `groups`
    claim shape, not every provider.
    """
    document = await _get_discovery_document(sso_config.issuer_url)
    client_secret = await _resolve_client_secret(sso_config.client_secret_ref)

    async with httpx.AsyncClient(timeout=10.0) as client:
        token_response = await client.post(
            document["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": sso_config.client_id,
                "client_secret": client_secret,
                "code_verifier": code_verifier,
            },
        )
        token_response.raise_for_status()
        token_payload = token_response.json()

        id_token = token_payload.get("id_token")
        if not id_token:
            raise PermissionDeniedError(
                "IdP token response did not include an ID token.",
                error_code="auth.idp_response_invalid",
            )

        jwks_response = await client.get(document["jwks_uri"])
        jwks_response.raise_for_status()
        jwks = jwks_response.json()

    try:
        unverified_header = jose_jwt.get_unverified_header(id_token)
    except JWTError as exc:
        raise PermissionDeniedError(
            "IdP returned a malformed ID token.",
            error_code="auth.idp_response_invalid",
        ) from exc

    matching_key = next(
        (key for key in jwks.get("keys", []) if key.get("kid") == unverified_header.get("kid")),
        None,
    )
    if matching_key is None:
        raise PermissionDeniedError(
            "Could not find a matching signing key for this ID token.",
            error_code="auth.idp_key_not_found",
        )

    try:
        claims = jose_jwt.decode(
            id_token,
            matching_key,
            algorithms=[matching_key.get("alg", "RS256")],
            audience=sso_config.client_id,
            issuer=sso_config.issuer_url,
        )
    except JWTError as exc:
        raise PermissionDeniedError(
            "ID token signature or claims verification failed.",
            error_code="auth.idp_token_invalid",
        ) from exc

    email = claims.get("email")
    if not email:
        raise PermissionDeniedError(
            "IdP did not provide a verified email claim.",
            error_code="auth.idp_response_invalid",
        )

    return VerifiedIdPClaims(
        sub=claims["sub"],
        email=email,
        name=claims.get("name"),
        groups=tuple(claims.get("groups") or ()),
    )


async def _resolve_or_provision_user(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    idp_subject: str,
    idp_claims: VerifiedIdPClaims,
) -> uuid.UUID:
    """Resolve an IdP subject to an EKIP user, provisioning both the
    authorization decision and the user itself just-in-time on first login.

    Supersedes the previous "a users row with this email already exists"
    stopgap (ENGINEERING_DECISIONS.md's SSO-provisioning-policy entry).
    "Is this login allowed to join this organization, and with which role" is
    now core/tenancy's `evaluate_provisioning` -- an actual, configurable
    policy decision, not a guess made inside authentication. This function's
    own job is orchestration only: ask tenancy for a decision, and only if
    it's allowed, ask core/users to create/resolve the user and grant the
    role, then record the mapping so this becomes a no-op on the next login.
    """
    mapping = await repository.get_external_identity_mapping(
        session, organization_id, idp_subject
    )
    if mapping is not None:
        return mapping.user_id

    decision = await tenancy_service.evaluate_provisioning(
        session,
        organization_id=organization_id,
        email=idp_claims.email,
        groups=idp_claims.groups,
    )
    if not decision.allowed:
        logger.warning(
            "sso_login_not_provisioned",
            organization_id=str(organization_id),
            idp_subject=idp_subject,
            reason=decision.reason,
        )
        raise PermissionDeniedError(
            "This account has not been provisioned. Contact your administrator.",
            error_code="auth.not_provisioned",
            detail={"organization_id": str(organization_id), "reason": decision.reason},
        )

    if decision.grants_role_id is None:
        # Should be unreachable -- ProvisioningDecision's contract guarantees
        # grants_role_id is set whenever allowed=True. Raised explicitly
        # (not via `assert`, which can be stripped under `-O`) so a future
        # bug in evaluate_provisioning fails loudly here, as an unexpected
        # error (AGENT_WORKFLOWS.md's "the system broke" case), rather than
        # surfacing as a confusing downstream foreign-key error.
        raise RuntimeError(
            "evaluate_provisioning returned allowed=True with no grants_role_id."
        )

    user_id = await users_service.get_or_create_user(
        session, email=idp_claims.email, display_name=idp_claims.name or idp_claims.email
    )
    await users_service.assign_role(
        session,
        user_id=user_id,
        organization_id=organization_id,
        role_id=decision.grants_role_id,
    )

    if decision.matched_invitation_id is not None:
        await tenancy_service.accept_invitation(session, decision.matched_invitation_id)

    await repository.insert_external_identity_mapping(
        session, organization_id=organization_id, user_id=user_id, idp_subject=idp_subject
    )
    logger.info(
        "sso_user_provisioned",
        user_id=str(user_id),
        organization_id=str(organization_id),
        reason=decision.reason,
    )
    return user_id


# --- Session issuance, refresh, logout -------------------------------------------


def _hash_token(raw_token: str) -> str:
    """Hash a raw refresh token for storage/lookup (section 12.1: never
    stored in plaintext). SHA-256 is sufficient here -- unlike a password,
    a refresh token is already a high-entropy random value, not something an
    attacker could feasibly brute-force from its hash.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _issue_access_token(user_id: uuid.UUID, organization_id: uuid.UUID) -> tuple[str, datetime, datetime]:
    """Sign a new access token, returning (token, issued_at, expires_at).

    `"type": "access"` is new, additive: it lets `verify_access_token`
    reject a well-signed-but-wrong-kind-of token (specifically, an
    org-selection token from `_issue_org_selection_token` below) instead of
    accepting anything bearing a valid signature regardless of what it was
    actually issued for -- the two token kinds share the same signing key,
    so the claim is what tells them apart.
    """
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=settings.jwt_expiry_minutes)
    claims = {
        "sub": str(user_id),
        "organization_id": str(organization_id),
        "type": "access",
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jose_jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, issued_at, expires_at


_ORG_SELECTION_TOKEN_TYPE = "org_selection"


def _issue_org_selection_token(user_id: uuid.UUID) -> tuple[str, datetime]:
    """Sign a short-lived token proving `user_id` already passed a password
    check, for the multi-organization login flow -- returned to the client
    inside `OrganizationSelectionRequired` and presented back to
    `select_organization` alongside the organization the user picked.

    Deliberately NOT an access token: it carries no `organization_id` (none
    has been chosen yet) and is tagged `"type": "org_selection"` rather than
    `"access"`, so `verify_access_token` -- and therefore every endpoint
    behind `CurrentIdentity` -- refuses it even though it is validly signed
    with the same `jwt_secret_key`. Its lifetime (`settings.
    org_selection_token_expiry_minutes`, default 10) is intentionally much
    shorter than a real access token's, since it only needs to survive the
    one extra round trip of the user choosing an organization.
    """
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=settings.org_selection_token_expiry_minutes)
    claims = {
        "sub": str(user_id),
        "type": _ORG_SELECTION_TOKEN_TYPE,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jose_jwt.encode(claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, expires_at


def _verify_org_selection_token(token: str) -> uuid.UUID:
    """Verify and decode a `selection_token`, returning the `user_id` it was
    issued for. Mirrors `verify_access_token`'s error shape (a generic
    `PermissionDeniedError`, distinct error_code), but checks for
    `"type": "org_selection"` instead of `"type": "access"` -- the two
    verifiers are deliberately not shared, so a bug in one can never
    accidentally relax the other.
    """
    settings = get_settings()
    try:
        claims = jose_jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except JWTError as exc:
        raise PermissionDeniedError(
            "Invalid or expired organization selection token.",
            error_code="auth.invalid_selection_token",
        ) from exc

    if claims.get("type") != _ORG_SELECTION_TOKEN_TYPE:
        raise PermissionDeniedError(
            "Invalid organization selection token.",
            error_code="auth.invalid_selection_token",
        )

    try:
        return uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise PermissionDeniedError(
            "Malformed organization selection token.",
            error_code="auth.invalid_selection_token",
        ) from exc


async def _issue_session(
    session: AsyncSession, *, user_id: uuid.UUID, organization_id: uuid.UUID, family_id: uuid.UUID
) -> SessionTokens:
    """Issue one access token + one new refresh token, persisting the latter.

    Shared by `complete_sso_login` (a fresh `family_id`) and `refresh`
    (the same `family_id` carried forward from the token being rotated) --
    the only difference between "first login" and "rotation" from this
    function's point of view is which `family_id` the caller passes in.

    Every caller (`complete_sso_login`, `signup`, `login_with_password`,
    `refresh`) reaches this function before any `Identity`-driven dependency
    (`app.api.deps.get_current_identity`) has had a chance to call
    `set_tenant_context` -- there is no request-scoped Identity yet at all
    on a first login/signup, and `refresh` resolves its own organization_id
    from the presented token rather than from an Identity. Milestone 10's
    `refresh_tokens` row is `FORCE ROW LEVEL SECURITY` (`c7d4e8f19a2b`), so
    the INSERT below is itself RLS-checked like any other write to that
    table -- calling `set_tenant_context` here, once, right before it,
    means every caller gets this for free instead of each having to
    remember it independently (a real gap: none of the three
    session-issuing callers set it themselves before this change, which
    surfaced as `asyncpg.exceptions.InvalidTextRepresentationError: invalid
    input syntax for type uuid: ""` the moment the app actually connected
    as the RLS-enforced `ekip_app` role instead of the `BYPASSRLS`
    `neondb_owner` every environment used until now).
    """
    await set_tenant_context(session, organization_id)
    access_token, issued_at, access_expires_at = _issue_access_token(user_id, organization_id)
    raw_refresh_token = secrets.token_urlsafe(48)
    refresh_expires_at = issued_at + _REFRESH_TOKEN_LIFETIME

    await repository.insert_refresh_token(
        session,
        user_id=user_id,
        organization_id=organization_id,
        family_id=family_id,
        token_hash=_hash_token(raw_refresh_token),
        expires_at=refresh_expires_at,
    )

    return SessionTokens(
        access_token=access_token,
        refresh_token=raw_refresh_token,
        expires_in=int((access_expires_at - issued_at).total_seconds()),
    )


async def peek_refresh_token(session: AsyncSession, raw_token: str):
    """Look up a refresh token's owning row without rotating or revoking it.

    Used by `app.mcp.oauth`'s `OAuthAuthorizationServerProvider.load_refresh_token`
    (the MCP-OAuth bridge for Claude's remote connector) to answer "does this
    refresh token exist, and is it still usable" -- the actual rotation and
    reuse-detection still happens in `refresh()` when the token is later
    exchanged, exactly as it would for a REST-originated refresh. Returns
    `None` for the same three reasons `refresh()` itself would reject the
    token (unknown, revoked, expired), just without consuming it.
    """
    token_hash = _hash_token(raw_token)
    token_organization_id = await repository.resolve_refresh_token_organization_id(session, token_hash)
    if token_organization_id is None:
        return None
    await set_tenant_context(session, token_organization_id)

    row = await repository.get_refresh_token_by_hash(session, token_hash)
    if row is None or row.revoked_at is not None or row.expires_at <= datetime.now(timezone.utc):
        return None
    return row


async def refresh(session: AsyncSession, data: RefreshRequest) -> SessionTokens:
    """Exchange a still-valid refresh token for a new session (rotation).

    Reuse detection (RefreshToken's model docstring): if the presented token
    has already been revoked (i.e. already rotated away, or already logged
    out), that is treated as a compromise signal -- the entire token family
    is revoked immediately and the request is denied, rather than silently
    accepting a replayed token.

    Milestone 10 RLS note: `refresh_tokens` is RLS-protected, and this
    function starts from a bare, client-presented token hash with no
    `Identity`/org context yet -- the same chicken-and-egg shape
    `ingestion.service._execute_ingestion_job` has for `connector_configs`.
    Resolved the same way: a narrow, RLS-bypassing lookup
    (`repository.resolve_refresh_token_organization_id`) discovers just the
    owning organization_id, `set_tenant_context` is set to it, and only then
    does the real, RLS-scoped `get_refresh_token_by_hash` query run.
    """
    now = datetime.now(timezone.utc)
    token_hash = _hash_token(data.refresh_token)

    token_organization_id = await repository.resolve_refresh_token_organization_id(
        session, token_hash
    )
    if token_organization_id is None:
        raise PermissionDeniedError(
            "Invalid refresh token.", error_code="auth.invalid_refresh_token"
        )
    await set_tenant_context(session, token_organization_id)

    row = await repository.get_refresh_token_by_hash(session, token_hash)
    if row is None:
        raise PermissionDeniedError(
            "Invalid refresh token.", error_code="auth.invalid_refresh_token"
        )

    if row.revoked_at is not None:
        await repository.revoke_family(session, row.family_id, revoked_at=now)
        logger.warning(
            "refresh_token_reuse_detected",
            family_id=str(row.family_id),
            user_id=str(row.user_id),
            organization_id=str(row.organization_id),
        )
        raise PermissionDeniedError(
            "This session has been revoked.", error_code="auth.refresh_token_reused"
        )

    if row.expires_at <= now:
        raise PermissionDeniedError(
            "Refresh token has expired.", error_code="auth.refresh_token_expired"
        )

    await repository.revoke_refresh_token(session, row.id, revoked_at=now)
    return await _issue_session(
        session, user_id=row.user_id, organization_id=row.organization_id, family_id=row.family_id
    )


async def logout(session: AsyncSession, data: RefreshRequest) -> None:
    """Revoke the single session identified by `data.refresh_token`.

    Idempotent: logging out a token that's already invalid/gone is a no-op,
    not an error -- a client retrying a logout call should never see a
    failure for something that already succeeded.

    Milestone 10 RLS note: same bare-token-hash-before-org-known shape as
    `refresh` above -- see that function's docstring. Here, an unresolvable
    token hash is simply a no-op (matching this function's own idempotent
    contract) rather than an error.
    """
    token_hash = _hash_token(data.refresh_token)

    token_organization_id = await repository.resolve_refresh_token_organization_id(
        session, token_hash
    )
    if token_organization_id is None:
        return
    await set_tenant_context(session, token_organization_id)

    row = await repository.get_refresh_token_by_hash(session, token_hash)
    if row is None or row.revoked_at is not None:
        return
    await repository.revoke_refresh_token(session, row.id, revoked_at=datetime.now(timezone.utc))


async def revoke_all_sessions(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> int:
    """Revoke every active session for `user_id` within `organization_id`
    ("log out everywhere", or admin-forced termination -- section 12.1).

    Returns the number of sessions revoked. Deliberately takes no `actor` /
    permission check yet: whether this may be called on one's own behalf only,
    or by an admin on someone else's behalf, is an authorization question for
    whatever future admin-tooling endpoint calls this -- not decided here.
    """
    return await repository.revoke_all_for_user(
        session, user_id, organization_id, revoked_at=datetime.now(timezone.utc)
    )


# --- Access token verification ------------------------------------------------


def verify_access_token(token: str) -> TokenClaims:
    """Verify and decode an access token, returning its claims.

    No database access, by design (mirrors this file's module docstring):
    this answers "whose token is this, and is it validly signed and
    unexpired" -- turning that into a full `Identity` (with roles/permissions
    resolved) is `core.users.service.resolve_identity`'s job, called
    separately by whatever boundary layer (REST or MCP) verified this token.

    The `claims.get("type", "access")` check below is deliberately
    backward-compatible: a token signed before this multi-organization
    change shipped (`_issue_access_token` not yet setting `"type"`) has no
    `type` claim at all, and is treated as `"access"` rather than rejected --
    otherwise every access token already outstanding at deploy time would
    suddenly fail verification. Only a token explicitly tagged some other
    type (currently just `_ORG_SELECTION_TOKEN_TYPE`,
    `"org_selection"`) is rejected here, which is exactly the new case this
    guards against: an `OrganizationSelectionRequired.selection_token`
    presented to an ordinary `CurrentIdentity`-gated endpoint instead of to
    `select_organization`.
    """
    settings = get_settings()
    try:
        claims = jose_jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except JWTError as exc:
        raise PermissionDeniedError(
            "Invalid or expired access token.", error_code="auth.invalid_token"
        ) from exc

    if claims.get("type", "access") != "access":
        raise PermissionDeniedError(
            "This token is not a valid access token.", error_code="auth.invalid_token"
        )

    try:
        return TokenClaims(
            user_id=uuid.UUID(claims["sub"]),
            organization_id=uuid.UUID(claims["organization_id"]),
            issued_at=datetime.fromtimestamp(claims["iat"], tz=timezone.utc),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
        )
    except (KeyError, ValueError) as exc:
        raise PermissionDeniedError(
            "Malformed access token.", error_code="auth.invalid_token"
        ) from exc


# --- Multi-organization selection/switching ----------------------------------


async def select_organization(session: AsyncSession, data: OrganizationSelectionRequest) -> SessionTokens:
    """Complete a multi-organization login: exchange a `selection_token`
    (from `login_with_password`'s `OrganizationSelectionRequired` response)
    plus a chosen `organization_id` for real `SessionTokens`.

    Security model, per the task's explicit requirement: `data.
    organization_id` is a request, never trusted by itself. The flow is
    `client requests organization X -> backend authenticates current user
    (via the selection_token, which already proved the password check
    succeeded) -> backend independently re-verifies user<->organization X
    membership via list_organizations_for_login -> backend issues a JWT for
    organization X` -- at no point does a client-supplied organization_id
    get written into a token without that membership check running first.
    """
    user_id = _verify_org_selection_token(data.selection_token)
    organization_ids = await users_service.list_organizations_for_login(session, user_id)
    if data.organization_id not in organization_ids:
        raise PermissionDeniedError(
            "You are not a member of that organization.", error_code="auth.not_a_member"
        )

    tokens = await _issue_session(
        session, user_id=user_id, organization_id=data.organization_id, family_id=uuid.uuid4()
    )
    logger.info(
        "organization_selection_completed",
        user_id=str(user_id),
        organization_id=str(data.organization_id),
    )
    return tokens


async def switch_organization(
    session: AsyncSession, *, user_id: uuid.UUID, data: SwitchOrganizationRequest
) -> SessionTokens:
    """Re-scope an already-authenticated session to a different organization
    the same user also belongs to (`POST /auth/switch-organization`).

    `user_id` comes from the caller's *existing*, already-verified
    `CurrentIdentity` (resolved by the router from their current access
    token) -- never from the request body. Same security model as
    `select_organization`: `data.organization_id` is independently
    re-verified via `list_organizations_for_login` before anything is
    issued, so a user cannot switch into an organization by simply naming
    its id. A brand-new access + refresh token pair is issued for the
    target organization (a fresh `family_id`, exactly like a new login)
    rather than mutating the caller's existing token in place -- the old
    token/session for the previous organization is left exactly as it was
    (still valid until its own expiry/logout), it simply does not carry
    access to the newly selected organization, matching the task's
    requirement that switching happen through a newly issued token rather
    than by granting the old one broader scope.
    """
    organization_ids = await users_service.list_organizations_for_login(session, user_id)
    if data.organization_id not in organization_ids:
        raise PermissionDeniedError(
            "You are not a member of that organization.", error_code="auth.not_a_member"
        )

    tokens = await _issue_session(
        session, user_id=user_id, organization_id=data.organization_id, family_id=uuid.uuid4()
    )
    logger.info(
        "organization_switch_completed",
        user_id=str(user_id),
        organization_id=str(data.organization_id),
    )
    return tokens


async def list_available_organizations(
    session: AsyncSession, user_id: uuid.UUID
) -> OrganizationsListResponse:
    """List every organization the given (already-authenticated) `user_id`
    belongs to, for `GET /auth/organizations`.

    The router passes `actor.user_id` from `CurrentIdentity` -- the verified
    caller's own id, never a client-supplied one -- so this always answers
    "which organizations does the *caller* belong to," never an arbitrary
    other user's membership.
    """
    organization_ids = await users_service.list_organizations_for_login(session, user_id)
    organizations = await tenancy_repository.get_organizations_by_ids(session, organization_ids)
    return OrganizationsListResponse(
        organizations=tuple(
            OrganizationSummary(id=org.id, name=org.name, slug=org.slug) for org in organizations
        )
    )
