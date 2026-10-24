"""This module handles database access for users, roles, and permissions. Each function 
performs a query and returns data, while `service.py` handles identity creation and 
authorization decisions. The key optimization is that `get_role_names` and 
`get_permission_codes` fetch the required information in just two indexed queries, 
allowing later permission checks to happen in memory without additional database calls. 
For multi-tenancy, both queries filter by `user_id` and `organization_id`, ensuring a 
user's roles and permissions are resolved only for the current organization, preventing 
cross-company permission leaks.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.core_models import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from app.database.models.tenancy_models import Project, ProjectMembership

# Defines the fixed permission list granted to the admin role during signup.

ADMIN_PERMISSION_CODES: Sequence[str] = (
    "tenancy:manage",
    "incident:read",
    "incident:write",
    "postmortem:write",
    "postmortem:approve",
    "knowledge:review",
    "observability:read",
    "audit:read",
)


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Fetch a single user by primary key, or None if absent."""
    return await session.get(User, user_id)


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    """Fetch a single user by their unique email, or None if absent.

    Used by core/auth during credential resolution (login looks a user up by
    email before issuing a token).
    """
    stmt = select(User).where(User.email == email)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_organization_members(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[tuple[User, Sequence[str]]]:
    """Return every user holding at least one role in `organization_id`,
    each paired with the names of every role they hold there -- the read
    side of "who is on this team," backing the previously-missing `GET
    /organizations/{id}/members` (the frontend's Users settings page called
    a `GET /users` endpoint that never existed; this is the real
    equivalent).

    Two queries, not a single query with an aggregate: `user_roles` is a
    many-to-many join (a user can hold more than one role in the same
    organization), and building the per-user role-name list in Python next
    to a single ordered `users` query is simpler and just as cheap at this
    scale as a `GROUP BY`/`array_agg`, matching `get_permission_codes`'s own
    "group in Python, not SQL" precedent in `get_project_permission_map`
    just below.
    """
    member_ids_stmt = (
        select(UserRole.user_id).where(UserRole.organization_id == organization_id).distinct()
    )
    member_ids = (await session.execute(member_ids_stmt)).scalars().all()
    if not member_ids:
        return []

    users_stmt = select(User).where(User.id.in_(member_ids)).order_by(User.display_name)
    users = (await session.execute(users_stmt)).scalars().all()

    roles_stmt = (
        select(UserRole.user_id, Role.name)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.organization_id == organization_id, UserRole.user_id.in_(member_ids))
    )
    roles_by_user: dict[uuid.UUID, list[str]] = {}
    for user_id, role_name in (await session.execute(roles_stmt)).all():
        roles_by_user.setdefault(user_id, []).append(role_name)

    return [(user, sorted(roles_by_user.get(user.id, []))) for user in users]


async def get_role_names(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> Sequence[str]:
    """Return the names of the roles assigned to `user_id` within
    `organization_id`.

    Joins `user_roles -> roles`, filtered by both `user_id` and
    `organization_id` -- a role assignment held in a different organization
    must never be returned (PROJECT_PLAN.md section 3.5). Empty sequence if
    the user has no roles in this organization.
    """
    stmt = (
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user_id,
            UserRole.organization_id == organization_id,
        )
        .order_by(Role.name)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def insert_user(session: AsyncSession, *, email: str, display_name: str) -> User:
    """Create a new user row and return it with server defaults populated.

    Called only after a decision to allow provisioning has already been made
    elsewhere (core/tenancy's `evaluate_provisioning`) -- this function
    performs no authorization or policy check of its own; core/users manages
    identity/roles, not who may join an organization.
    """
    row = User(email=email, display_name=display_name)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_role_by_name(session: AsyncSession, name: str) -> Role | None:
    """Fetch a single role by its unique name, or None if absent.

    Used by core/tenancy to resolve a role name (e.g. `"engineer"`, as
    supplied in an `AccessRuleCreate`/`InvitationCreate` request) into the
    `grants_role_id` actually stored -- a read-only reference-data lookup, not
    a tenant-scoped query, since the `roles` catalog itself is global
    (DATABASE_DESIGN.md).
    """
    stmt = select(Role).where(Role.name == name)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_role(
    session: AsyncSession,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    role_id: uuid.UUID,
) -> UserRole | None:
    """Fetch one (user, organization, role) assignment row, or None if it
    doesn't exist -- lets callers make role assignment idempotent by checking
    first, rather than relying on catching an integrity error.
    """
    return await session.get(UserRole, (user_id, organization_id, role_id))


async def insert_user_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    role_id: uuid.UUID,
) -> None:
    """Assign `role_id` to `user_id` within `organization_id`.

    Not idempotent by itself -- inserting a duplicate composite key raises an
    integrity error. Callers are expected to check `get_user_role` first
    (the same "check, then act" pattern used elsewhere in this module, e.g.
    `resolve_identity`'s `is_active` check) rather than this function
    silently swallowing a conflict.
    """
    session.add(UserRole(user_id=user_id, organization_id=organization_id, role_id=role_id))
    await session.flush()


async def get_permission_codes(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> set[str]:
    """Return the flattened set of permission codes granted to `user_id`
    *within `organization_id`*.

    Joins `user_roles -> role_permissions -> permissions`, filtered by both
    `user_id` and `organization_id`, and de-duplicates: two roles granting the
    same permission collapse to one code. This set is what the service loads
    into `Identity.permissions` -- scoped to a single organization, never
    merged across every organization a user might belong to.
    """
    stmt = (
        select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .where(
            UserRole.user_id == user_id,
            UserRole.organization_id == organization_id,
        )
        .distinct()
    )
    result = await session.execute(stmt)
    return set(result.scalars().all())


async def get_project_permission_map(
    session: AsyncSession, user_id: uuid.UUID, organization_id: uuid.UUID
) -> dict[uuid.UUID, frozenset[str]]:

    """Return a user's permission codes grouped by project within an organization."""

    stmt = (
        select(ProjectMembership.project_id, Permission.code)
        .join(Project, Project.id == ProjectMembership.project_id)
        .join(RolePermission, RolePermission.role_id == ProjectMembership.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            ProjectMembership.user_id == user_id,
            Project.organization_id == organization_id,
        )
        .distinct()
    )
    result = await session.execute(stmt)

    grouped: dict[uuid.UUID, set[str]] = {}
    for project_id, code in result.all():
        grouped.setdefault(project_id, set()).add(code)
    return {project_id: frozenset(codes) for project_id, codes in grouped.items()}


async def update_password_hash(session: AsyncSession, *, user_id: uuid.UUID, password_hash: str) -> None:
    """Set (or replace) `user_id`'s local password credential.

    Used only by `core.auth.service.signup` -- keeps the "only core/users's
    repository writes `users` rows" discipline intact rather than having
    core/auth reach into this table directly.
    """
    row = await session.get(User, user_id)
    if row is None:
        raise ValueError(f"update_password_hash: no such user {user_id}")  # unreachable in practice
    row.password_hash = password_hash
    await session.flush()


async def get_first_organization_id(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """Return an organization where the user has a role, or None if they have none.
Used during password login when the user's organization is not yet known.
Because Row-Level Security blocks normal queries before tenant context exists,
it uses a dedicated SQL function to safely resolve the organization. Signup
currently assigns each password-auth user exactly one organization role.
"""

    result = await session.execute(
        text("SELECT resolve_user_first_organization(:user_id)"),
        {"user_id": str(user_id)},
    )
    return result.scalar_one_or_none()


async def list_organization_ids(session: AsyncSession, user_id: uuid.UUID) -> Sequence[uuid.UUID]:
    """Return every organization `user_id` holds a role in (possibly empty).

    The generalized sibling of `get_first_organization_id` above, added for
    the multi-organization login/session design gap (`f6a7b8c9d0e1_list_
    user_organizations_function.py`): a password login needs to tell
    "exactly one organization" apart from "more than one" *before* deciding
    whether to log the user straight in or require them to pick, which
    `get_first_organization_id`'s `LIMIT 1`-shaped result can't answer by
    itself. Deliberately does NOT replace or change `get_first_organization_id`
    -- both remain, each used where its own shape is what's needed.

    Same RLS bootstrap problem as `get_first_organization_id` (no tenant
    context can exist yet at this point in the flow, and `user_roles` is
    `FORCE ROW LEVEL SECURITY`), so this goes through the equally narrow
    `list_user_organization_ids` SQL function rather than a plain ORM query
    against `UserRole`, mirroring `app.ingestion.repository.
    list_active_connector_config_ids`'s exact multi-row bypass-function
    pattern.
    """
    result = await session.execute(
        text("SELECT organization_id FROM list_user_organization_ids(:user_id)"),
        {"user_id": str(user_id)},
    )
    return result.scalars().all()


async def get_or_create_permissions(session: AsyncSession, codes: Sequence[str]) -> list[Permission]:
    """Fetch existing `Permission` rows for `codes`, inserting any missing
    ones -- the same "populate the fixed catalog if it isn't there yet"
    behavior `scripts/seed_test_organization.py`'s dev bootstrap already
    relies on, now reusable by real signup instead of only a dev script.
    """
    result = await session.execute(select(Permission).where(Permission.code.in_(codes)))
    existing = {row.code: row for row in result.scalars().all()}

    permissions = []
    for code in codes:
        if code in existing:
            permissions.append(existing[code])
            continue
        permission = Permission(code=code, description=f"Seeded by signup bootstrap: {code}")
        session.add(permission)
        permissions.append(permission)
    await session.flush()
    return permissions


async def get_or_create_role_by_name(session: AsyncSession, name: str, *, description: str) -> Role:
    """Fetch the `Role` named `name`, creating it (with `description`) if it
    doesn't exist yet. Idempotent -- safe to call on every signup, not just
    the first.
    """
    existing = await get_role_by_name(session, name)
    if existing is not None:
        return existing

    role = Role(name=name, description=description)
    session.add(role)
    await session.flush()
    return role


async def grant_permissions_to_role(
    session: AsyncSession, *, role_id: uuid.UUID, permissions: Sequence[Permission]
) -> None:
    """Grant every permission in `permissions` to `role_id`, skipping any
    already granted -- idempotent, mirroring `insert_user_role`'s sibling
    `get_user_role`-then-`insert_user_role` pattern rather than relying on a
    database-constraint-driven retry.
    """
    granted = await session.execute(
        select(RolePermission.permission_id).where(RolePermission.role_id == role_id)
    )
    already_granted = set(granted.scalars().all())
    for permission in permissions:
        if permission.id not in already_granted:
            session.add(RolePermission(role_id=role_id, permission_id=permission.id))
    await session.flush()
