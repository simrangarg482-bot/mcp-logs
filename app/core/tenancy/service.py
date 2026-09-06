"""Public interface for core/tenancy -- organizations, projects, SSO
configuration, connector configuration.

Owned by: core/tenancy. This module is the authority on "what organization/
project does this belong to, and what's connected to it" (PROJECT_PLAN.md
section 9.2). Business rules and ORM->Pydantic mapping live here; raw SQL
lives in repository.py; the wire/HTTP concerns live in the future api/ layer.

Tenant isolation (PROJECT_PLAN.md section 3.7): every function below that
takes an `organization_id` argument also takes the calling `actor: Identity`
and verifies `actor.organization_id == organization_id` before doing anything
else -- there is no operation here that lets an authenticated caller read or
write another organization's tenancy data, matching the "no admin override
query path that skips it" rule. `create_organization` is the sole exception,
since an organization does not exist yet at the moment it is being created --
see its docstring.

Authorization: mutating operations (`create_project`, `register_connector`,
`configure_sso`) additionally require the `tenancy:manage` permission via
core/users's `require_permission`, and record an audit event via
core/audit's `record_audit_event` -- the same cross-submodule dependency
pattern documented for core/incidents (PROJECT_PLAN.md section 9.4). Seeding
`tenancy:manage` into the platform's fixed permission catalog is a data
migration concern, not something this module manages.

Milestone 10 addition (PROJECT_PLAN.md section 12.5): `register_connector`
now depends on `shared/security` to envelope-encrypt a connector's plaintext
credential before persisting it -- the first real caller of that module.
See `register_connector`'s own docstring for the encrypt-at-write/decrypt-
at-read split with `ingestion.service`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.service import record_audit_event
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.tenancy import repository
from app.core.tenancy.schemas import (
    AccessRule,
    AccessRuleCreate,
    ConnectorConfig,
    ConnectorConfigCreate,
    IngestionJobStats,
    IngestionRun,
    Invitation,
    InvitationCreate,
    Organization,
    OrganizationCreate,
    Project,
    ProjectCreate,
    ProvisioningDecision,
    SSOConfiguration,
    SSOConfigurationCreate,
)
from app.core.users import repository as users_repository
from app.core.users.service import require_permission, require_project_permission
from app.database.session import set_tenant_context
from app.shared.config.logging import get_logger
from app.shared.schemas import Identity
from app.shared.security import (
    KmsUnavailableError,
    encrypt_secret,
    generate_opaque_token,
    get_kms,
    hash_opaque_token,
)

logger = get_logger(__name__)

_MANAGE_PERMISSION = "tenancy:manage"
# Same permission as `agents.service.get_agent_execution_stats`/`core.
# observability.service.get_mcp_dashboard` -- an aggregate health dashboard
# is an observability concern, not connector configuration, so it reuses
# `observability:read` rather than `tenancy:manage` (unlike
# `list_ingestion_runs` above, whose per-connector run *history* is closer
# to a configuration drill-down and correctly reuses `_MANAGE_PERMISSION`
# instead -- these are two different questions about the same table).
_OBSERVABILITY_READ_PERMISSION = "observability:read"
# Applied when InvitationCreate.expires_at is omitted -- not yet a Settings
# field, same accepted gap as core/auth/service.py's _REFRESH_TOKEN_LIFETIME.
_DEFAULT_INVITATION_LIFETIME = timedelta(days=14)


def _ensure_same_organization(actor: Identity, organization_id: uuid.UUID) -> None:
    """Tenant-isolation guard: deny any operation scoped to an organization
    other than the caller's own (PROJECT_PLAN.md section 3.7).

    Deliberately a `PermissionDeniedError`, not a `NotFoundError` -- consistent
    with `core.users.service.require_permission`'s existing convention for
    authorization failures elsewhere in the codebase, rather than introducing
    a second denial style for this module alone.
    """
    if actor.organization_id != organization_id:
        logger.warning(
            "tenancy_cross_organization_denied",
            actor=actor.audit_tag,
            actor_organization_id=str(actor.organization_id),
            requested_organization_id=str(organization_id),
        )
        raise PermissionDeniedError(
            "Cannot access another organization's data.",
            error_code="tenancy.cross_organization_denied",
            detail={"organization_id": str(organization_id)},
        )


# --- Organizations -----------------------------------------------------------


async def create_organization(
    session: AsyncSession, data: OrganizationCreate, actor: Identity | None = None
) -> Organization:
    """Create a new organization together with its mandatory default project.

    `actor` is optional and defaults to `None`: an organization does not
    exist yet at the moment it is created, so there is no valid
    organization-scoped Identity to *require* one from (Identity.
    organization_id is mandatory per ENGINEERING_DECISIONS.md #004). This is
    the sole exception to this module's "every mutating operation requires
    `tenancy:manage`" rule (see module docstring) for exactly the callers
    that genuinely have no `Identity` yet: `core.auth.service.signup`'s
    public self-serve flow, and the dev-only bootstrap scripts
    (`scripts/seed_test_organization.py`, `scripts/test_milestone6.py`).
    None of these are gated here -- a brand-new customer creating their own
    first organization via signup is the intended, unauthenticated path, not
    a gap.

    A caller that *does* already have an `Identity` -- the REST
    `POST /organizations` endpoint, reached by an already-authenticated user
    creating an *additional* organization -- is a different case and is
    gated by `require_permission(actor, _MANAGE_PERMISSION)` the same as
    every other mutating operation in this module. This was previously
    unenforced (any authenticated user, in any organization, with any or no
    permissions, could call this endpoint to spin up an arbitrary new
    organization) -- a real, confirmed authorization gap, not an
    intentionally open one; the docstring here previously said so itself.
    `tenancy:manage` is checked against the actor's *own current*
    organization, since the organization being created doesn't exist yet to
    check anything against -- this doesn't by itself make the created
    organization reachable by that actor (there is still no multi-org-per-
    session capability; see PROJECT_STATUS.md's "Organization switching"
    item), it only restricts who may trigger the creation at all.
    Unauthorized callers still fail closed with the same
    `PermissionDeniedError` `require_permission` raises everywhere else.

    Auto-creates the "General" default project in the same transaction
    (PROJECT_PLAN.md section 3.2: every organization has at least one project,
    so every incident/document has a uniform `project_id` even for customers
    who never create a second one).

    Raises ConflictError if `data.slug` is already taken. Note: this is a
    pre-check, not a database-constraint-driven retry -- a race between two
    concurrent signups for the same slug is a known, accepted gap (the
    `slug` column's own uniqueness constraint is the final backstop against
    actually storing a duplicate, it just wouldn't surface as this clean an
    error in that narrow race window).
    """
    if actor is not None:
        require_permission(actor, _MANAGE_PERMISSION)

    existing = await repository.get_organization_by_slug(session, data.slug)
    if existing is not None:
        raise ConflictError(
            "An organization with this slug already exists.",
            error_code="organization.slug_taken",
            detail={"slug": data.slug},
        )

    org_row = await repository.insert_organization(
        session, name=data.name, slug=data.slug
    )
    # Milestone 10 RLS (c7d4e8f19a2b) enforces `app.current_organization_id`
    # on every tenant-owned table, `projects` included -- the default
    # project's INSERT below fails under RLS unless the tenant context is
    # set first. There is no actor/Identity yet to have set it already (see
    # this function's own docstring), so it must be set here, using the
    # organization row just created above, before that insert.
    await set_tenant_context(session, org_row.id)
    await repository.insert_project(
        session, organization_id=org_row.id, name="General", is_default=True
    )

    if actor is not None:
        await record_audit_event(
            session,
            actor,
            action="organization.create",
            resource_type="organization",
            resource_id=org_row.id,
            metadata={"slug": org_row.slug},
        )

    logger.info(
        "organization_created", organization_id=str(org_row.id), slug=org_row.slug
    )
    return Organization.model_validate(org_row)


async def get_organization(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> Organization:
    """Fetch one organization. Raises NotFoundError if it doesn't exist."""
    _ensure_same_organization(actor, organization_id)

    row = await repository.get_organization_by_id(session, organization_id)
    if row is None:
        raise NotFoundError(
            "Organization not found.",
            error_code="organization.not_found",
            detail={"organization_id": str(organization_id)},
        )
    return Organization.model_validate(row)


async def list_organizations(session: AsyncSession) -> list[Organization]:
    """Return every organization in the system, unscoped.

    No `actor` parameter, by design -- like `get_organization_sso_config`,
    this is a deliberate exception to "every operation takes an actor and is
    checked against it," not an oversight. The only legitimate caller is a
    scheduled, system-internal job that must iterate every tenant by
    definition (`app.agents.workers.tasks`'s Knowledge Gap Agent cron,
    Milestone 9 -- mirroring `app.ingestion.workers.tasks.
    scheduled_reconciliation`'s identical precedent of calling a repository-
    level, unscoped listing directly from a worker task rather than through
    a normal actor-scoped service call). There is no narrower organization
    to scope this to when the whole point of the call is "every
    organization" -- nothing about this function is reachable from REST or
    MCP, where an actor is always available and this would be the wrong
    tool.
    """
    rows = await repository.list_organizations(session)
    return [Organization.model_validate(row) for row in rows]


async def get_organization_sso_config(
    session: AsyncSession, org_slug: str
) -> SSOConfiguration:
    """Resolve an organization's SSO configuration by its login-URL slug.

    Called *before* any Identity exists -- this is the very first step of the
    SSO login flow (PROJECT_PLAN.md section 3.3, section 11.1:
    `GET /o/{org-slug}/login`), so unlike every other function in this module
    it takes no `actor` and performs no tenant-isolation check: the slug
    itself is the only thing identifying which organization the employee is
    trying to log into.

    Raises NotFoundError if the slug doesn't resolve to an organization, or if
    the organization exists but has no SSO configured yet (an organization
    mid-onboarding, before an IT Admin has connected an IdP).

    Milestone 10 RLS note: `get_organization_by_slug` needs no bypass --
    `organizations` itself is deliberately excluded from RLS (see the RLS
    migration's own docstring), so this lookup succeeds unrestricted before
    `organization_id` is even known. The very next query, though
    (`sso_configurations`, which *is* RLS-protected), does need the GUC set
    first -- done immediately below, the moment `org_row.id` is known.
    """
    org_row = await repository.get_organization_by_slug(session, org_slug)
    if org_row is None:
        raise NotFoundError(
            "Organization not found.",
            error_code="organization.not_found",
            detail={"slug": org_slug},
        )

    await set_tenant_context(session, org_row.id)
    sso_row = await repository.get_sso_configuration_by_organization_id(
        session, org_row.id
    )
    if sso_row is None:
        raise NotFoundError(
            "This organization has not configured SSO yet.",
            error_code="sso_configuration.not_found",
            detail={"organization_id": str(org_row.id)},
        )
    return SSOConfiguration.model_validate(sso_row)


_REDACTED_CLIENT_SECRET = "••••••••"  # noqa: RUF001 -- intentional bullet glyphs, not a lookalike-character bug


def _redact_client_secret(config: SSOConfiguration) -> SSOConfiguration:
    """Never let a caller's actual `client_secret_ref` column value --
    envelope-encrypted ciphertext, not a human-readable reference -- reach
    the wire. Every read of an `SSOConfiguration` (both the write response
    below and `get_sso_config`) goes through this before returning.
    """
    return config.model_copy(update={"client_secret_ref": _REDACTED_CLIENT_SECRET})


async def get_sso_config(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> SSOConfiguration:
    """Read back `organization_id`'s SSO configuration for display in
    settings UI (`SsoSettingsPage.tsx`'s `getSsoConfig`, previously pointed
    at a GET endpoint that never existed on the backend at all).

    Requires `tenancy:manage` -- unlike most org-scoped reads (e.g.
    `list_projects`), viewing IdP configuration (issuer URL, client id) is
    treated the same as changing it, not as a general-membership read.
    Raises NotFoundError if SSO hasn't been configured yet (same error code
    `get_organization_sso_config`'s pre-login lookup already uses).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    row = await repository.get_sso_configuration_by_organization_id(session, organization_id)
    if row is None:
        raise NotFoundError(
            "This organization has not configured SSO yet.",
            error_code="sso_configuration.not_found",
            detail={"organization_id": str(organization_id)},
        )
    return _redact_client_secret(SSOConfiguration.model_validate(row))


async def configure_sso(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: SSOConfigurationCreate,
) -> SSOConfiguration:
    """Configure `organization_id`'s SSO provider for the first time.

    Raises ConflictError if SSO is already configured -- replacing an
    existing configuration is a distinct, not-yet-built operation (see
    repository.insert_sso_configuration's docstring), not silently handled
    here as an upsert.

    `data.client_secret_ref` is envelope-encrypted before being persisted,
    the same encrypt-at-write/decrypt-at-read split `register_connector`
    already established for connector credentials -- previously this
    function stored the client secret unencrypted, and `core.auth.service.
    _resolve_client_secret` read it back as if it already were plaintext.
    Both ends of that gap are fixed together; see `_resolve_client_secret`'s
    updated docstring.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    existing = await repository.get_sso_configuration_by_organization_id(
        session, organization_id
    )
    if existing is not None:
        raise ConflictError(
            "SSO is already configured for this organization.",
            error_code="sso_configuration.already_exists",
            detail={"organization_id": str(organization_id)},
        )

    try:
        encrypted_client_secret_ref = await encrypt_secret(get_kms(), data.client_secret_ref)
    except KmsUnavailableError as exc:
        # Same translation as `register_connector` above -- this call site
        # shares the exact same `encrypt_secret(get_kms(), ...)` dependency
        # and was failing identically (verified directly against production:
        # both reproduced the same unhandled-500-with-no-CORS-headers symptom).
        raise ServiceUnavailableError(
            "The SSO client secret cannot be stored right now -- the key "
            "management service is unreachable or misconfigured. Try again "
            "shortly; if this persists, it needs operator attention.",
            error_code="sso_configuration.kms_unavailable",
        ) from exc
    row = await repository.insert_sso_configuration(
        session,
        organization_id=organization_id,
        provider=data.provider,
        protocol=data.protocol,
        issuer_url=data.issuer_url,
        client_id=data.client_id,
        client_secret_ref=encrypted_client_secret_ref,
    )
    await record_audit_event(
        session,
        actor,
        action="sso_configuration.configure",
        resource_type="sso_configuration",
        resource_id=row.id,
        metadata={"organization_id": str(organization_id), "provider": data.provider},
    )
    return _redact_client_secret(SSOConfiguration.model_validate(row))


# --- Projects ----------------------------------------------------------------


async def list_projects(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[Project]:
    """Return every project belonging to `organization_id`."""
    _ensure_same_organization(actor, organization_id)

    rows = await repository.list_projects(session, organization_id)
    return [Project.model_validate(row) for row in rows]


async def get_default_project(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> Project:
    """Fetch `organization_id`'s auto-created default project.

    Used by callers (core/incidents, when a caller omits `project_id` on
    incident creation) that need "the project" for an organization that
    hasn't bothered creating more than one (PROJECT_PLAN.md section 3.2).
    Raises NotFoundError in the pathological case where an organization
    somehow has none -- should be unreachable given `create_organization`
    always creates one alongside the organization itself, but not re-derived
    or assumed here.
    """
    _ensure_same_organization(actor, organization_id)

    row = await repository.get_default_project(session, organization_id)
    if row is None:
        raise NotFoundError(
            "This organization has no default project.",
            error_code="project.default_missing",
            detail={"organization_id": str(organization_id)},
        )
    return Project.model_validate(row)


async def create_project(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: ProjectCreate,
) -> Project:
    """Create a new project within `organization_id`."""
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    row = await repository.insert_project(
        session,
        organization_id=organization_id,
        name=data.name,
        is_default=data.is_default,
    )
    await record_audit_event(
        session,
        actor,
        action="project.create",
        resource_type="project",
        resource_id=row.id,
        metadata={"organization_id": str(organization_id), "name": data.name},
    )
    return Project.model_validate(row)


# --- Connector configuration ----------------------------------------------------

_REDACTED_CREDENTIAL = "••••••••"  # noqa: RUF001 -- intentional bullet glyphs, matching _REDACTED_CLIENT_SECRET above


def _redact_credential(config: ConnectorConfig) -> ConnectorConfig:
    """Never let a caller's actual `credential_ref` column value --
    envelope-encrypted ciphertext, not a human-readable reference -- reach
    the wire. Every read of a `ConnectorConfig` goes through this before
    returning, the same treatment `_redact_client_secret` already gives
    `SSOConfiguration.client_secret_ref` (this field was the one
    inconsistency a Phase 3 security audit found: identical sensitivity,
    previously returned unredacted here).
    """
    # Underscore-prefixed values are reserved worker state (for example
    # SharePoint delta links and ingestion pagination checkpoints). Provider
    # cursors can embed bearer-like query parameters and must never be
    # serialized through the connector-management API.
    public_config = {key: value for key, value in config.config.items() if not key.startswith("_")}
    return config.model_copy(
        update={"credential_ref": _REDACTED_CREDENTIAL, "config": public_config}
    )


async def register_connector(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: ConnectorConfigCreate,
) -> ConnectorConfig:
    """Register a new (organization, external tool) connection.

    If `data.project_id` is given, verifies that project actually belongs to
    `organization_id` -- without this check, a caller could otherwise scope a
    connector to a project belonging to a *different* organization, which
    would be a tenant-isolation leak at write time rather than read time.
    Once validated, the `tenancy:manage` check is itself narrowed to that
    project (`require_project_permission`) rather than the organization as a
    whole -- a user granted `tenancy:manage` only on one project should not
    thereby be able to register a connector scoped to a different project in
    the same organization. A connector with no `project_id` (org-wide) still
    requires the plain org-level permission, since there is no narrower scope
    to check it against.

    `data.credential_ref` (the plaintext credential a caller submits -- e.g.
    a Slack bot token, a Jira API token pair) is envelope-encrypted (§12.5,
    `app.shared.security`) before it is ever persisted; only the encrypted
    envelope is stored, and the plaintext value is never logged or written
    to `connector_configs` directly. `ingestion.service._execute_ingestion_
    job` is the sole place that decrypts it back, immediately before a
    connector's `authenticate()` needs it -- see that function's own
    docstring.
    """
    _ensure_same_organization(actor, organization_id)

    if data.project_id is not None:
        project_row = await repository.get_project_by_id(session, data.project_id)
        if project_row is None or project_row.organization_id != organization_id:
            raise ValidationError(
                "project_id does not belong to this organization.",
                error_code="connector_config.invalid_project",
                detail={
                    "organization_id": str(organization_id),
                    "project_id": str(data.project_id),
                },
            )
        require_project_permission(actor, data.project_id, _MANAGE_PERMISSION)
    else:
        require_permission(actor, _MANAGE_PERMISSION)

    try:
        encrypted_credential_ref = await encrypt_secret(get_kms(), data.credential_ref)
    except KmsUnavailableError as exc:
        # See `KmsUnavailableError`'s own docstring: without this translation
        # an unreachable/misconfigured KMS surfaced as a bare, unhandled 500
        # that never passed back through `CORSMiddleware` -- indistinguishable,
        # in a browser, from a CORS failure (this is exactly what "adding a
        # connector" was doing before this fix, for every source, not just one).
        raise ServiceUnavailableError(
            "Connector credentials cannot be stored right now -- the key "
            "management service is unreachable or misconfigured. Try again "
            "shortly; if this persists, it needs operator attention.",
            error_code="connector_config.kms_unavailable",
        ) from exc
    row = await repository.insert_connector_config(
        session,
        organization_id=organization_id,
        source=data.source,
        credential_ref=encrypted_credential_ref,
        project_id=data.project_id,
        config=data.config,
    )
    await record_audit_event(
        session,
        actor,
        action="connector_config.register",
        resource_type="connector_config",
        resource_id=row.id,
        metadata={"organization_id": str(organization_id), "source": data.source},
    )
    return _redact_credential(ConnectorConfig.model_validate(row))


async def list_connectors(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[ConnectorConfig]:
    """Return every connector configuration belonging to `organization_id`.

    Gated by `tenancy:manage`, matching `register_connector`/`get_connector`
    -- a connector's `credential_ref` field is an encrypted reference, not a
    plaintext secret, but the list of what's connected and its sync status
    is still `tenancy:manage`-scoped configuration data, not something every
    org member should see by default. Previously ungated while its sibling
    write (`register_connector`) already required this permission -- a real
    inconsistency, not an intentional "reads are open" exception.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    rows = await repository.list_connector_configs(session, organization_id)
    return [_redact_credential(ConnectorConfig.model_validate(row)) for row in rows]


async def get_connector(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
) -> ConnectorConfig:
    """Fetch one connector configuration, enforcing the same ownership and
    `tenancy:manage` permission check `register_connector` applies at write
    time -- the read-then-act counterpart needed by `POST /tenancy/
    connectors/{id}/sync` (the API layer enqueues the actual ingestion job
    itself, via its own injected `arq` pool; this function only answers
    "does this connector exist, belong to this organization, and may `actor`
    act on it," the same shape `update_connector_sync_status` already
    establishes for its own ownership check).
    """
    _ensure_same_organization(actor, organization_id)

    row = await repository.get_connector_config_by_id(session, connector_config_id)
    if row is None or row.organization_id != organization_id:
        raise NotFoundError(
            "Connector configuration not found.",
            error_code="connector_config.not_found",
            detail={"connector_config_id": str(connector_config_id)},
        )

    if row.project_id is not None:
        require_project_permission(actor, row.project_id, _MANAGE_PERMISSION)
    else:
        require_permission(actor, _MANAGE_PERMISSION)

    return _redact_credential(ConnectorConfig.model_validate(row))


async def list_ingestion_runs(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[IngestionRun]:
    """List `connector_config_id`'s ingestion run history, newest first.

    Phase 2D addition -- backs `GET /tenancy/connectors/{id}/runs`, the
    frontend's ingestion-monitoring page. No new permission was introduced:
    `tenancy:manage` already gates everything else about a connector
    (`register_connector`, `list_connectors`, `get_connector`) -- ingestion
    run history is the same kind of connector-configuration-adjacent data,
    not a separately-owned resource, so reusing the existing permission is
    correct per this codebase's own "don't invent a permission a real one
    already covers" convention. `get_connector` already applies exactly the
    ownership + permission check this needs, so this reuses it rather than
    duplicating that check a third time.
    """
    connector = await get_connector(session, actor, organization_id, connector_config_id)
    rows = await repository.list_ingestion_runs(
        session, organization_id, connector.id, limit=limit, offset=offset
    )
    return [IngestionRun.model_validate(row) for row in rows]


async def get_replayable_ingestion_run(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    job_id: uuid.UUID,
) -> IngestionRun:
    """Authorize an explicit replay of one exhausted ingestion attempt."""
    connector = await get_connector(session, actor, organization_id, connector_config_id)
    row = await repository.get_ingestion_run(
        session, organization_id, connector.id, job_id
    )
    if row is None:
        raise NotFoundError(
            "Ingestion run not found.",
            error_code="ingestion_job.not_found",
            detail={"job_id": str(job_id)},
        )
    run = IngestionRun.model_validate(row)
    if run.status != "dead_lettered":
        raise ConflictError(
            "Only a dead-lettered ingestion run can be replayed.",
            error_code="ingestion_job.not_replayable",
            detail={"job_id": str(job_id), "status": run.status},
        )
    return run


async def get_ingestion_job_stats(
    session: AsyncSession, actor: Identity, *, since: datetime | None = None
) -> list[IngestionJobStats]:
    """Per-connector ingestion run aggregate for `actor.organization_id`
    (Phase 5.6) -- backs `GET /observability/ingestion`, the
    `ingestion_jobs`-side counterpart to `agents.service.
    get_agent_execution_stats`/`core.observability.service.
    get_mcp_dashboard`. See `_OBSERVABILITY_READ_PERMISSION`'s own comment
    for why this reuses `observability:read`, not `tenancy:manage`.
    """
    require_permission(actor, _OBSERVABILITY_READ_PERMISSION)
    rows = await repository.get_ingestion_job_stats(session, organization_id=actor.organization_id, since=since)
    return [
        IngestionJobStats(
            connector_config_id=row.connector_config_id,
            run_count=row.run_count,
            succeeded_count=int(row.succeeded_count or 0),
            failed_count=int(row.failed_count or 0),
            dead_lettered_count=int(getattr(row, "dead_lettered_count", 0) or 0),
            avg_duration_seconds=(
                float(row.avg_duration_seconds) if row.avg_duration_seconds is not None else None
            ),
            total_documents_processed=int(row.total_documents_processed or 0),
            total_pages_fetched=int(getattr(row, "total_pages_fetched", 0) or 0),
            total_items_discovered=int(getattr(row, "total_items_discovered", 0) or 0),
            total_items_skipped=int(getattr(row, "total_items_skipped", 0) or 0),
            total_chunks_embedded=int(getattr(row, "total_chunks_embedded", 0) or 0),
            total_retries=int(getattr(row, "total_retries", 0) or 0),
        )
        for row in rows
    ]


async def update_connector_sync_status(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    *,
    status: str,
    last_synced_at: datetime | None = None,
    config_patch: dict | None = None,
) -> ConnectorConfig:
    """Record a connector's sync outcome (PROJECT_PLAN.md section 4.5:
    "job status is tracked explicitly since the caller and worker no longer
    share a call stack") -- ingestion's new consumer of this module
    (app/ingestion/service.py, task #12).

    `config_patch`, when given, is shallow-merged into the connector's
    existing `config` JSONB rather than replacing it -- ingestion's own
    caller uses this to persist a cross-sync resume token (`FetchResult.
    resume_token`, see that field's docstring) under a reserved `
    "_resume_token"` key without disturbing the admin-supplied keys
    (`site_ids`/`channels`/...) already living in the same JSONB blob.

    Deliberately NOT gated by `require_permission(_MANAGE_PERMISSION)`,
    unlike `register_connector`/`configure_sso`: this is a system-triggered
    completion step reporting the outcome of a job that was already
    legitimately running -- ingestion's worker calls this as
    `Identity.for_agent("ingestion_worker", organization_id)`, not on behalf
    of a human requesting a new privileged action. This mirrors
    `core.incidents.service.create_postmortem`'s reasoning for why
    persisting an already-triggered background result isn't itself
    permission-gated. `_ensure_same_organization` still applies
    unconditionally: that's a structural tenant-isolation invariant, not a
    business permission, and holds regardless of who or what is calling.
    """
    _ensure_same_organization(actor, organization_id)

    existing = await repository.get_connector_config_by_id(session, connector_config_id)
    if existing is None or existing.organization_id != organization_id:
        raise NotFoundError(
            "Connector configuration not found.",
            error_code="connector_config.not_found",
            detail={"connector_config_id": str(connector_config_id)},
        )

    row = await repository.update_connector_config_sync_status(
        session,
        connector_config_id,
        status=status,
        last_synced_at=last_synced_at,
        config_patch=config_patch,
    )
    if row is None:
        raise RuntimeError("Connector configuration disappeared mid-update.")  # unreachable: fetched above

    await record_audit_event(
        session,
        actor,
        action="connector_config.sync_status_update",
        resource_type="connector_config",
        resource_id=connector_config_id,
        metadata={"status": status},
    )
    return _redact_credential(ConnectorConfig.model_validate(row))


async def checkpoint_connector_sync(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
    *,
    config_patch: dict,
) -> ConnectorConfig:
    """Durably save an ingestion cursor without emitting an audit event.

    Status changes remain audited through :func:`update_connector_sync_status`.
    This separate system-only path exists because pagination checkpoints are
    operational state written after every completed page, not user/domain
    actions. Tenant ownership is still checked at both the service and query
    boundaries.
    """
    _ensure_same_organization(actor, organization_id)
    existing = await repository.get_connector_config_by_id(session, connector_config_id)
    if existing is None or existing.organization_id != organization_id:
        raise NotFoundError(
            "Connector configuration not found.",
            error_code="connector_config.not_found",
            detail={"connector_config_id": str(connector_config_id)},
        )
    row = await repository.patch_connector_config_ingestion_checkpoint(
        session, connector_config_id, config_patch=config_patch
    )
    if row is None:
        raise RuntimeError("Connector configuration disappeared mid-update.")
    return _redact_credential(ConnectorConfig.model_validate(row))


async def disconnect_connector(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    connector_config_id: uuid.UUID,
) -> ConnectorConfig:
    """User-facing "delete a connector" (`DELETE /tenancy/connectors/{id}`).

    A real hard `DELETE` on `connector_configs` is not available: `ingestion_
    jobs.connector_config_id` is `ON DELETE RESTRICT` (deliberately, to
    protect a connector's own sync/job history from disappearing along with
    it), so Postgres itself refuses to drop a row that has ever been synced
    even once -- which in practice is nearly every connector a user would
    ever want to remove. `ConnectorStatus` already defines `"disconnected"`
    for exactly this case (present in the schema/type since Milestone 9's
    original design, never previously reachable from any code path) -- this
    reuses `update_connector_config_sync_status` (the same repository
    function ingestion's own worker calls to report sync outcomes) to set
    it, rather than inventing a second update path.

    Two real, load-bearing effects of the resulting `"disconnected"` status,
    not just a cosmetic label:
      1. `list_active_connector_config_ids()` (`d2e5f8a3c1b6`) -- the
         `SECURITY DEFINER` function `scheduled_reconciliation`'s hourly cron
         pass uses to pick which connectors to re-sync -- only ever selects
         `'active'`/`'error'` rows. A disconnected connector is silently
         excluded from all future automatic reconciliation, with no
         additional code needed here.
      2. `app.ingestion.service._execute_ingestion_job` checks this status
         immediately after loading the connector row and refuses to proceed
         (a fast `ConflictError`, not a full sync attempt) -- covering both
         a stale manual "Sync now" already in flight when this call lands,
         and, more importantly, any `arq` retry already queued for a job
         that timed out before the user disconnected it: the retry still
         fires, but now fails in milliseconds instead of running the same
         slow sync to the same timeout a second and third time.

    Reachable from the UI as a card-level "Delete" action even though the
    row survives -- the frontend's own connector list filters `"disconnected"`
    rows out of the default view (see `ConnectorsPage.tsx`), so this reads
    as "gone" to the user while `ingestion_jobs`/`documents` history stays
    intact for anyone with direct database/audit-log access.
    """
    connector = await get_connector(session, actor, organization_id, connector_config_id)

    row = await repository.update_connector_config_sync_status(
        session, connector.id, status="disconnected"
    )
    if row is None:
        raise RuntimeError("Connector configuration disappeared mid-update.")  # unreachable: fetched above

    await record_audit_event(
        session,
        actor,
        action="connector_config.disconnect",
        resource_type="connector_config",
        resource_id=connector_config_id,
    )
    return _redact_credential(ConnectorConfig.model_validate(row))


# --- Organization access rules (domain / group auto-join) --------------------


async def _resolve_role_id(session: AsyncSession, role_name: str) -> uuid.UUID:
    """Resolve a role name (as supplied on `AccessRuleCreate`/`InvitationCreate`)
    to its id, raising a clean domain error rather than letting a bad name
    surface as a foreign-key violation at insert time.
    """
    role = await users_repository.get_role_by_name(session, role_name)
    if role is None:
        raise ValidationError(
            "Unknown role name.",
            error_code="role.not_found",
            detail={"role": role_name},
        )
    return role.id


async def create_access_rule(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: AccessRuleCreate,
) -> AccessRule:
    """Create a domain/group auto-join rule for `organization_id`."""
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    role_id = await _resolve_role_id(session, data.grants_role)
    row = await repository.insert_access_rule(
        session,
        organization_id=organization_id,
        rule_type=data.rule_type,
        value=data.value,
        grants_role_id=role_id,
        is_active=data.is_active,
    )
    await record_audit_event(
        session,
        actor,
        action="access_rule.create",
        resource_type="organization_access_rule",
        resource_id=row.id,
        metadata={
            "organization_id": str(organization_id),
            "rule_type": data.rule_type,
            "value": data.value,
        },
    )
    return AccessRule.model_validate(row)


async def list_access_rules(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[AccessRule]:
    """Return every access rule (active or not) belonging to `organization_id`.

    Gated by `tenancy:manage`, matching `create_access_rule`/
    `deactivate_access_rule` -- previously ungated, a real inconsistency
    (see `list_connectors`'s identical fix above).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    rows = await repository.list_access_rules(session, organization_id)
    return [AccessRule.model_validate(row) for row in rows]


async def deactivate_access_rule(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    rule_id: uuid.UUID,
) -> AccessRule:
    """Suspend an access rule without deleting it.

    Verifies the rule actually belongs to `organization_id` before touching
    it -- without this check, a caller could deactivate another
    organization's rule by guessing/enumerating its id, a write-time
    tenant-isolation leak of the same shape `register_connector` already
    guards against for `project_id`.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    rule_row = await repository.get_access_rule_by_id(session, rule_id)
    if rule_row is None or rule_row.organization_id != organization_id:
        raise NotFoundError(
            "Access rule not found.",
            error_code="access_rule.not_found",
            detail={"organization_id": str(organization_id), "rule_id": str(rule_id)},
        )

    row = await repository.deactivate_access_rule(session, rule_id)
    await record_audit_event(
        session,
        actor,
        action="access_rule.deactivate",
        resource_type="organization_access_rule",
        resource_id=rule_id,
        metadata={"organization_id": str(organization_id)},
    )
    return AccessRule.model_validate(row)


# --- Invitations ---------------------------------------------------------------


async def create_invitation(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    data: InvitationCreate,
) -> Invitation:
    """Invite `data.email` to join `organization_id`.

    Requires `actor.user_id` to be set -- only a `USER`-kind identity (an
    actual admin) can send an invitation, since `invitations.invited_by` is a
    required reference to a `users` row; a service/agent identity has none.
    Raises ConflictError if a pending invitation for this email already
    exists (the partial unique index's application-level counterpart, for a
    clean domain error instead of a raw integrity-error surface).

    Phase 7.5: generates a random, single-use token, stores only its hash
    (`token_hash`), and returns the raw value exactly once via the response
    `Invitation.token` field -- the caller (an admin UI, or whatever sends
    the actual invitation email) is responsible for including it in the
    invitation link. This is what `POST /invitations/{id}/accept`'s
    password-organization flow (`core.auth.service.
    accept_invitation_with_password`) checks as proof the accepter controls
    the invited email address -- previously the invitation's own database id
    was the only "token," provable by anyone who merely learned it.
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    if actor.user_id is None:
        raise ValidationError(
            "Only a user identity can send invitations.",
            error_code="invitation.invalid_actor",
        )

    existing = await repository.get_pending_invitation(session, organization_id, data.email)
    if existing is not None:
        raise ConflictError(
            "An invitation is already pending for this email.",
            error_code="invitation.already_pending",
            detail={"organization_id": str(organization_id), "email": data.email},
        )

    role_id = await _resolve_role_id(session, data.grants_role)
    expires_at = data.expires_at or (datetime.now(timezone.utc) + _DEFAULT_INVITATION_LIFETIME)

    raw_token = generate_opaque_token()
    row = await repository.insert_invitation(
        session,
        organization_id=organization_id,
        email=data.email,
        grants_role_id=role_id,
        invited_by=actor.user_id,
        expires_at=expires_at,
        token_hash=hash_opaque_token(raw_token),
    )
    await record_audit_event(
        session,
        actor,
        action="invitation.create",
        resource_type="invitation",
        # Never the raw token -- see this function's own docstring on why
        # it must be exposed exactly once, in the response only.
        metadata={"organization_id": str(organization_id), "email": data.email},
        resource_id=row.id,
    )
    return Invitation.model_validate(row).model_copy(update={"token": raw_token})


async def list_invitations(
    session: AsyncSession, actor: Identity, organization_id: uuid.UUID
) -> list[Invitation]:
    """Return every invitation belonging to `organization_id`, newest first.

    Gated by `tenancy:manage`, matching `create_invitation`/
    `revoke_invitation` -- previously ungated, which leaked every invited
    email address in the organization to any authenticated org member (see
    `list_connectors`'s identical fix above for the same class of bug).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    rows = await repository.list_invitations(session, organization_id)
    return [Invitation.model_validate(row) for row in rows]


async def revoke_invitation(
    session: AsyncSession,
    actor: Identity,
    organization_id: uuid.UUID,
    invitation_id: uuid.UUID,
) -> Invitation:
    """Revoke a pending invitation before it's accepted or expires.

    Raises ConflictError if the invitation is not currently `"pending"` --
    an already-accepted or already-expired/revoked invitation is a closed
    state machine, mirroring the `status`-transition discipline already used
    for postmortems (DATABASE_DESIGN.md).
    """
    _ensure_same_organization(actor, organization_id)
    require_permission(actor, _MANAGE_PERMISSION)

    invitation_row = await repository.get_invitation_by_id(session, invitation_id)
    if invitation_row is None or invitation_row.organization_id != organization_id:
        raise NotFoundError(
            "Invitation not found.",
            error_code="invitation.not_found",
            detail={"organization_id": str(organization_id), "invitation_id": str(invitation_id)},
        )
    if invitation_row.status != "pending":
        raise ConflictError(
            "Only a pending invitation can be revoked.",
            error_code="invitation.not_pending",
            detail={"status": invitation_row.status},
        )

    row = await repository.update_invitation_status(session, invitation_id, status="revoked")
    await record_audit_event(
        session,
        actor,
        action="invitation.revoke",
        resource_type="invitation",
        resource_id=invitation_id,
        metadata={"organization_id": str(organization_id)},
    )
    return Invitation.model_validate(row)


async def accept_invitation(session: AsyncSession, invitation_id: uuid.UUID) -> None:
    """Mark an invitation accepted.

    Called by core/auth only after the invited user has actually been
    created/linked -- kept as a separate, explicit step from
    `evaluate_provisioning` (which only decides whether provisioning is
    *allowed*), even though both happen inside the same database transaction
    as the rest of SSO login completion. No `actor`: this runs as part of the
    same pre-session login flow as `evaluate_provisioning`, not as an
    admin-facing action.

    Guards added for its second caller, `auth_service.
    accept_invitation_with_password` (Phase 7.5 -- itself behind
    `POST /invitations/{invitation_id}/accept`, which verifies the caller's
    `token` against `Invitation.token_hash` *before* ever reaching here):
    raises NotFoundError for an unknown id, and ConflictError if the
    invitation is not currently `"pending"` (already accepted/revoked) or has
    passed its `expires_at`. `evaluate_provisioning` only ever passes an id it
    just confirmed satisfies both conditions, so these checks are a no-op,
    defense-in-depth addition for that call path, not a behavior change for
    it; for the password-acceptance path they're deliberately re-checked here
    too, rather than trusted to still hold from the caller's own earlier
    check, in case anything changed between the two.

    Does not itself check `token_hash` -- that proof-of-control check happens
    once, in `auth_service.accept_invitation_with_password`, before user
    provisioning even starts. This function only flips `status`; it has no
    token to check for its other caller (`evaluate_provisioning`'s SSO path),
    where a signed `id_token` already proved identity.
    """
    invitation = await repository.get_invitation_by_id(session, invitation_id)
    if invitation is None:
        raise NotFoundError(
            "Invitation not found.",
            error_code="invitation.not_found",
            detail={"invitation_id": str(invitation_id)},
        )
    if invitation.status != "pending":
        raise ConflictError(
            "Only a pending invitation can be accepted.",
            error_code="invitation.not_pending",
            detail={"status": invitation.status},
        )
    if invitation.expires_at <= datetime.now(timezone.utc):
        raise ConflictError(
            "This invitation has expired.",
            error_code="invitation.expired",
            detail={"invitation_id": str(invitation_id)},
        )

    await repository.update_invitation_status(
        session, invitation_id, status="accepted", accepted_at=datetime.now(timezone.utc)
    )


# --- Provisioning policy evaluation ----------------------------------------------


async def evaluate_provisioning(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    email: str,
    groups: Sequence[str] = (),
) -> ProvisioningDecision:
    """Decide whether a verified SSO login may provision a user in
    `organization_id`, and which role it should receive if so.

    No `actor` parameter: this runs mid-login, before any session/Identity
    exists yet (same precedent as `get_organization_sso_config`) -- it is
    core/auth's job to call this, never the reverse, keeping "is this
    authentication valid" and "is this login authorized to provision an
    account" as two distinct steps (this migration's whole point).

    Precedence, most to least specific:
      1. A pending, unexpired invitation for this exact `email`.
      2. An active `domain` rule matching the email's domain.
      3. An active `group` rule matching one of `groups` (only checked if the
         IdP actually sent a groups claim -- an empty `groups` sequence
         means "no group claim available," not "matches no group," and
         group rules are simply skipped rather than treated as a denial
         signal on their own).
      4. Otherwise, denied.

    Milestone 10 RLS note: unlike `get_organization_sso_config`, this
    function is *handed* `organization_id` directly by its caller (core/auth,
    which resolved it via `get_organization_sso_config`'s own slug lookup
    earlier in the same login transaction) rather than discovering it itself
    -- so the GUC is set unconditionally at the top, before the first
    RLS-protected query (`invitations`) runs. Since `set_tenant_context` uses
    `SET LOCAL` (transaction-scoped, not connection-scoped), and SSO login
    completion runs `get_organization_sso_config` -> `evaluate_provisioning`
    -> `accept_invitation` inside one shared transaction, this same call also
    covers `accept_invitation`'s later `invitations` update on this session
    -- no separate wiring needed there.
    """
    await set_tenant_context(session, organization_id)
    now = datetime.now(timezone.utc)

    invitation = await repository.get_pending_invitation(session, organization_id, email)
    if invitation is not None:
        if invitation.expires_at > now:
            return ProvisioningDecision(
                allowed=True,
                grants_role_id=invitation.grants_role_id,
                matched_invitation_id=invitation.id,
                reason="invitation_match",
            )
        # Past-due but still "pending": lazily mark it expired now that
        # we've noticed (get_pending_invitation's docstring -- no sweep job
        # exists yet), then fall through to the coarser rule checks below.
        await repository.update_invitation_status(session, invitation.id, status="expired")
        logger.info(
            "invitation_expired",
            invitation_id=str(invitation.id),
            organization_id=str(organization_id),
        )

    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if domain:
        for rule in await repository.get_active_rules_by_type(session, organization_id, "domain"):
            if rule.value.lower() == domain:
                return ProvisioningDecision(
                    allowed=True, grants_role_id=rule.grants_role_id, reason="domain_match"
                )

    if groups:
        normalized_groups = {group.lower() for group in groups}
        for rule in await repository.get_active_rules_by_type(session, organization_id, "group"):
            if rule.value.lower() in normalized_groups:
                return ProvisioningDecision(
                    allowed=True, grants_role_id=rule.grants_role_id, reason="group_match"
                )

    logger.info(
        "provisioning_denied", organization_id=str(organization_id), email=email
    )
    return ProvisioningDecision(allowed=False, reason="no_matching_policy")
