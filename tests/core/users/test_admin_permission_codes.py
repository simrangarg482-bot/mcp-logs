"""Regression test for the `incident:read` / `ADMIN_PERMISSION_CODES` gap
fixed by `app/database/migrations/versions/f3e7c05b146e_grant_incident_read_to_admin_role.py`.

`d706a360fc2a` (2026-08-18) added the `incident:read` permission and gated
`core.incidents.service.get_incident`/`list_incidents`/`get_timeline` on it,
backfilling every role that existed at the time -- but
`core.users.repository.ADMIN_PERMISSION_CODES`, the fixed list
`core.users.service.ensure_admin_role` grants to the shared "admin" role on
every self-service signup, was never updated to include it. Every
organization whose admin role was (re-)granted by signup after that
migration shipped could write incidents but not read them back: a real
production bug ("incidents unable to load" against the deployed frontend),
diagnosed and fixed this session.

This test pins the fix in place: it fails again the moment
`ADMIN_PERMISSION_CODES` drops `incident:read`, without needing a live
database or a real signup to notice.
"""

from __future__ import annotations

from app.core.incidents.service import _INCIDENT_READ_PERMISSION, _INCIDENT_WRITE_PERMISSION
from app.core.users.repository import ADMIN_PERMISSION_CODES


def test_admin_permission_codes_includes_incident_read() -> None:
    """A newly bootstrapped ("admin") role must be able to read incidents,
    not just write them -- the exact gap this regression covers.
    """
    assert _INCIDENT_READ_PERMISSION in ADMIN_PERMISSION_CODES


def test_admin_permission_codes_includes_incident_write() -> None:
    """Sanity check the list still grants what it always granted -- this
    fix must not have narrowed anything while widening `incident:read`.
    """
    assert _INCIDENT_WRITE_PERMISSION in ADMIN_PERMISSION_CODES


def test_admin_permission_codes_has_no_duplicates() -> None:
    """A hand-maintained list (per its own docstring) -- guard against a
    copy-paste duplicate slipping in alongside a future addition.
    """
    codes = list(ADMIN_PERMISSION_CODES)
    assert len(codes) == len(set(codes))
