"""grant incident:read to the admin role (backfill for ADMIN_PERMISSION_CODES gap)

Revision ID: f3e7c05b146e
Revises: f6a7b8c9d0e1
Create Date: 2026-09-06 00:00:00.000000

Fixes a real production bug: `d706a360fc2a` (2026-08-18) added the
`incident:read` permission and backfilled it onto every role that existed
*at that time*, but `app.core.users.repository.ADMIN_PERMISSION_CODES` --
the fixed list `core.users.service.ensure_admin_role` grants to the single
shared "admin" role on every self-service signup -- was never updated to
include it (only `scripts/seed_test_organization.py`'s dev-only bootstrap
was, per its own "one exception" comment). Every organization whose admin
role was created or re-granted by real signup after `d706a360fc2a` shipped
could therefore create incidents (`incident:write`, which the list does
have) but not read them back: `GET /incidents` / `GET
/incidents/{id}` / timeline all 403 with `required_permission:
"incident:read"`. This is the bug behind the "incidents unable to load"
report against the production frontend.

The code half of this fix (adding `"incident:read"` to
`ADMIN_PERMISSION_CODES`) self-heals every organization sharing that "admin"
role the next time anyone signs up (`ensure_admin_role` is idempotent and
re-grants the full list every call) -- but that could be an arbitrarily long
wait for an org that already exists. This migration grants the permission to
the "admin" role directly and immediately, the same
`INSERT ... ON CONFLICT DO NOTHING` idempotency `d706a360fc2a` itself used,
scoped only to the role actually created by this bootstrap path (`admin`)
rather than every role, since `d706a360fc2a` already covered every role that
existed before it and no other role-creation path exists in this codebase
(`core.users.repository.get_or_create_role_by_name` has exactly one caller,
`core.users.service.ensure_admin_role`, which only ever names `"admin"`).

`permissions`/`roles`/`role_permissions` carry no Row-Level Security policy
(`c7d4e8f19a2b_milestone_10_row_level_security.py`'s "genuinely global
catalogs" list) -- no `set_tenant_context`/GUC call is needed here, matching
`d706a360fc2a`'s own reasoning.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3e7c05b146e'
down_revision: str | None = 'f6a7b8c9d0e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_CODE = "incident:read"
_PERMISSION_DESCRIPTION = "Read an incident, its timeline, and its postmortem (2026-08 audit 'H4')."
_ROLE_NAME = "admin"


def upgrade() -> None:
    bind = op.get_bind()

    # Same idempotent seed `d706a360fc2a` used -- a no-op if it already ran.
    bind.execute(
        sa.text(
            "INSERT INTO permissions (code, description) "
            "VALUES (:code, :description) "
            "ON CONFLICT (code) DO NOTHING"
        ),
        {"code": _PERMISSION_CODE, "description": _PERMISSION_DESCRIPTION},
    )

    bind.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r "
            "CROSS JOIN permissions p "
            "WHERE p.code = :code AND r.name = :role_name "
            "ON CONFLICT DO NOTHING"
        ),
        {"code": _PERMISSION_CODE, "role_name": _ROLE_NAME},
    )


def downgrade() -> None:
    bind = op.get_bind()

    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN ("
            "SELECT id FROM permissions WHERE code = :code"
            ") AND role_id IN ("
            "SELECT id FROM roles WHERE name = :role_name"
            ")"
        ),
        {"code": _PERMISSION_CODE, "role_name": _ROLE_NAME},
    )
