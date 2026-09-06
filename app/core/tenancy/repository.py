"""Persistence for core/tenancy -- organizations, projects, SSO configuration,
connector configuration.

Owned by: core/tenancy. Pure data access, same discipline as
core/audit/repository.py and core/users/repository.py: each function issues
one statement and returns ORM rows (or None/a sequence of them). No business
rules (onboarding-state transitions, "does this slug already exist") and no
ORM->Pydantic mapping live here -- that's service.py's job
(ARCHITECTURE.md section 3: infrastructure/persistence holds no business
logic).

Every insert function flushes and refreshes so DB-generated columns (`id` via
gen_random_uuid(), timestamps via now()) are populated on the returned row;
the surrounding transaction is committed by the caller's session scope, not
by these functions -- same convention as core/audit/repository.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.ingestion_models import IngestionJob
from app.database.models.tenancy_models import (
    ConnectorConfig,
    Invitation,
    Organization,
    OrganizationAccessRule,
    Project,
    SSOConfiguration,
)

# --- Organizations -----------------------------------------------------------


async def insert_organization(
    session: AsyncSession, *, name: str, slug: str, status: str = "onboarding"
) -> Organization:
    """Create one organization row and return it with server defaults populated."""
    row = Organization(name=name, slug=slug, status=status)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_organization_by_id(
    session: AsyncSession, organization_id: uuid.UUID
) -> Organization | None:
    """Fetch a single organization by primary key, or None if absent."""
    return await session.get(Organization, organization_id)


async def list_organizations(session: AsyncSession) -> Sequence[Organization]:
    """Return every organization in the system, unscoped -- see
    `service.list_organizations`'s docstring for why this has no actor/
    organization_id filter, unlike every other query in this file.
    """
    result = await session.execute(select(Organization))
    return result.scalars().all()


async def get_organization_by_slug(
    session: AsyncSession, slug: str
) -> Organization | None:
    """Fetch a single organization by its login-URL slug, or None if absent.

    Backs the `/o/{org-slug}/login` lookup in the SSO flow (PROJECT_PLAN.md
    section 3.3, section 11.1) -- resolving which organization's
    `sso_configurations` row to redirect an employee against.
    """
    stmt = select(Organization).where(Organization.slug == slug)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_organizations_by_ids(
    session: AsyncSession, organization_ids: Sequence[uuid.UUID]
) -> Sequence[Organization]:
    """Fetch `Organization` rows for a caller-supplied set of ids, in no
    particular order -- for the multi-organization login/session work
    (`core.users.service.list_organizations_for_login`), turning a
    membership id list into the `{id, name, slug}` summaries a login/
    organizations response actually needs to show a person.

    Deliberately unscoped by any organization_id filter, same as
    `list_organizations` above -- but unlike that function this is NOT a
    system-internal "every organization" listing: the caller is expected to
    have already restricted `organization_ids` to ones a specific user
    actually belongs to (via `list_user_organization_ids`/
    `list_organizations_for_login`) before calling this. This is safe
    without `set_tenant_context` for the same reason `get_organization_by_id`
    above is: `organizations` itself carries no RLS policy, since it IS the
    tenant boundary a policy would otherwise scope by (see the `Organization`
    model's own docstring) -- there is no narrower row set to leak into.
    An empty `organization_ids` returns an empty sequence rather than every
    organization, unlike a naive unfiltered query would.
    """
    if not organization_ids:
        return []
    stmt = select(Organization).where(Organization.id.in_(organization_ids))
    result = await session.execute(stmt)
    return result.scalars().all()


# --- Projects ----------------------------------------------------------------


async def insert_project(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    name: str,
    is_default: bool = False,
) -> Project:
    """Create one project row within `organization_id` and return it."""
    row = Project(organization_id=organization_id, name=name, is_default=is_default)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_project_by_id(
    session: AsyncSession, project_id: uuid.UUID
) -> Project | None:
    """Fetch a single project by primary key, or None if absent."""
    return await session.get(Project, project_id)


async def list_projects(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[Project]:
    """Return every project belonging to `organization_id`, ordered by name."""
    stmt = (
        select(Project)
        .where(Project.organization_id == organization_id)
        .order_by(Project.name)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_default_project(
    session: AsyncSession, organization_id: uuid.UUID
) -> Project | None:
    """Fetch `organization_id`'s auto-created default project, or None if it
    somehow has none yet.

    Every organization is expected to have exactly one `is_default=True`
    project (PROJECT_PLAN.md section 3.2), created alongside the organization
    itself -- this is what lets small customers get a uniform `project_id` on
    every incident/document without ever creating a second project.
    """
    stmt = select(Project).where(
        Project.organization_id == organization_id, Project.is_default.is_(True)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# --- SSO configuration ---------------------------------------------------------


async def insert_sso_configuration(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    provider: str,
    protocol: str,
    issuer_url: str,
    client_id: str,
    client_secret_ref: str,
) -> SSOConfiguration:
    """Create the (single, unique-per-organization) SSO configuration row.

    The model's `unique=True` on `organization_id` is the actual "one SSO
    config per org" guarantee (PROJECT_PLAN.md section 3.2) -- this function
    does not itself check for an existing row; replacing an organization's SSO
    provider is a distinct, not-yet-built operation left to a future
    `update_sso_configuration`, not silently folded into insert.
    """
    row = SSOConfiguration(
        organization_id=organization_id,
        provider=provider,
        protocol=protocol,
        issuer_url=issuer_url,
        client_id=client_id,
        client_secret_ref=client_secret_ref,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_sso_configuration_by_organization_id(
    session: AsyncSession, organization_id: uuid.UUID
) -> SSOConfiguration | None:
    """Fetch `organization_id`'s SSO configuration, or None if not yet set up.

    Backs `core/auth`'s `get_organization_sso_config(org_slug)` step in the
    login flow (PROJECT_PLAN.md section 11.1) once the caller has already
    resolved the slug to an `organization_id` via `get_organization_by_slug`.
    """
    stmt = select(SSOConfiguration).where(
        SSOConfiguration.organization_id == organization_id
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# --- Connector configuration ----------------------------------------------------


async def insert_connector_config(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    source: str,
    credential_ref: str,
    project_id: uuid.UUID | None = None,
    config: dict | None = None,
    status: str = "connecting",
) -> ConnectorConfig:
    """Create one connector configuration row and return it.

    `credential_ref` is expected to already be a valid reference into the
    encrypted secret store (PROJECT_PLAN.md section 12.5) -- this function
    never receives or stores a raw credential.
    """
    row = ConnectorConfig(
        organization_id=organization_id,
        project_id=project_id,
        source=source,
        credential_ref=credential_ref,
        config=config if config is not None else {},
        status=status,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_connector_config_by_id(
    session: AsyncSession, connector_config_id: uuid.UUID
) -> ConnectorConfig | None:
    """Fetch a single connector configuration by primary key, or None if absent."""
    return await session.get(ConnectorConfig, connector_config_id)


async def list_connector_configs(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[ConnectorConfig]:
    """Return every connector configuration belonging to `organization_id`,
    newest first. Backs `list_connectors` (PROJECT_PLAN.md section 9.2).
    """
    stmt = (
        select(ConnectorConfig)
        .where(ConnectorConfig.organization_id == organization_id)
        .order_by(ConnectorConfig.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_ingestion_runs(
    session: AsyncSession,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> Sequence[IngestionJob]:
    """Return `connector_config_id`'s ingestion run history, newest first.

    Phase 2D addition backing `GET /tenancy/connectors/{id}/runs` -- reads
    the `ingestion_jobs` table directly (owned by `app.ingestion`'s data
    model, but core/tenancy is import-linter-forbidden from depending on
    `app.ingestion` itself) rather than reaching through `app.ingestion.
    service`, the same "read the raw table directly" precedent `app.
    ingestion.repository` already established for its own reads of
    `connector_configs` (this module's own table). Filtered by both
    `organization_id` and `connector_config_id` -- the caller
    (`core.tenancy.service.list_ingestion_runs`) already verified the
    connector belongs to this organization, but filtering here too costs
    nothing and means this function is never the one place a tenant-
    isolation bug could slip through.
    """
    stmt = (
        select(IngestionJob)
        .where(
            IngestionJob.organization_id == organization_id,
            IngestionJob.connector_config_id == connector_config_id,
        )
        .order_by(IngestionJob.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_ingestion_run(
    session: AsyncSession,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    job_id: uuid.UUID,
) -> IngestionJob | None:
    stmt = select(IngestionJob).where(
        IngestionJob.id == job_id,
        IngestionJob.organization_id == organization_id,
        IngestionJob.connector_config_id == connector_config_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_ingestion_job_stats(
    session: AsyncSession, organization_id: uuid.UUID, *, since: datetime | None = None
) -> list[Any]:
    """Aggregate `ingestion_jobs` by `connector_config_id` for
    `organization_id` (Phase 5.6): count, succeeded/failed counts, average
    duration, total documents processed -- the ingestion-side counterpart to
    `app.agents.repository.get_agent_execution_stats`/`core.observability.
    repository.get_mcp_tool_stats`. Same "read `ingestion_jobs` directly"
    precedent as `list_ingestion_runs` above (see that function's own
    docstring for why core/tenancy, not `app.ingestion`, owns this read).

    Returns raw SQLAlchemy `Row` objects -- `service.py` builds
    `IngestionJobStats` directly from these labeled columns, same
    convention as the two aggregate functions named above.
    """
    succeeded_case = case((IngestionJob.status == "succeeded", 1), else_=0)
    failed_case = case((IngestionJob.status == "failed", 1), else_=0)
    dead_lettered_case = case((IngestionJob.status == "dead_lettered", 1), else_=0)
    # Average only *successful* runs. A failed/interrupted job's `completed_at`
    # is stamped by `repository.insert_ingestion_job`'s stale-predecessor
    # recovery at the moment a replacement attempt starts -- which can be days
    # after its `started_at` -- so `completed_at - started_at` for those rows
    # is wall-clock-since-abandoned, not work time, and dragged the reported
    # average into the tens of thousands of seconds.
    duration_seconds_case = case(
        (
            (IngestionJob.status == "succeeded")
            & IngestionJob.completed_at.is_not(None)
            & IngestionJob.started_at.is_not(None),
            func.extract("epoch", IngestionJob.completed_at - IngestionJob.started_at),
        ),
        else_=None,
    )

    stmt = (
        select(
            IngestionJob.connector_config_id.label("connector_config_id"),
            func.count().label("run_count"),
            func.sum(succeeded_case).label("succeeded_count"),
            func.sum(failed_case).label("failed_count"),
            func.sum(dead_lettered_case).label("dead_lettered_count"),
            func.avg(duration_seconds_case).label("avg_duration_seconds"),
            func.sum(IngestionJob.documents_processed).label("total_documents_processed"),
            func.sum(IngestionJob.pages_fetched).label("total_pages_fetched"),
            func.sum(IngestionJob.items_discovered).label("total_items_discovered"),
            func.sum(IngestionJob.items_skipped).label("total_items_skipped"),
            func.sum(IngestionJob.chunks_embedded).label("total_chunks_embedded"),
            func.sum(IngestionJob.retry_count).label("total_retries"),
        )
        .where(IngestionJob.organization_id == organization_id)
        .group_by(IngestionJob.connector_config_id)
    )
    if since is not None:
        stmt = stmt.where(IngestionJob.created_at >= since)

    result = await session.execute(stmt)
    return list(result.all())


async def update_connector_config_sync_status(
    session: AsyncSession,
    connector_config_id: uuid.UUID,
    *,
    status: str,
    last_synced_at: datetime | None = None,
    config_patch: dict | None = None,
) -> ConnectorConfig | None:
    """Update a connector's `status` (and optionally `last_synced_at`/
    `config`) after a sync attempt, returning the updated row or None if it
    doesn't exist.

    `config_patch`, when given, is shallow-merged into the existing `config`
    JSONB (`row.config = {**row.config, **config_patch}`) rather than
    replacing it outright -- see `service.update_connector_sync_status`'s
    own docstring for why (persisting a cross-sync resume token without
    disturbing admin-supplied config keys).

    A narrow, explicit mutation rather than a generic update-by-dict: ingestion
    reporting "this connector's sync just succeeded/failed" is the only
    tenancy-owned mutation an external caller currently needs
    (PROJECT_PLAN.md section 4.5 -- job status is tracked explicitly since the
    caller and worker no longer share a call stack).

    Once `"disconnected"` (the "Delete connector" feature, `core.tenancy.
    service.disconnect_connector`), a transition to any *other* status is
    silently ignored rather than applied -- a real race, not a hypothetical
    one: `app.ingestion.service._execute_ingestion_job` only checks
    disconnected status once, at the very start of a sync, in its own
    long-lived transaction. A user can disconnect a connector *while* one of
    its syncs is already mid-flight (this check runs too early to catch
    that), and that sync's own eventual success/failure report -- this exact
    function, called with `status="active"`/`"error"` -- must not be allowed
    to silently revive a connector the user already deleted. Disconnecting
    an already-disconnected connector (the one case where `status` here
    genuinely is `"disconnected"` again) still applies normally; it's a
    same-value no-op either way.
    """
    row = await session.get(ConnectorConfig, connector_config_id)
    if row is None:
        return None
    if row.status == "disconnected" and status != "disconnected":
        return row
    row.status = status
    if last_synced_at is not None:
        row.last_synced_at = last_synced_at
    if config_patch is not None:
        row.config = {**row.config, **config_patch}
    await session.flush()
    await session.refresh(row)
    return row


async def patch_connector_config_ingestion_checkpoint(
    session: AsyncSession,
    connector_config_id: uuid.UUID,
    *,
    config_patch: dict,
) -> ConnectorConfig | None:
    """Persist high-frequency, worker-owned pagination progress.

    This intentionally does not change connector status or ``last_synced_at``
    and does not use the audited status-update path: a large source can emit
    thousands of pages, and turning each durability checkpoint into a domain
    audit event would make the audit log unusable. A disconnected connector
    is left untouched so an in-flight worker cannot mutate user-deleted
    configuration after the disconnect wins the race.
    """
    row = await session.get(ConnectorConfig, connector_config_id)
    if row is None:
        return None
    if row.status == "disconnected":
        return row
    row.config = {**row.config, **config_patch}
    await session.flush()
    await session.refresh(row)
    return row


# --- Organization access rules (domain / group auto-join) --------------------


async def insert_access_rule(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    rule_type: str,
    value: str,
    grants_role_id: uuid.UUID,
    is_active: bool = True,
) -> OrganizationAccessRule:
    """Create one access rule row and return it with server defaults populated."""
    row = OrganizationAccessRule(
        organization_id=organization_id,
        rule_type=rule_type,
        value=value,
        grants_role_id=grants_role_id,
        is_active=is_active,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_access_rule_by_id(
    session: AsyncSession, rule_id: uuid.UUID
) -> OrganizationAccessRule | None:
    """Fetch a single access rule by primary key, or None if absent."""
    return await session.get(OrganizationAccessRule, rule_id)


async def list_access_rules(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[OrganizationAccessRule]:
    """Return every access rule belonging to `organization_id` (active or
    not), for admin-facing listing. Evaluation-time lookups use
    `get_active_rules_by_type` instead, which filters to active rows only.
    """
    stmt = (
        select(OrganizationAccessRule)
        .where(OrganizationAccessRule.organization_id == organization_id)
        .order_by(OrganizationAccessRule.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_active_rules_by_type(
    session: AsyncSession, organization_id: uuid.UUID, rule_type: str
) -> Sequence[OrganizationAccessRule]:
    """Return every *active* rule of `rule_type` for `organization_id`.

    Used by `evaluate_provisioning` to check a login's email domain or IdP
    groups against the organization's configured rules -- restricted to
    `is_active=True` so a suspended rule never matches.
    """
    stmt = select(OrganizationAccessRule).where(
        OrganizationAccessRule.organization_id == organization_id,
        OrganizationAccessRule.rule_type == rule_type,
        OrganizationAccessRule.is_active.is_(True),
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def deactivate_access_rule(
    session: AsyncSession, rule_id: uuid.UUID
) -> OrganizationAccessRule | None:
    """Set an access rule's `is_active` to False, returning the updated row
    or None if it doesn't exist. Deactivating, not deleting, preserves the
    rule's history rather than losing it.
    """
    row = await session.get(OrganizationAccessRule, rule_id)
    if row is None:
        return None
    row.is_active = False
    await session.flush()
    await session.refresh(row)
    return row


# --- Invitations ---------------------------------------------------------------


async def insert_invitation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    email: str,
    grants_role_id: uuid.UUID,
    invited_by: uuid.UUID,
    expires_at: datetime,
    token_hash: str | None = None,
) -> Invitation:
    """Create one invitation row (status defaults to `"pending"`, per the
    model's column default) and return it with server defaults populated.

    `token_hash` (Phase 7.5) is optional -- `None` is a legitimate value,
    not an omission: see `Invitation.token_hash`'s own column comment for
    when a `NULL` token is correct (pre-Phase-7.5 rows; this codebase has no
    other caller that leaves it unset going forward).

    Does not itself enforce "at most one pending invitation per email" --
    that is the database's partial unique index
    (`uq_invitations_org_email_pending`), which raises an integrity error on
    violation; the service layer is expected to check `get_pending_invitation`
    first for a clean domain error instead of surfacing a raw database
    exception.
    """
    row = Invitation(
        organization_id=organization_id,
        email=email,
        grants_role_id=grants_role_id,
        invited_by=invited_by,
        expires_at=expires_at,
        token_hash=token_hash,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_invitation_by_id(
    session: AsyncSession, invitation_id: uuid.UUID
) -> Invitation | None:
    """Fetch a single invitation by primary key, or None if absent."""
    return await session.get(Invitation, invitation_id)


async def get_pending_invitation(
    session: AsyncSession, organization_id: uuid.UUID, email: str
) -> Invitation | None:
    """Fetch `email`'s pending invitation within `organization_id`, or None.

    Returns the row regardless of whether `expires_at` has already passed --
    a `status="pending"` row past its expiry simply hasn't been swept yet
    (lazy expiration, since no cleanup job exists yet); `evaluate_provisioning`
    is responsible for checking `expires_at` against the current time itself
    rather than trusting `status` alone to be up to date.
    """
    stmt = select(Invitation).where(
        Invitation.organization_id == organization_id,
        Invitation.email == email,
        Invitation.status == "pending",
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_invitations(
    session: AsyncSession, organization_id: uuid.UUID
) -> Sequence[Invitation]:
    """Return every invitation belonging to `organization_id`, newest first."""
    stmt = (
        select(Invitation)
        .where(Invitation.organization_id == organization_id)
        .order_by(Invitation.created_at.desc())
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def update_invitation_status(
    session: AsyncSession,
    invitation_id: uuid.UUID,
    *,
    status: str,
    accepted_at: datetime | None = None,
) -> Invitation | None:
    """Transition an invitation's status (accepted/expired/revoked), returning
    the updated row or None if it doesn't exist.

    One narrow mutation covering all three terminal transitions, rather than
    three separate functions, since they share the same shape (set `status`,
    optionally set `accepted_at`) -- mirrors
    `update_connector_config_sync_status`'s precedent above.
    """
    row = await session.get(Invitation, invitation_id)
    if row is None:
        return None
    row.status = status
    if accepted_at is not None:
        row.accepted_at = accepted_at
    await session.flush()
    await session.refresh(row)
    return row
