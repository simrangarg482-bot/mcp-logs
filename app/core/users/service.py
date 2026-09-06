"""Public interface for core/users -- identity resolution and authorization.

Owned by: core/users. This module is the authority on who a user is and what
they may do. It resolves persisted role assignments into an `Identity`
(consumed everywhere) and provides `authorize()` / `require_permission()` --
the checks that make REST and MCP access control identical (ARCHITECTURE.md
section 6; API_DESIGN.md section 2).

Design notes:
  - `resolve_identity` is the ONLY sanctioned way to build a user `Identity`
    (see the deliberate absence of an `Identity.for_user` constructor): it
    guarantees the permission set is actually loaded, so an identity can never
    silently carry empty/incorrect permissions.
  - `authorize` is intentionally pure (no session): once an identity is
    resolved, every permission check is an in-memory set-membership test. This
    is what lets the same identity be checked cheaply many times per request.
  - Multi-tenancy (PROJECT_PLAN.md section 3.5-3.6): every identity is
    resolved *within* one organization, so `resolve_identity` and
    `get_user_profile` both now require `organization_id` -- there is no
    "resolve a user" operation that isn't also "resolve them for this
    organization." `authorize`/`require_permission` additionally accept an
    optional `project_id` for the finer-grained project-scoped check; omitting
    it preserves the original, cheaper org-level-only check.
  - Project-level permission *resolution* (populating
    `Identity.project_permissions` from `project_memberships`) is now wired
    up: `resolve_identity` loads it via `repository.get_project_permission_map`
    in the same fixed-number-of-queries shape as the org-level `roles`/
    `permissions` lookups. `has_permission` already supported a `project_id`
    argument before this landed, so this was purely additive -- no other
    module's call sites needed to change to start getting real project-scoped
    enforcement (see `core.incidents.service`, whose `require_permission(...,
    project_id=...)` calls predate this and were, until now, silently always
    falling back to the org-level check for every caller).
  - `require_project_permission` is a thin, explicitly-named wrapper around
    `require_permission` for call sites that are *always* project-scoped
    (never optionally) -- it changes no enforcement behavior, only readability
    at the call site.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.core.users import repository
from app.core.users.schemas import OrganizationMember, UserCredentialLookup, UserProfile
from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.schemas import ActorKind, Identity

logger = get_logger(__name__)


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    """Tenant-isolation guard, same shape as `core.tenancy.service`'s own
    (duplicated per that module's own precedent -- see its docstring on why
    a small, cheap guard like this is copied per-module rather than shared).
    Needed here because `list_organization_members`, unlike every other
    function in this file, is reached via an explicit `{organization_id}`
    path parameter (`GET /organizations/{id}/members`), not derived purely
    from `actor.organization_id` with no override possible.
    """
    if actor.organization_id != organization_id:
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="users.cross_organization_denied",
            detail={"organization_id": str(organization_id)},
        )


async def resolve_identity(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> Identity:
    """Build a fully-populated `Identity` for a user, scoped to
    `organization_id`.

    Loads the user row plus their role names and flattened permission codes
    *within that organization* (PROJECT_PLAN.md section 3.5), in a small fixed
    number of indexed queries. Raises:
      - NotFoundError if no such user exists.
      - PermissionDeniedError if the user exists but is deactivated (a
        disabled account must never yield a usable identity).

    A user with no role assignment in `organization_id` resolves successfully
    to an Identity with empty `roles`/`permissions`, rather than raising here:
    every downstream `authorize()` check then fails closed for them, which is
    the safe behavior. Verifying that a user is actually a *member* of
    `organization_id` at all (vs. simply having zero permissions there) is a
    login-time concern for core/auth (PROJECT_PLAN.md section 3.3), not
    something this resolver re-derives.

    This is the resolver that core/auth calls after it has verified a
    credential/token and determined which organization the session is scoped
    to; auth never reads roles/permissions itself.

    Milestone 10 RLS note: `user_roles` (and the tables `get_role_names`/
    `get_permission_codes` join through it) is RLS-protected -- and,
    critically, this function is what runs *before* `app.api.deps.
    get_current_identity`/`app.mcp.dispatch.run_mcp_tool` get a chance to
    call `set_tenant_context` themselves (both call it only *after*
    `resolve_identity` already returned). Since `organization_id` is already
    a required parameter here -- unlike the genuine bare-id chicken-and-egg
    cases elsewhere in Milestone 10 -- the fix is simply to set the GUC
    here, first, rather than waiting for a caller further up the stack.
    Without this, every single request's role/permission lookup would
    silently return empty sets under RLS (not an error -- an `Identity`
    with zero permissions, failing every `authorize()` check closed) rather
    than the caller's real roles.
    """
    await set_tenant_context(session, organization_id)

    user = await repository.get_by_id(session, user_id)
    if user is None:
        raise NotFoundError(
            "User not found.",
            error_code="user.not_found",
            detail={"user_id": str(user_id)},
        )
    if not user.is_active:
        raise PermissionDeniedError(
            "User account is inactive.",
            error_code="user.inactive",
            detail={"user_id": str(user_id)},
        )

    role_names = await repository.get_role_names(session, user_id, organization_id)
    permission_codes = await repository.get_permission_codes(
        session, user_id, organization_id
    )
    project_permissions = await repository.get_project_permission_map(
        session, user_id, organization_id
    )

    return Identity(
        kind=ActorKind.USER,
        subject=str(user.id),
        organization_id=organization_id,
        user_id=user.id,
        display_name=user.display_name,
        roles=tuple(role_names),
        permissions=frozenset(permission_codes),
        project_permissions=project_permissions,
    )


async def get_user_profile(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> UserProfile:
    """Return the human-facing profile (user + roles + permissions) for a
    user, scoped to `organization_id`.

    Backs `GET /auth/me` and user administration. Scoped the same way as
    `resolve_identity` and for the same reason: the same person can hold
    different roles in different organizations, so "their profile" only means
    something relative to one of them. Raises NotFoundError if the user does
    not exist. Unlike `resolve_identity`, this does not reject inactive users
    -- an admin still needs to see and manage them.
    """
    user = await repository.get_by_id(session, user_id)
    if user is None:
        raise NotFoundError(
            "User not found.",
            error_code="user.not_found",
            detail={"user_id": str(user_id)},
        )

    role_names = await repository.get_role_names(session, user_id, organization_id)
    permission_codes = await repository.get_permission_codes(
        session, user_id, organization_id
    )

    return UserProfile(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        roles=tuple(role_names),
        permissions=tuple(sorted(permission_codes)),
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def list_organization_members(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[OrganizationMember]:
    """List every user holding a role in `organization_id`, with their role
    names -- backs `GET /organizations/{id}/members` (Phase 2's fix for the
    frontend's Users settings page, which previously called a `GET /users`
    endpoint that never existed on the backend at all).

    No `require_permission` gate beyond organization membership itself:
    seeing your own teammates' names, emails, and roles is ordinary within
    an organization (needed for e.g. deciding who to loop into an incident),
    unlike `core.tenancy.service.list_invitations`/`list_access_rules`
    (Phase 1's fix), which expose configuration data, not who's on the team.
    """
    _ensure_same_organization(actor, organization_id)
    rows = await repository.list_organization_members(session, organization_id)
    return [
        OrganizationMember(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            is_active=user.is_active,
            roles=tuple(role_names),
            created_at=user.created_at,
        )
        for user, role_names in rows
    ]


async def get_or_create_user(
    session: AsyncSession, *, email: str, display_name: str
) -> uuid.UUID:
    """Resolve `email` to a user, creating one if none exists yet.

    Called only after a provisioning decision has already been made
    elsewhere (core/tenancy's `evaluate_provisioning`) -- this function
    performs no authorization/policy check of its own.

    `User` is a single global person record, not scoped to one company
    (see `core_models.py`'s `User` docstring): "does this email already have
    an account" is checked globally, not per-organization, so the same
    person logging into a *second* organization for the first time reuses
    their existing account rather than creating a duplicate -- consistent
    with the existing design that lets one person hold different roles in
    different companies via `UserRole`.
    """
    existing = await repository.get_by_email(session, email)
    if existing is not None:
        return existing.id

    created = await repository.insert_user(session, email=email, display_name=display_name)
    logger.info("user_provisioned", user_id=str(created.id), email=email)
    return created.id


async def assign_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    role_id: uuid.UUID,
) -> None:
    """Grant `role_id` to `user_id` within `organization_id`, idempotently.

    Checks for an existing assignment first rather than relying on the
    database to reject a duplicate composite key, so a caller that runs this
    on every login (e.g. re-affirming a role an active access rule grants)
    never has to handle an integrity error for the common "already has this
    role" case.

    Takes no `actor`/permission check: like `get_or_create_user`, this is
    only meant to be called once a decision to grant this role has already
    been made elsewhere (SSO provisioning today; a future admin
    role-management endpoint would need its own `require_permission` check at
    its own boundary, not one added retroactively here).
    """
    existing = await repository.get_user_role(session, user_id, organization_id, role_id)
    if existing is not None:
        return

    await repository.insert_user_role(
        session, user_id=user_id, organization_id=organization_id, role_id=role_id
    )
    logger.info(
        "role_assigned",
        user_id=str(user_id),
        organization_id=str(organization_id),
        role_id=str(role_id),
    )


def authorize(
    actor: Identity, permission_code: str, project_id: uuid.UUID | None = None
) -> bool:
    """Pure authorization check: does `actor` hold `permission_code`?

    No database access -- operates on the permission set(s) already resolved
    onto the identity. With no `project_id`, checks the org-level set only
    (identical behavior to before this migration). With a `project_id`, checks
    that project's override if one exists, else falls back to the org-level
    set (PROJECT_PLAN.md section 3.6). This is the boolean form (API_DESIGN.md
    section 2); use `require_permission` when a denial should abort the
    operation.
    """
    return actor.has_permission(permission_code, project_id=project_id)


def require_permission(
    actor: Identity, permission_code: str, project_id: uuid.UUID | None = None
) -> None:
    """Enforce `authorize`, raising `PermissionDeniedError` on failure.

    The enforcing form used at the top of every guarded service operation, so
    a denied action fails uniformly regardless of entry point (REST or MCP)
    and regardless of whether the check is org-level or project-scoped.
    """
    if not authorize(actor, permission_code, project_id=project_id):
        logger.warning(
            "permission_denied",
            actor=actor.audit_tag,
            organization_id=str(actor.organization_id),
            project_id=str(project_id) if project_id is not None else None,
            required_permission=permission_code,
        )
        raise PermissionDeniedError(
            "You do not have permission to perform this action.",
            error_code="permission_denied",
            detail={"required_permission": permission_code},
        )


async def get_credential_lookup(session: AsyncSession, email: str) -> UserCredentialLookup | None:
    """Look up `email`'s local password credential, or `None` if no user
    with that email exists at all. See `UserCredentialLookup`'s own
    docstring for why this returns a narrow schema, not a `User` row.
    """
    row = await repository.get_by_email(session, email)
    if row is None:
        return None
    return UserCredentialLookup(
        user_id=row.id, password_hash=row.password_hash, is_active=row.is_active
    )


async def set_password(session: AsyncSession, *, user_id: uuid.UUID, password_hash: str) -> None:
    """Set `user_id`'s local password credential (already hashed by the
    caller -- `core/users` has no opinion on the hashing scheme, that's
    `core.auth.service`'s job). Only called from `core.auth.service.signup`,
    which needs `users` written without reaching into its table directly
    (see `repository.update_password_hash`'s own docstring).
    """
    await repository.update_password_hash(session, user_id=user_id, password_hash=password_hash)


async def resolve_organization_for_login(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """Resolve which organization a password-authenticated `user_id` should
    log into -- `None` if they hold no role assignment anywhere (shouldn't
    happen for a `signup`-created account, but not assumed away). See
    `repository.get_first_organization_id`'s docstring for the current
    one-organization-per-password-account scope this reflects.
    """
    return await repository.get_first_organization_id(session, user_id)


async def list_organizations_for_login(session: AsyncSession, user_id: uuid.UUID) -> Sequence[uuid.UUID]:
    """Return every organization `user_id` holds a role in, so
    `core.auth.service.login_with_password` can tell "exactly one" apart
    from "more than one" and require explicit organization selection in the
    latter case, instead of silently picking one via
    `resolve_organization_for_login`/`get_first_organization_id` (both left
    unchanged -- this is a new, additive sibling, not a replacement).

    Empty means the user holds no role assignment anywhere -- the same case
    `resolve_organization_for_login` represents as `None`, just spelled as an
    empty sequence here since "zero of many" is the natural empty case for a
    listing rather than a special sentinel.
    """
    return await repository.list_organization_ids(session, user_id)


_ADMIN_ROLE_NAME = "admin"


async def ensure_admin_role(session: AsyncSession) -> uuid.UUID:
    """Fetch-or-create the platform's `"admin"` role, granting it every
    permission code `repository.ADMIN_PERMISSION_CODES` lists, and return its
    id.

    Idempotent -- safe to call on every signup, not just the first ever. This
    is the same bootstrap `scripts/seed_test_organization.py`'s dev-only
    script has always performed by hand; extracted here so real signup
    (`core.auth.service.signup`) grants a newly-created organization's first
    user the same working permission set without duplicating that logic.
    """
    permissions = await repository.get_or_create_permissions(session, repository.ADMIN_PERMISSION_CODES)
    role = await repository.get_or_create_role_by_name(
        session, _ADMIN_ROLE_NAME, description="Full-access role granted to an organization's first user."
    )
    await repository.grant_permissions_to_role(session, role_id=role.id, permissions=permissions)
    return role.id


def require_project_permission(
    actor: Identity, project_id: uuid.UUID, permission_code: str
) -> None:
    """Project-scoped `require_permission` -- for call sites that always have
    a specific project in hand and are never checking at the organization
    level only.

    Pure call-site clarity, not a new enforcement path: this is exactly
    `require_permission(actor, permission_code, project_id=project_id)`.
    Keeping both names lets a reader tell "this operation is scoped to one
    project, unconditionally" (this function) apart from "this operation
    optionally narrows to a project if one happens to be known"
    (`require_permission`'s own `project_id: ... | None` parameter) without
    reading the call site's surrounding context.
    """
    require_permission(actor, permission_code, project_id=project_id)
