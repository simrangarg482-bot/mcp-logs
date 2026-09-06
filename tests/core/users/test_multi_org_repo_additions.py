"""Repository-level tests for the multi-organization login/session design-gap
fix's two new data-access functions:

  - `app.core.users.repository.list_organization_ids` (the SECURITY DEFINER
    `list_user_organization_ids` bypass wrapper added by
    `f6a7b8c9d0e1_list_user_organizations_function.py`), the generalized,
    additive sibling of the existing `get_first_organization_id` -- neither
    that function nor `core.users.service.resolve_organization_for_login`
    is touched by this change.
  - `app.core.tenancy.repository.get_organizations_by_ids`, turning a list
    of organization ids a user belongs to into the `Organization` rows a
    login/organizations response needs to show.

Same style as `tests/core/users/test_repository.py` and
`tests/ingestion/test_repository.py`'s existing bypass-function tests: no
real Postgres connection is available to this suite, so this exercises the
statement/parameter shape against a fake `AsyncSession`, not the actual
bypass behavior against a live database.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.tenancy import repository as tenancy_repository
from app.core.users import repository as users_repository


class _FakeScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class _FakeSession:
    def __init__(self, return_value) -> None:
        self._return_value = return_value
        self.executed: list[tuple[str, dict[str, object]]] = []

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params or {}))
        return _FakeScalarResult(self._return_value)


# --- core.users.repository.list_organization_ids ----------------------------


@pytest.mark.asyncio
async def test_list_organization_ids_calls_bypass_function() -> None:
    user_id = uuid.uuid4()
    organization_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    session = _FakeSession(organization_ids)

    result = await users_repository.list_organization_ids(session, user_id)

    assert result == organization_ids
    statement, params = session.executed[0]
    assert "list_user_organization_ids" in statement
    assert params == {"user_id": str(user_id)}


@pytest.mark.asyncio
async def test_list_organization_ids_returns_empty_when_user_holds_no_role() -> None:
    session = _FakeSession([])

    result = await users_repository.list_organization_ids(session, uuid.uuid4())

    assert result == []


@pytest.mark.asyncio
async def test_list_organization_ids_returns_single_element_for_one_org_user() -> None:
    """The exactly-one-organization case, which `login_with_password` relies
    on to preserve the existing convenient single-organization login
    behavior unchanged.
    """
    organization_id = uuid.uuid4()
    session = _FakeSession([organization_id])

    result = await users_repository.list_organization_ids(session, uuid.uuid4())

    assert list(result) == [organization_id]


# --- core.tenancy.repository.get_organizations_by_ids ------------------------


class _FakeOrganizationRow:
    def __init__(self, *, id, name, slug) -> None:
        self.id = id
        self.name = name
        self.slug = slug


class _FakeOrgLookupSession:
    def __init__(self, rows) -> None:
        self._rows = rows
        self.executed: list[str] = []

    async def execute(self, statement, params=None):
        self.executed.append(str(statement))
        return self

    def scalars(self):
        return self

    def all(self):
        return self._rows


@pytest.mark.asyncio
async def test_get_organizations_by_ids_returns_matching_rows() -> None:
    org_a = _FakeOrganizationRow(id=uuid.uuid4(), name="Org A", slug="org-a")
    org_b = _FakeOrganizationRow(id=uuid.uuid4(), name="Org B", slug="org-b")
    session = _FakeOrgLookupSession([org_a, org_b])

    result = await tenancy_repository.get_organizations_by_ids(session, [org_a.id, org_b.id])

    assert result == [org_a, org_b]
    assert session.executed  # a real query was actually issued


@pytest.mark.asyncio
async def test_get_organizations_by_ids_returns_empty_for_empty_input_without_querying() -> None:
    """An empty id list must short-circuit to an empty result rather than
    running an unfiltered `WHERE id IN ()` (which some backends treat as
    matching nothing, but SQLAlchemy/asyncpg can warn on or mishandle) --
    and, more importantly, must never silently become "every organization."
    """
    session = _FakeOrgLookupSession([_FakeOrganizationRow(id=uuid.uuid4(), name="Should not appear", slug="x")])

    result = await tenancy_repository.get_organizations_by_ids(session, [])

    assert result == []
    assert session.executed == []
