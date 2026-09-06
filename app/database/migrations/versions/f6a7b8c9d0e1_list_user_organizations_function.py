"""list_user_organization_ids: SECURITY DEFINER bypass for multi-org login/switch

Revision ID: f6a7b8c9d0e1
Revises: d4e5f6a7b8c9
Create Date: 2026-09-05 00:00:00.000000

Closes the multi-organization login/session design gap: `core.auth.
service.login_with_password` previously discovered which organization to
put in a fresh access token via `core.users.repository.
get_first_organization_id` (`resolve_user_first_organization`,
`c5e2a9f4d7b3`) -- "one organization this user_id holds a role in, or
none" -- silently selecting whichever organization a password-authenticated
user's `user_roles` rows happened to return first when they belonged to
more than one. This migration adds the generalized "all organizations"
counterpart so the application layer can actually tell "exactly one" apart
from "more than one" and require explicit selection in the latter case,
rather than continuing to guess.

Same "chicken-and-egg" RLS gap as `resolve_user_first_organization`
(see that migration's own docstring) and the same fix shape: `user_roles`
is one of `c7d4e8f19a2b`'s `FORCE ROW LEVEL SECURITY` tables, and a
password login (or an already-authenticated user asking to switch
organizations) starts from a bare `user_id` with no -- or, for a switch,
the *wrong* -- tenant context set for the organization being checked. This
function answers only "which organization ids does this user hold a role
in," exactly the same narrow, read-only enumeration
`resolve_user_first_organization` already performs, generalized from
`LIMIT 1` to no limit. The caller still calls `set_tenant_context` and
runs ordinary RLS-scoped queries for anything beyond that -- this function
grants no broader read access than the existing one already established
for the identical problem.

`SET search_path = public` for the same reason every other `SECURITY
DEFINER` function in this codebase pins it (see `d2e5f8a3c1b6`'s own
module docstring on the search_path-hijack footgun this closes).
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: str | None = 'd4e5f6a7b8c9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION list_user_organization_ids(user_id_arg uuid)
        RETURNS TABLE(organization_id uuid)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT DISTINCT organization_id FROM user_roles WHERE user_id = user_id_arg;
        $$;
        """
    )


def downgrade() -> None:
    op.execute('DROP FUNCTION IF EXISTS list_user_organization_ids(uuid)')
